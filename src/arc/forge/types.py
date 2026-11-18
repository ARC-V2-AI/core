from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from arc.foundation.system_dependencies import SystemDependency

# Official, user-installable ARC service repositories. Add new service slugs
# here when they are published by the ARC-V2-AI organization.
OFFICIAL_COMPONENT_REGISTRY: dict[str, str] = {
    "inference-runtime": "https://github.com/ARC-V2-AI/inference-runtime.git",
}

# Framework packages are managed internally and are not user-installable services.
SERVICE_RUNNER_REGISTRY: dict[str, str] = {
    "arc-v2-service-runner": "https://github.com/ARC-V2-AI/service-runner.git",
}

# Backwards-compatible name for existing internal imports.
SERVICE_REGISTRY = SERVICE_RUNNER_REGISTRY


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
