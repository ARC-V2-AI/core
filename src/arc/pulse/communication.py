"""Core-owned local broker with a durable SQLite event queue.

The broker is deliberately separate from Pulse's control socket: the latter is
for supervisor commands, while this protocol is for service-to-service calls.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from arc_service.communication import (
    MAX_FRAME_BYTES,
    PROTOCOL_VERSION,
    EVENT_LEASE_SECONDS,
    Activate,
    EventAck,
    EventDelivery,
    EventExtend,
    EventNack,
    EventQueueFullError,
    Hello,
    HelloAck,
    ListServices,
    ProtocolErrorMessage,
    Publish,
    Published,
    RpcCall,
    RpcRequest,
    RpcResult,
    ServicesList,
)

logger = logging.getLogger(__name__)
MAX_DELIVERY_ATTEMPTS = 10
MAX_RETRY_DELAY_SECONDS = 60.0
DELIVERY_POLL_SECONDS = 0.2
MAX_QUEUED_EVENTS_PER_SUBSCRIBER = 10_000


@dataclass(slots=True)
class _Client:
    service_id: str
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    rpc_methods: set[str]
    subscriptions: set[str]
    active: bool = False
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    delivery_task: asyncio.Task[None] | None = None
    closed: bool = False

    async def send(self, message: Any) -> None:
        if isinstance(message, dict):
            data = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
        else:
            data = message.model_dump_json().encode("utf-8") + b"\n"
        if len(data) > MAX_FRAME_BYTES:
            raise ValueError(f"Message exceeds {MAX_FRAME_BYTES} bytes")
        async with self.send_lock:
            if self.closed or self.writer.is_closing():
                raise ConnectionError("service connection is closed")
            self.writer.write(data)
            await self.writer.drain()


class CommunicationBroker:
    """Async Unix-socket broker; SQLite owns durable event and delivery state."""

    def __init__(
        self,
        socket_path: str | Path,
        database_path: str | Path,
        *,
        delivery_lease_seconds: float = EVENT_LEASE_SECONDS,
        max_queued_events_per_subscriber: int = MAX_QUEUED_EVENTS_PER_SUBSCRIBER,
    ) -> None:
        self.socket_path = Path(socket_path).expanduser()
        self.database_path = Path(database_path).expanduser()
        self.delivery_lease_seconds = max(15.0, delivery_lease_seconds)
        if max_queued_events_per_subscriber < 1:
            raise ValueError("max_queued_events_per_subscriber must be positive")
        self.max_queued_events_per_subscriber = max_queued_events_per_subscriber
        self._server: asyncio.AbstractServer | None = None
        self._clients: dict[str, _Client] = {}
        self._clients_lock = asyncio.Lock()
        self._db_lock = asyncio.Lock()
        self._pending_calls: dict[str, tuple[str, str]] = {}
        self._db: sqlite3.Connection | None = None

    async def start(self) -> None:
        if self._server is not None:
            return
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

        if self.socket_path.exists():
            # Do not unlink a live socket owned by another broker. A successful
            # connect means another instance is active and must not be replaced.
            try:
                reader, writer = await asyncio.open_unix_connection(
                    str(self.socket_path), limit=MAX_FRAME_BYTES
                )
                writer.close()
                await writer.wait_closed()
            except (OSError, ConnectionError):
                self.socket_path.unlink(missing_ok=True)
            else:
                raise RuntimeError(
                    f"ARC communication socket is already active: {self.socket_path}"
                )

        self._open_database()
        try:
            self._server = await asyncio.start_unix_server(
                self._handle_connection,
                path=str(self.socket_path),
                limit=MAX_FRAME_BYTES,
            )
            os.chmod(self.socket_path, 0o600)
        except BaseException:
            if self._db is not None:
                self._db.close()
                self._db = None
            self.socket_path.unlink(missing_ok=True)
            raise
        logger.info("ARC service communication listening at %s", self.socket_path)

    async def close(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            await server.wait_closed()

        async with self._clients_lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            await self._disconnect(client)

        if self.socket_path.exists():
            self.socket_path.unlink(missing_ok=True)
        if self._db is not None:
            self._db.close()
            self._db = None

    def _open_database(self) -> None:
        connection = sqlite3.connect(
            self.database_path,
            isolation_level=None,
            timeout=10.0,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            os.chmod(self.database_path, 0o600)
        except OSError:
            connection.close()
            raise
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                service_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                PRIMARY KEY (service_id, event_type)
            );

            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                source TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS deliveries (
                delivery_id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
                service_id TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                available_at REAL NOT NULL,
                lease_until REAL,
                last_error TEXT,
                UNIQUE (event_id, service_id)
            );
            CREATE INDEX IF NOT EXISTS idx_deliveries_service
                ON deliveries(service_id, available_at, lease_until);

            CREATE TABLE IF NOT EXISTS dead_letters (
                dead_letter_id TEXT PRIMARY KEY,
                delivery_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                source TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                service_id TEXT NOT NULL,
                attempts INTEGER NOT NULL,
                last_error TEXT,
                failed_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_dead_letters_service
                ON dead_letters(service_id, failed_at);
            """
        )
        self._db = connection
        # WAL/SHM contain persisted user-provided event payloads as well.
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{self.database_path}{suffix}")
            if sidecar.exists():
                try:
                    os.chmod(sidecar, 0o600)
                except OSError:
                    logger.warning("Could not restrict permissions on %s", sidecar)

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        client: _Client | None = None
        registered = False
        try:
            raw = await reader.readline()
            if not raw:
                return
            hello = Hello.model_validate_json(raw)
            if hello.protocol_version != PROTOCOL_VERSION:
                await self._send_raw(
                    writer,
                    HelloAck(
                        ok=False,
                        error=(
                            f"Protocol version mismatch: client={hello.protocol_version}, "
                            f"broker={PROTOCOL_VERSION}"
                        ),
                    ),
                )
                return

            client = _Client(
                service_id=hello.service_id,
                reader=reader,
                writer=writer,
                rpc_methods=set(hello.rpc_methods),
                subscriptions=set(hello.subscriptions),
            )
            async with self._clients_lock:
                if hello.service_id in self._clients:
                    existing = self._clients[hello.service_id]
                    if not existing.closed:
                        await client.send(
                            HelloAck(ok=False, error=f"Service {hello.service_id!r} is already connected")
                        )
                        return
                self._clients[hello.service_id] = client
                registered = True

            await self._sync_subscriptions(client)
            await client.send(HelloAck(ok=True))
            logger.info(
                "Service %s connected to communication broker (%d RPC methods, %d event handlers)",
                client.service_id,
                len(client.rpc_methods),
                len(client.subscriptions),
            )

            while True:
                raw = await reader.readline()
                if not raw:
                    break
                if len(raw) > MAX_FRAME_BYTES:
                    raise ValueError("incoming message exceeds maximum frame size")
                try:
                    message = json.loads(raw)
                    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
                        raise ValueError("message must be an object with a type field")
                    await self._dispatch(client, message)
                except (json.JSONDecodeError, ValidationError, ValueError) as exc:
                    logger.warning("Invalid communication message from %s: %s", client.service_id, exc)
                    await client.send(ProtocolErrorMessage(error=str(exc)[:2048]))
        except asyncio.CancelledError:
            raise
        except (ConnectionError, BrokenPipeError, ConnectionResetError, OSError):
            pass
        except (ValidationError, ValueError) as exc:
            logger.warning("Rejected ARC communication connection: %s", exc)
            if client is None:
                try:
                    await self._send_raw(writer, HelloAck(ok=False, error=str(exc)[:2048]))
                except Exception:
                    pass
        except Exception:
            logger.exception("ARC communication connection failed")
        finally:
            if client is not None and registered:
                async with self._clients_lock:
                    if self._clients.get(client.service_id) is client:
                        self._clients.pop(client.service_id, None)
                await self._disconnect(client)
            else:
                # A duplicate service connection is rejected without touching
                # the already-connected process's queue leases or RPC calls.
                writer.close()
                try:
                    await writer.wait_closed()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

    async def _send_raw(self, writer: asyncio.StreamWriter, message: Any) -> None:
        data = message.model_dump_json().encode("utf-8") + b"\n"
        writer.write(data)
        await writer.drain()

    async def _sync_subscriptions(self, client: _Client) -> None:
        """Persist current code-declared subscriptions and clear removed ones."""
        assert self._db is not None
        async with self._db_lock:
            db = self._db
            db.execute("BEGIN IMMEDIATE")
            try:
                if client.subscriptions:
                    placeholders = ",".join("?" for _ in client.subscriptions)
                    db.execute(
                        f"DELETE FROM deliveries WHERE service_id=? AND event_id IN "
                        f"(SELECT event_id FROM events WHERE event_type NOT IN ({placeholders}))",
                        (client.service_id, *sorted(client.subscriptions)),
                    )
                else:
                    db.execute("DELETE FROM deliveries WHERE service_id=?", (client.service_id,))
                db.execute("DELETE FROM subscriptions WHERE service_id=?", (client.service_id,))
                db.executemany(
                    "INSERT INTO subscriptions(service_id, event_type) VALUES (?, ?)",
                    [(client.service_id, event_type) for event_type in sorted(client.subscriptions)],
                )
                db.execute(
                    "DELETE FROM events WHERE NOT EXISTS "
                    "(SELECT 1 FROM deliveries WHERE deliveries.event_id=events.event_id)"
                )
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    async def _dispatch(self, client: _Client, raw: dict[str, Any]) -> None:
        message_type = raw.get("type")
        if message_type == "activate":
            Activate.model_validate(raw)
            if not client.active:
                client.active = True
                client.delivery_task = asyncio.create_task(
                    self._delivery_loop(client),
                    name=f"arc-event-delivery:{client.service_id}",
                )
            return
        if message_type == "call":
            message = RpcCall.model_validate(raw)
            await self._route_call(client, message)
            return
        if message_type == "rpc_result":
            message = RpcResult.model_validate(raw)
            await self._route_rpc_result(client, message)
            return
        if message_type == "publish":
            message = Publish.model_validate(raw)
            try:
                event_id, queued_for = await self._publish(client.service_id, message)
                response = Published(
                    id=message.id,
                    event_id=event_id,
                    queued_for=queued_for,
                )
            except EventQueueFullError as exc:
                response = Published(id=message.id, ok=False, error=str(exc))
            await client.send(response)
            return
        if message_type == "list_services":
            message = ListServices.model_validate(raw)
            await client.send(
                ServicesList(id=message.id, services=await self._service_summaries())
            )
            return
        if message_type == "ack":
            message = EventAck.model_validate(raw)
            await self._ack(client.service_id, message.delivery_id)
            return
        if message_type == "nack":
            message = EventNack.model_validate(raw)
            await self._nack(client.service_id, message.delivery_id, message.error)
            return
        if message_type == "extend":
            message = EventExtend.model_validate(raw)
            await self._extend(client.service_id, message.delivery_id)
            return
        raise ValueError(f"unknown message type: {message_type!r}")

    async def _route_call(self, caller: _Client, message: RpcCall) -> None:
        if message.target == caller.service_id:
            await caller.send(
                RpcResult(
                    id=message.id,
                    ok=False,
                    error="A service cannot make an RPC call to itself through the broker",
                    error_type="INVALID_TARGET",
                )
            )
            return
        async with self._clients_lock:
            target = self._clients.get(message.target)
        if target is None or target.closed or not target.active:
            await caller.send(
                RpcResult(
                    id=message.id,
                    ok=False,
                    error=f"Service {message.target!r} is not available or not active yet",
                    error_type="SERVICE_UNAVAILABLE",
                )
            )
            return
        if message.method not in target.rpc_methods:
            await caller.send(
                RpcResult(
                    id=message.id,
                    ok=False,
                    error=f"Service {message.target!r} does not expose method {message.method!r}",
                    error_type="RPC_METHOD_NOT_FOUND",
                )
            )
            return
        self._pending_calls[message.id] = (caller.service_id, target.service_id)
        try:
            await target.send(
                RpcRequest(
                    id=message.id,
                    caller=caller.service_id,
                    method=message.method,
                    payload=message.payload,
                )
            )
        except (ConnectionError, OSError, ValueError):
            self._pending_calls.pop(message.id, None)
            await caller.send(
                RpcResult(
                    id=message.id,
                    ok=False,
                    error=f"Service {message.target!r} disconnected while routing the call",
                    error_type="SERVICE_UNAVAILABLE",
                )
            )

    async def _route_rpc_result(self, responder: _Client, message: RpcResult) -> None:
        pending = self._pending_calls.pop(message.id, None)
        if pending is None:
            return
        caller_id, target_id = pending
        if target_id != responder.service_id:
            logger.warning("Ignoring RPC response with mismatched service identity from %s", responder.service_id)
            return
        async with self._clients_lock:
            caller = self._clients.get(caller_id)
        if caller is None or caller.closed:
            return
        try:
            await caller.send(message)
        except (ConnectionError, OSError, ValueError):
            logger.debug("Caller %s disconnected before RPC response", caller_id)

    async def _publish(self, source: str, message: Publish) -> tuple[str, int]:
        assert self._db is not None
        event_id = str(uuid.uuid4())
        occurred_at = datetime.now(UTC).isoformat()
        payload_json = json.dumps(message.payload, separators=(",", ":"), ensure_ascii=False)
        async with self._db_lock:
            db = self._db
            db.execute("BEGIN IMMEDIATE")
            try:
                recipients = [
                    row[0]
                    for row in db.execute(
                        "SELECT service_id FROM subscriptions WHERE event_type=? ORDER BY service_id",
                        (message.event_type,),
                    ).fetchall()
                ]
                for recipient in recipients:
                    queued = db.execute(
                        "SELECT COUNT(*) FROM deliveries WHERE service_id=?",
                        (recipient,),
                    ).fetchone()[0]
                    if queued >= self.max_queued_events_per_subscriber:
                        raise EventQueueFullError(
                            f"Durable queue for subscriber {recipient!r} reached its "
                            f"limit of {self.max_queued_events_per_subscriber} pending events"
                        )
                if recipients:
                    db.execute(
                        "INSERT INTO events(event_id,event_type,source,payload_json,occurred_at,created_at) "
                        "VALUES (?,?,?,?,?,?)",
                        (event_id, message.event_type, source, payload_json, occurred_at, time.time()),
                    )
                    db.executemany(
                        "INSERT INTO deliveries(delivery_id,event_id,service_id,attempts,available_at) "
                        "VALUES (?,?,?,0,?)",
                        [
                            (str(uuid.uuid4()), event_id, service_id, time.time())
                            for service_id in recipients
                        ],
                    )
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return event_id, len(recipients)

    async def _delivery_loop(self, client: _Client) -> None:
        try:
            while not client.closed and client.active:
                delivery = await self._claim_next(client.service_id)
                if delivery is None:
                    await asyncio.sleep(DELIVERY_POLL_SECONDS)
                    continue
                try:
                    await client.send(EventDelivery.model_validate(delivery))
                except (ConnectionError, OSError, ValueError):
                    return
                # Exactly one in-flight delivery per subscriber preserves order.
                # The loop waits on the leased head item until ACK or lease expiry.
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Event delivery loop failed for service %s", client.service_id)

    async def _claim_next(self, service_id: str) -> dict[str, Any] | None:
        assert self._db is not None
        now = time.time()
        async with self._db_lock:
            db = self._db
            db.execute("BEGIN IMMEDIATE")
            try:
                while True:
                    row = db.execute(
                        "SELECT d.delivery_id,d.event_id,d.service_id,d.attempts,d.available_at,"
                        "d.lease_until,d.last_error,e.event_type,e.source,e.payload_json,e.occurred_at,e.created_at "
                        "FROM deliveries d JOIN events e ON e.event_id=d.event_id "
                        "WHERE d.service_id=? ORDER BY e.created_at,e.event_id LIMIT 1",
                        (service_id,),
                    ).fetchone()
                    if row is None:
                        db.execute("COMMIT")
                        return None
                    if row["available_at"] > now or (
                        row["lease_until"] is not None and row["lease_until"] > now
                    ):
                        db.execute("COMMIT")
                        return None
                    if row["attempts"] >= MAX_DELIVERY_ATTEMPTS:
                        db.execute(
                            "INSERT INTO dead_letters(dead_letter_id,delivery_id,event_id,event_type,source,"
                            "payload_json,occurred_at,service_id,attempts,last_error,failed_at) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                str(uuid.uuid4()), row["delivery_id"], row["event_id"], row["event_type"],
                                row["source"], row["payload_json"], row["occurred_at"], service_id,
                                row["attempts"], row["last_error"], now,
                            ),
                        )
                        db.execute("DELETE FROM deliveries WHERE delivery_id=?", (row["delivery_id"],))
                        db.execute(
                            "DELETE FROM events WHERE event_id=? AND NOT EXISTS "
                            "(SELECT 1 FROM deliveries WHERE deliveries.event_id=events.event_id)",
                            (row["event_id"],),
                        )
                        continue
                    attempt = row["attempts"] + 1
                    db.execute(
                        "UPDATE deliveries SET attempts=?,lease_until=? WHERE delivery_id=?",
                        (attempt, now + self.delivery_lease_seconds, row["delivery_id"]),
                    )
                    result = {
                        "delivery_id": row["delivery_id"],
                        "event_id": row["event_id"],
                        "event_type": row["event_type"],
                        "source": row["source"],
                        "occurred_at": row["occurred_at"],
                        "payload": json.loads(row["payload_json"]),
                        "attempt": attempt,
                    }
                    db.execute("COMMIT")
                    return result
            except BaseException:
                db.execute("ROLLBACK")
                raise

    async def _ack(self, service_id: str, delivery_id: str) -> None:
        assert self._db is not None
        async with self._db_lock:
            db = self._db
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(
                    "SELECT event_id FROM deliveries WHERE delivery_id=? AND service_id=?",
                    (delivery_id, service_id),
                ).fetchone()
                if row is not None:
                    db.execute(
                        "DELETE FROM deliveries WHERE delivery_id=? AND service_id=?",
                        (delivery_id, service_id),
                    )
                    db.execute(
                        "DELETE FROM events WHERE event_id=? AND NOT EXISTS "
                        "(SELECT 1 FROM deliveries WHERE deliveries.event_id=events.event_id)",
                        (row["event_id"],),
                    )
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    async def _nack(self, service_id: str, delivery_id: str, error: str) -> None:
        assert self._db is not None
        async with self._db_lock:
            row = self._db.execute(
                "SELECT attempts FROM deliveries WHERE delivery_id=? AND service_id=?",
                (delivery_id, service_id),
            ).fetchone()
            if row is None:
                return
            delay = min(2.0 ** max(0, int(row["attempts"]) - 1), MAX_RETRY_DELAY_SECONDS)
            self._db.execute(
                "UPDATE deliveries SET available_at=?,lease_until=NULL,last_error=? "
                "WHERE delivery_id=? AND service_id=?",
                (time.time() + delay, error[:2048], delivery_id, service_id),
            )

    async def _extend(self, service_id: str, delivery_id: str) -> None:
        assert self._db is not None
        async with self._db_lock:
            self._db.execute(
                "UPDATE deliveries SET lease_until=? WHERE delivery_id=? AND service_id=?",
                (time.time() + self.delivery_lease_seconds, delivery_id, service_id),
            )

    async def _service_summaries(self) -> list[dict[str, Any]]:
        async with self._clients_lock:
            return [
                {
                    "service_id": client.service_id,
                    "active": client.active and not client.closed,
                    "methods": sorted(client.rpc_methods),
                    "subscriptions": sorted(client.subscriptions),
                }
                for client in sorted(self._clients.values(), key=lambda item: item.service_id)
                if not client.closed
            ]

    async def _disconnect(self, client: _Client) -> None:
        if client.closed:
            return
        client.closed = True
        client.active = False
        if client.delivery_task is not None:
            client.delivery_task.cancel()
            await asyncio.gather(client.delivery_task, return_exceptions=True)
            client.delivery_task = None

        # Release any in-flight delivery immediately. A handler that completed
        # just before disconnect may be run again; that is the documented
        # at-least-once contract, so handlers should be idempotent where possible.
        if self._db is not None:
            async with self._db_lock:
                self._db.execute(
                    "UPDATE deliveries SET lease_until=NULL,available_at=? WHERE service_id=? AND lease_until IS NOT NULL",
                    (time.time(), client.service_id),
                )

        for call_id, (caller_id, target_id) in list(self._pending_calls.items()):
            if caller_id == client.service_id:
                self._pending_calls.pop(call_id, None)
            elif target_id == client.service_id:
                self._pending_calls.pop(call_id, None)
                async with self._clients_lock:
                    caller = self._clients.get(caller_id)
                if caller is not None and not caller.closed:
                    try:
                        await caller.send(
                            RpcResult(
                                id=call_id,
                                ok=False,
                                error=f"Service {client.service_id!r} disconnected during an RPC call",
                                error_type="SERVICE_UNAVAILABLE",
                            )
                        )
                    except Exception:
                        pass

        client.writer.close()
        try:
            await client.writer.wait_closed()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
