from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import defaultdict

from arc_service.types import ProcessOutcome, ServiceStatus
from arc.forge.runtime import RuntimeBootstrap, VirtualEnvManager
from arc.foundation.constants import (
    ARC_DIR,
    MAX_UNHEALTHY_SERVICE_CHECKS,
    WAIT_ON_DEPENDENCIES,
)
from arc.foundation.service import ServiceState
from arc.pulse.communication import CommunicationBroker
from arc.pulse.configLoader import ConfigLoader
from arc.pulse.manager import ServiceManager
from arc.pulse.registry import ServiceRegistry

logger = logging.getLogger(__name__)

CONTROL_SOCKET = ARC_DIR / "arc.sock"
COMMUNICATION_SOCKET = ARC_DIR / "communication.sock"
COMMUNICATION_DATABASE = ARC_DIR / "communication.sqlite3"
RESTART_ALWAYS = "always"
RESTART_ON_FAILURE = "on-failure"
RESTART_NEVER = "never"

HEALTH_ACTION_IGNORE = "ignore"
HEALTH_ACTION_RESTART = "restart"
HEALTH_ACTION_STOP = "stop"


class Pulse:
    def __init__(self, autofix: bool) -> None:
        self._autofix = autofix
        self._service_registry = ServiceRegistry()

        self._runtime = VirtualEnvManager()
        self._service_manager = ServiceManager(
            self._service_registry,
            runtime_python=self._runtime.python,
        )

        self._service_health_counter: defaultdict[int, int] = defaultdict(int)

        self._control_server: asyncio.AbstractServer | None = None
        self._communication_broker: CommunicationBroker | None = None
        self._previous_communication_socket = os.environ.get("ARC_COMMUNICATION_SOCKET")

    async def startup(
        self,
        wait_on_deps: bool = WAIT_ON_DEPENDENCIES,
    ) -> None:
        RuntimeBootstrap(self._runtime).ensure(self._autofix)
        tree = ConfigLoader(self._autofix).run()
        self._service_registry.register_tree(tree)

        await self._start_control_server()
        try:
            await self._start_communication_broker()
            await self._service_manager.start_all(wait_on_deps)
        except BaseException:
            await self.shutdown()
            raise

    async def _start_communication_broker(self) -> None:
        broker = CommunicationBroker(
            socket_path=COMMUNICATION_SOCKET,
            database_path=COMMUNICATION_DATABASE,
        )
        await broker.start()
        self._communication_broker = broker
        # ServiceProcess uses os.execve(..., os.environ.copy()), so every service
        # runner inherits the broker path without changing Pulse's control IPC.
        os.environ["ARC_COMMUNICATION_SOCKET"] = str(COMMUNICATION_SOCKET)

    async def _start_control_server(self) -> None:
        CONTROL_SOCKET.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        if CONTROL_SOCKET.exists():
            CONTROL_SOCKET.unlink()
        self._control_server = await asyncio.start_unix_server(
            self._handle_control_client,
            path=str(CONTROL_SOCKET),
        )

        os.chmod(CONTROL_SOCKET, 0o600)

        logger.info(
            "Pulse control socket listening at %s",
            CONTROL_SOCKET,
        )

    async def _handle_control_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            data = await reader.readline()
            if not data:
                return

            request = json.loads(data)
            command = request.get("command")

            if command == "status":
                response = self._status_response()
            elif command == "shutdown":
                response = {
                    "ok": True,
                    "message": "Shutdown requested",
                }
                writer.write((json.dumps(response) + "\n").encode())
                await writer.drain()
                asyncio.create_task(self.shutdown())
                return
            else:
                response = {
                    "ok": False,
                    "error": f"Unknown command: {command}",
                }

            writer.write((json.dumps(response) + "\n").encode())
            await writer.drain()

        except Exception as exc:
            logger.exception("Control request failed")
            response = {
                "ok": False,
                "error": str(exc),
            }
            try:
                writer.write((json.dumps(response) + "\n").encode())
                await writer.drain()
            except (
                BrokenPipeError,
                ConnectionResetError,
            ):
                pass

        finally:
            writer.close()
            await writer.wait_closed()

    def _status_response(self):
        services = []
        for service in self._service_registry.iter_startup_order():
            process = service.process
            services.append(
                {
                    "id": service.config.name,
                    "state": service.state.value,
                    "pid": service.pid,
                    "running": (process is not None and process.is_alive()),
                    "started_at": (
                        service.started_at.isoformat()
                        if service.started_at is not None
                        else None
                    ),
                    "restart_count": service.restart_count,
                    "last_error": service.last_error,
                }
            )
        return {
            "ok": True,
            "services": services,
        }

    async def supervise(
        self,
        poll_interval: float = 2.0,
    ) -> None:
        while True:
            for service in self._service_registry.iter_startup_order():
                if service.state not in (
                    ServiceState.RUNNING,
                    ServiceState.FAILED,
                ):
                    continue

                outcome = self._service_manager.check(service)
                if outcome is not None:
                    logger.info(
                        "Service '%s' exited: %s",
                        service.config.name,
                        outcome.value,
                    )
                    if service.pid is not None:
                        self._service_health_counter.pop(service.pid, None)
                    if self._should_restart(service.config.restart, outcome):
                        await self._service_manager.restart(service)
                    continue

                process = service.process
                if process is None or service.pid is None:
                    continue

                status: ServiceStatus | None = await process.status()
                if status is None:
                    logger.warning(
                        "Could not determine health for service '%s'",
                        service.config.name,
                    )
                    continue

                pid = service.pid
                if status.healthy:
                    self._service_health_counter[pid] = 0
                    continue

                self._service_health_counter[pid] += 1
                if self._service_health_counter[pid] < MAX_UNHEALTHY_SERVICE_CHECKS:
                    continue

                self._service_health_counter[pid] = 0
                logger.warning(
                    "Service '%s' reports unhealthy: %s",
                    service.config.name,
                    status.healthy_reason,
                )

                if service.config.health == HEALTH_ACTION_RESTART:
                    await self._service_manager.restart(service)
                elif service.config.health == HEALTH_ACTION_STOP:
                    await self._service_manager.stop(service)

            await asyncio.sleep(poll_interval)

    @staticmethod
    def _should_restart(
        policy: str,
        outcome: ProcessOutcome,
    ) -> bool:
        if policy == RESTART_ALWAYS:
            return True
        if policy == RESTART_ON_FAILURE:
            return outcome is ProcessOutcome.CRASHED
        return False

    async def shutdown(self) -> None:
        # Stop producers/consumers before closing their broker. Unacknowledged
        # events remain persisted and are retried after the next Pulse startup.
        await self._service_manager.stop_all()

        if self._communication_broker is not None:
            await self._communication_broker.close()
            self._communication_broker = None

        if self._control_server is not None:
            self._control_server.close()
            await self._control_server.wait_closed()
            self._control_server = None

        if CONTROL_SOCKET.exists():
            CONTROL_SOCKET.unlink(missing_ok=True)

        if self._previous_communication_socket is None:
            os.environ.pop("ARC_COMMUNICATION_SOCKET", None)
        else:
            os.environ["ARC_COMMUNICATION_SOCKET"] = self._previous_communication_socket
