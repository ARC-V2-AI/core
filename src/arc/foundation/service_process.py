from __future__ import annotations

import asyncio
import logging
import os
import signal
from dataclasses import dataclass, field
from datetime import UTC, datetime
from multiprocessing import get_context
from typing import Protocol

from arc_service.types import ProcessOutcome, ProcessResult, ServiceStatus

logger = logging.getLogger(__name__)

_CTX = get_context("fork")


class PipeLike(Protocol):
    def fileno(self) -> int: ...

    def close(self) -> None: ...

    def send_bytes(self, buf: bytes) -> None: ...

    def recv_bytes(self) -> bytes: ...

    def poll(self, timeout: float | None = None) -> bool: ...


@dataclass(slots=True)
class ServiceProcess:
    name: str
    module: str
    runtime_python: str

    started_at: datetime | None = field(default=None, init=False)

    _result_conn: PipeLike = field(init=False)
    _child_result_conn: PipeLike = field(init=False)

    _control_conn: PipeLike = field(init=False)
    _child_control_conn: PipeLike = field(init=False)

    _pid: int | None = field(default=None, init=False)
    _exitcode: int | None = field(default=None, init=False)

    _status_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        self._result_conn, self._child_result_conn = _CTX.Pipe(
            duplex=False,
        )

        self._control_conn, self._child_control_conn = _CTX.Pipe(
            duplex=True,
        )

    @property
    def pid(self) -> int | None:
        return self._pid

    @property
    def exitcode(self) -> int | None:
        self._reap()
        return self._exitcode

    def start(self) -> None:
        if self._pid is not None:
            raise RuntimeError(f"Service '{self.name}' is already running")

        # DO NOT use os.path.realpath() here.
        #
        # ~/arc/runtime/bin/python may be a symlink to the base
        # interpreter. Resolving it can cause the venv to be lost.
        runtime_python = self.runtime_python

        if not os.path.isfile(runtime_python):
            raise FileNotFoundError(f"Runtime Python not found: {runtime_python}")

        pid = os.fork()

        if pid == 0:
            self._exec_child(runtime_python)
            os._exit(127)

        self._pid = pid
        self.started_at = datetime.now(UTC)

        # Parent keeps:
        #   _control_conn
        #   _result_conn
        #
        # Parent does not need the child-side descriptors.
        self._child_control_conn.close()
        self._child_result_conn.close()

    def _exec_child(self, runtime_python: str) -> None:
        try:
            # Child keeps the child-side descriptors.
            self._control_conn.close()
            self._result_conn.close()

            control_fd = self._child_control_conn.fileno()
            result_fd = self._child_result_conn.fileno()

            os.set_inheritable(control_fd, True)
            os.set_inheritable(result_fd, True)

            argv = [
                runtime_python,
                "-m",
                "arc_service.runner",
                "--module",
                self.module,
                "--service-name",
                self.name,
                "--control-fd",
                str(control_fd),
                "--result-fd",
                str(result_fd),
            ]

            # Pulse owns service shutdown. Ignore terminal Ctrl+C in child
            # processes and let Pulse send the graceful IPC stop command.
            _ = signal.signal(signal.SIGINT, signal.SIG_IGN)

            os.execve(
                runtime_python,
                argv,
                os.environ.copy(),
            )

        except BaseException:
            os._exit(127)

    async def status(
        self,
        timeout: float = 2.0,
    ) -> ServiceStatus | None:
        async with self._status_lock:
            if not self.is_alive():
                return None

            try:
                self._control_conn.send_bytes(b"status")

                if not self._control_conn.poll(timeout):
                    logger.warning(
                        "Timed out querying service '%s'",
                        self.name,
                    )
                    return None

                return ServiceStatus.from_bytes(self._control_conn.recv_bytes())

            except (
                EOFError,
                BrokenPipeError,
                ConnectionResetError,
                OSError,
            ) as exc:
                logger.warning(
                    "Could not query service '%s': %s",
                    self.name,
                    exc,
                )
                return None

    def poll_result(self) -> ProcessResult | None:
        """
        Return a runner-reported result if one exists.

        If the Runner was externally killed, it cannot send a result.
        In that case waitpid()/the exit code is used to synthesize
        a ProcessResult.
        """
        self._reap()

        try:
            if self._result_conn.poll():
                return ProcessResult.from_bytes(self._result_conn.recv_bytes())

        except (
            EOFError,
            BrokenPipeError,
            ConnectionResetError,
            OSError,
        ):
            pass

        # Runner may have died before sending anything.
        if self._exitcode is None:
            return None

        if self._exitcode < 0:
            signal_number = -self._exitcode

            return ProcessResult(
                outcome=ProcessOutcome.CRASHED,
                error=(
                    f"Process terminated by signal "
                    f"{signal_number} ({signal.strsignal(signal_number)})"
                ),
            )

        if self._exitcode != 0:
            return ProcessResult(
                outcome=ProcessOutcome.CRASHED,
                error=(f"Process exited with code {self._exitcode}"),
            )

        return ProcessResult(
            outcome=ProcessOutcome.STOPPED,
        )

    def is_alive(self) -> bool:
        self._reap()

        return self._pid is not None and self._exitcode is None

    def _reap(self) -> None:
        if self._pid is None or self._exitcode is not None:
            return

        try:
            pid, status = os.waitpid(
                self._pid,
                os.WNOHANG,
            )
        except ChildProcessError:
            # The child has already been reaped somewhere else.
            self._exitcode = 1
            return

        if pid == 0:
            return

        if os.WIFEXITED(status):
            self._exitcode = os.WEXITSTATUS(status)
            return

        if os.WIFSIGNALED(status):
            self._exitcode = -os.WTERMSIG(status)
            return

        # Defensive fallback.
        self._exitcode = 1

    async def terminate(
        self,
        timeout: float = 5.0,
    ) -> None:
        if not self.is_alive():
            return

        try:
            self._control_conn.send_bytes(b"stop")
        except (
            BrokenPipeError,
            ConnectionResetError,
            OSError,
        ):
            pass

        deadline = asyncio.get_running_loop().time() + timeout

        while self.is_alive():
            if asyncio.get_running_loop().time() >= deadline:
                break

            await asyncio.sleep(0.1)

        if self.is_alive():
            self.kill()

    def kill(self) -> None:
        if not self.is_alive():
            return

        assert self._pid is not None

        try:
            os.kill(
                self._pid,
                signal.SIGKILL,
            )
        except ProcessLookupError:
            pass

        try:
            os.waitpid(
                self._pid,
                0,
            )
        except ChildProcessError:
            pass

        self._exitcode = -signal.SIGKILL

    def close(self) -> None:
        self._result_conn.close()
        self._control_conn.close()
