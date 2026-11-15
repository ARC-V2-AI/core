from __future__ import annotations

import os
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from logging import Logger
from typing import TYPE_CHECKING, TypeAlias

from arc.foundation.system_dependencies import SystemDependency

if TYPE_CHECKING:
    # Used to prevent an hard import cycle
    from arc.foundation.service_process import ServiceProcess


@dataclass
class ServiceConfig:
    name: str
    module: str
    restart: str = "never"
    health: str = "ignore"
    depends_on: list[str] = field(default_factory=list)
    package: str | None = None
    system_dependencies: list[SystemDependency] = field(default_factory=list)


ServiceNode: TypeAlias = tuple[str, ServiceConfig]
ServiceTree: TypeAlias = list[list[ServiceNode]]


class ServiceState(Enum):
    NONE = "none"
    REGISTERED = "registered"
    CREATED = "created"
    STARTING = "starting"
    READY = "ready"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True)
class ServiceInfo:
    name: str
    version: str
    description: str = ""


@dataclass(slots=True)
class BaseContext:
    logger: Logger
    env: Mapping[str, str] = field(default_factory=dict)
    service_name: str = ""
    process_name: str = ""


class Service(ABC):
    _info: ServiceInfo

    def __init__(
        self,
        name: str,
        version: str,
        description: str = "",
    ) -> None:
        self.ctx: BaseContext
        self._info = ServiceInfo(
            name=name,
            version=version,
            description=description,
        )

    def _load_context_env(self):
        """Load the ctx env vars into actual env."""
        self.ctx.logger.debug(
            f"Loading environment variables in {self.ctx.service_name}'s context..."
        )
        for key, value in self.ctx.env.items():
            _ = os.environ.setdefault(key, value)

    @property
    def info(self) -> ServiceInfo:
        return self._info

    async def start(self, ctx: BaseContext) -> None:
        self.ctx = ctx
        self._load_context_env()
        await self.run()

    @abstractmethod
    async def run(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def stop(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def healthy(self) -> tuple[bool, str | None]:
        """Return runtime health state."""
        raise NotImplementedError

    @abstractmethod
    async def ready(self) -> tuple[bool, str | None]:
        """Return initialization readiness state."""
        raise NotImplementedError


@dataclass
class ServiceInstance:
    config: ServiceConfig

    pid: int | None = None
    process: ServiceProcess | None = None

    state: ServiceState = ServiceState.NONE

    started_at: datetime | None = None

    restart_count: int = 0

    last_error: str | None = None

    @classmethod
    def from_config_registry(cls, config: ServiceConfig):
        """Create a ServiceInstance from the service configuration."""
        return cls(config, state=ServiceState.REGISTERED)
