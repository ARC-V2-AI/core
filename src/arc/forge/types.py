from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from arc.foundation.system_dependencies import SystemDependency

# unique-name : github url
SERVICE_REGISTRY: dict[str, str] = {
    "arc-v2-service-runner": "https://github.com/ARC-V2-AI/service-runner.git"
}


class ServiceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    version: str
    package: str
    description: str = ""

    module: str

    restart: str = "never"
    health: str = "ignore"

    depends: list[str] = Field(default_factory=list)
    system_dependencies: list[SystemDependency] = Field(default_factory=list)


class ResolvedSource(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)  # pyright: ignore[reportUnannotatedClassAttribute]

    kind: Literal["local", "git", "registry"]
    location: str
    ref: str | None = None
    path: Path | None = None
