from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path

from arc_service.types import ProcessOutcome

from arc.foundation.service import ServiceInstance, ServiceState
from arc.foundation.service_process import ServiceProcess
from arc.foundation.system_dependencies import (
    SystemDependencyChecker,
    SystemDependencyError,
)
from arc.pulse.registry import ServiceRegistry

logger = logging.getLogger(__name__)


class ServiceManager:
    def __init__(
        self,
        registry: ServiceRegistry,
        runtime_python: Path,
    ) -> None:
        self._registry = registry
        self._runtime_python = runtime_python
        self._dependency_checker = SystemDependencyChecker(runtime_python)

    async def start(
        self,
        service: ServiceInstance,
    ) -> None:
        if service.process is not None and service.process.is_alive():
            raise RuntimeError(
                f"Service '{service.config.name}' is already running"
            )

        service.state = ServiceState.STARTING
        service.last_error = None

        try:
            report = self._dependency_checker.check(
                package=service.config.package,
                module=service.config.module,
                declared=service.config.system_dependencies,
            )

            if not report.ok:
                raise SystemDependencyError(
                    report,
                    service_name=service.config.name,
                )

            if report.has_findings:
                logger.warning(
                    "%s",
                    report.format(service_name=service.config.name),
                )

            process = ServiceProcess(
                name=service.config.name,
                module=service.config.module,
                runtime_python=str(self._runtime_python),
            )

            process.start()

            service.process = process
            service.pid = process.pid
            service.started_at = datetime.now(UTC)
            service.state = ServiceState.RUNNING

            logger.info(
                "Started service '%s' with PID %s",
                service.config.name,
                service.pid,
            )

        except SystemDependencyError as exc:
            service.state = ServiceState.FAILED
            service.last_error = str(exc)
            raise

        except Exception:
            service.state = ServiceState.FAILED
            logger.exception(
                "Failed to start service '%s'",
                service.config.name,
            )
            raise

    async def wait_ready(
        self,
        service: ServiceInstance,
        timeout: float = 30.0,
    ) -> None:
        if service.process is None:
            raise RuntimeError(f"Service '{service.config.name}' has no process")

        deadline = asyncio.get_running_loop().time() + timeout

        while True:
            if not service.process.is_alive():
                result = service.process.poll_result()
                service.state = ServiceState.FAILED

                if result is not None:
                    logger.error(
                        "Service '%s' failed before becoming ready: %s",
                        service.config.name,
                        result.error or result.outcome.value,
                    )
                else:
                    logger.error(
                        "Service '%s' failed before becoming ready",
                        service.config.name,
                    )

                raise RuntimeError(
                    f"Service '{service.config.name}' failed before becoming ready"
                )

            status = await service.process.status()
            if status is not None and status.ready:
                service.state = ServiceState.RUNNING
                logger.info("Service '%s' is ready", service.config.name)
                return

            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(
                    f"Service '{service.config.name}' "
                    f"did not become ready within {timeout} seconds"
                )

            await asyncio.sleep(0.1)

    async def start_all(
        self,
        wait_on_deps: bool,
    ) -> None:
        for level in self._registry.iter_levels():
            results = await asyncio.gather(
                *(self._start_and_wait(service, wait_on_deps) for service in level),
                return_exceptions=True,
            )

            for result in results:
                if isinstance(result, BaseException):
                    raise result

    async def _start_and_wait(
        self,
        service: ServiceInstance,
        wait_on_deps: bool,
    ) -> None:
        await self.start(service)
        if wait_on_deps:
            await self.wait_ready(service)

    def check(
        self,
        service: ServiceInstance,
    ) -> ProcessOutcome | None:
        process = service.process
        if process is None:
            return None

        result = process.poll_result()
        if result is None:
            return None

        if result.outcome is ProcessOutcome.STOPPED:
            service.state = ServiceState.STOPPED
        elif result.outcome is ProcessOutcome.CRASHED:
            service.state = ServiceState.FAILED
            service.last_error = result.traceback or result.error
            logger.error(
                "Service '%s' crashed: %s",
                service.config.name,
                result.error or "unknown error",
            )
            if result.traceback:
                logger.error(
                    "Service '%s' traceback:\n%s",
                    service.config.name,
                    result.traceback,
                )
        elif result.outcome is ProcessOutcome.CANCELLED:
            service.state = ServiceState.STOPPED

        process.close()
        service.process = None
        service.pid = None
        return result.outcome

    async def stop(
        self,
        service: ServiceInstance,
    ) -> None:
        process = service.process
        if process is None:
            return

        service.state = ServiceState.STOPPING
        try:
            await process.terminate()
        finally:
            process.close()
            service.process = None
            service.pid = None
            service.state = ServiceState.STOPPED

    async def restart(
        self,
        service: ServiceInstance,
    ) -> None:
        logger.info("Restarting service '%s'", service.config.name)
        process = service.process
        if process is not None:
            await process.terminate()
            process.close()

        service.process = None
        service.pid = None
        service.state = ServiceState.STOPPED

        await self.start(service)
        await self.wait_ready(service)

    async def stop_all(self) -> None:
        for service in reversed(list(self._registry.iter_startup_order())):
            try:
                await self.stop(service)
            except Exception:
                logger.exception(
                    "Failed to stop service '%s'",
                    service.config.name,
                )
