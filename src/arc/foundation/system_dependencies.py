from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal

from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from pydantic import BaseModel, ConfigDict, field_validator



class SystemDependency(BaseModel):
    """A system-level dependency declared by an ARC service."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["shared-library", "command"]
    name: str
    required: bool = True
    description: str = ""

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value or "\x00" in value:
            raise ValueError("System dependency name must be non-empty")
        return value


@dataclass(frozen=True, slots=True)
class MissingSystemDependency:
    name: str
    kind: str
    required: bool = True
    source: str | None = None
    description: str = ""


@dataclass(frozen=True, slots=True)
class SystemDependencyReport:
    package: str | None
    missing: tuple[MissingSystemDependency, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def missing_required(self) -> tuple[MissingSystemDependency, ...]:
        return tuple(item for item in self.missing if item.required)

    @property
    def missing_optional(self) -> tuple[MissingSystemDependency, ...]:
        return tuple(item for item in self.missing if not item.required)

    @property
    def ok(self) -> bool:
        return not self.missing_required

    @property
    def has_findings(self) -> bool:
        return bool(self.missing or self.warnings)

    def format(
        self,
        *,
        service_name: str | None = None,
        stage: Literal["install", "startup"] = "startup",
        color: Literal["red", "yellow", "green","none"] = "none",
    ) -> str:
        subject = f"ARC service '{service_name}'" if service_name else "ARC service"

        colors = {
    "red": "\033[31m",
    "yellow": "\033[33m",
    "green": "\033[32m",
        }
        reset = "\033[0m"

        header = f"{colors[color]} System dependency check for {subject}:{reset}" if color != "none" else f"System dependency check for {subject}"
        lines = [header]

        if self.missing:
            lines.append("Missing system dependencies:")

            for item in self.missing:
                level = "required" if item.required else "optional"
                lines.append(f"  - {item.name} ({level}, {item.kind})")

                if item.source:
                    lines.append(f"    Referenced by: {item.source}")

                if item.description:
                    lines.append(f"    {item.description}")

            if self.missing_required and stage == "install":
                lines.append(
                    "The package was installed, but the service may not start "
                    "until the required system dependencies are installed."
                )
            elif self.missing_required:
                lines.append(
                    "Service startup was blocked. Install the missing dependencies "
                    "and run ARC again."
                )
            else:
                lines.append(
                    "The service may start, but the listed optional capabilities "
                    "will be unavailable."
                )

        if self.warnings:
            lines.append("Dependency scan warnings:")
            lines.extend(f"  - {warning}" for warning in self.warnings)

        return "\n".join(lines)

class SystemDependencyError(RuntimeError):
    """Raised when required system dependencies prevent service startup."""

    def __init__(
        self,
        report: SystemDependencyReport,
        *,
        service_name: str,
    ) -> None:
        self.report = report
        super().__init__(report.format(service_name=service_name, stage="startup"))


class SystemDependencyChecker:
    """Checks declared requirements and ELF DT_NEEDED entries for a service."""

    _READELF_NEEDED = re.compile(
        r"\(NEEDED\).*Shared library: \[([^]]+)\]"
    )
    _READELF_PATH = re.compile(
        r"\((?:RPATH|RUNPATH)\).*Library (?:rpath|runpath): \[([^]]*)\]"
    )
    _OBJDUMP_NEEDED = re.compile(r"^\s*NEEDED\s+(\S+)")
    _OBJDUMP_PATH = re.compile(r"^\s*(?:RPATH|RUNPATH)\s+(\S+)")

    def __init__(self, runtime_python: str | Path) -> None:
        self.python = Path(runtime_python).expanduser()
        self._readelf = shutil.which("readelf")
        self._objdump = shutil.which("objdump")
        self._ldconfig = shutil.which("ldconfig")
        self._library_cache: set[str] | None = None

    def check(
        self,
        *,
        package: str | None = None,
        module: str | None = None,
        declared: Iterable[SystemDependency] = (),
    ) -> SystemDependencyReport:
        findings: dict[tuple[str, str], MissingSystemDependency] = {}
        warnings: list[str] = []

        def record(item: MissingSystemDependency) -> None:
            key = (item.kind, item.name)
            previous = findings.get(key)
            if previous is None:
                findings[key] = item
                return

            # A required declaration always takes precedence over an optional one.
            findings[key] = MissingSystemDependency(
                name=item.name,
                kind=item.kind,
                required=previous.required or item.required,
                source=previous.source or item.source,
                description=previous.description or item.description,
            )

        for dependency in declared:
            if dependency.kind == "command":
                available = shutil.which(dependency.name) is not None
            else:
                available = self._shared_library_available(dependency.name)

            if not available:
                record(
                    MissingSystemDependency(
                        name=dependency.name,
                        kind=dependency.kind,
                        required=dependency.required,
                        description=dependency.description,
                    )
                )

        shared_objects, distribution_warnings = self._distribution_shared_objects(
            package=package,
            module=module,
        )
        warnings.extend(distribution_warnings)

        if shared_objects and self._readelf is None and self._objdump is None:
            warnings.append(
                "Neither 'readelf' nor 'objdump' is available; linked "
                "shared-library dependencies could not be inspected."
            )

        for shared_object in shared_objects:
            missing, scan_warning = self._missing_shared_libraries(shared_object)
            for name in missing:
                record(
                    MissingSystemDependency(
                        name=name,
                        kind="shared-library",
                        required=True,
                        source=str(shared_object),
                    )
                )
            if scan_warning:
                warnings.append(scan_warning)

        return SystemDependencyReport(
            package=package,
            missing=tuple(sorted(findings.values(), key=lambda item: (not item.required, item.name))),
            warnings=tuple(dict.fromkeys(warnings)),
        )

    def _shared_library_available(
        self,
        name: str,
        *,
        origin: Path | None = None,
        search_paths: Iterable[str] = (),
    ) -> bool:
        if "/" in name and Path(name).expanduser().is_file():
            return True

        candidates: list[Path] = []
        if origin is not None:
            candidates.append(origin / name)

        for directory in os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep):
            if directory:
                candidates.append(Path(directory).expanduser() / name)

        if origin is not None:
            origin_value = str(origin)
            for entry in search_paths:
                expanded = entry.replace("${ORIGIN}", origin_value).replace(
                    "$ORIGIN", origin_value
                )
                if expanded:
                    candidates.append(Path(expanded).expanduser() / name)

        if any(candidate.is_file() for candidate in candidates):
            return True

        return name in self._system_library_cache()

    def _system_library_cache(self) -> set[str]:
        if self._library_cache is not None:
            return self._library_cache

        self._library_cache = set()
        if self._ldconfig is None:
            return self._library_cache

        try:
            result = subprocess.run(
                [self._ldconfig, "-p"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return self._library_cache

        if result.returncode == 0:
            for line in result.stdout.splitlines():
                fields = line.strip().split()
                if len(fields) >= 2 and "(" in fields[1]:
                    self._library_cache.add(fields[0])

        return self._library_cache

    @staticmethod
    def _active_requirement_names(
        requirements: Iterable[str],
        marker_environment: dict[str, str] | None = None,
    ) -> list[str]:
        # Evaluate PEP 508 markers for the service runtime, not necessarily
        # the Python interpreter that runs the ARC CLI. The empty extra
        # context excludes optional dependency groups.
        environment = default_environment()
        if marker_environment:
            environment.update(marker_environment)
        environment["extra"] = ""
        names: list[str] = []

        for raw_requirement in requirements:
            try:
                requirement = Requirement(raw_requirement)
            except InvalidRequirement:
                continue

            if requirement.marker is not None and not requirement.marker.evaluate(
                environment
            ):
                continue

            names.append(requirement.name)

        return list(dict.fromkeys(names))

    def _distribution_shared_objects(
        self,
        *,
        package: str | None,
        module: str | None,
    ) -> tuple[list[Path], tuple[str, ...]]:
        if not package and not module:
            return [], ()

        script = r'''
import importlib.metadata as metadata
import json
import os
import platform
import sys
from pathlib import Path

package, module = sys.argv[1], sys.argv[2]
if package:
    roots = [package]
elif module:
    roots = metadata.packages_distributions().get(module.split(".", 1)[0], [])
else:
    roots = []

distributions = []
for distribution in metadata.distributions():
    name = distribution.metadata.get("Name")
    if not name:
        continue

    files = set()
    for entry in distribution.files or ():
        path = Path(distribution.locate_file(entry))
        if ".so" in path.name and path.is_file():
            files.add(str(path))

    distributions.append({
        "name": name,
        "requires": list(distribution.requires or ()),
        "files": sorted(files),
    })

runtime_version = platform.python_version()
print(json.dumps({
    "roots": roots,
    "distributions": distributions,
    "marker_environment": {
        "implementation_name": sys.implementation.name,
        "implementation_version": runtime_version,
        "platform_python_implementation": platform.python_implementation(),
        "os_name": os.name,
        "platform_machine": platform.machine(),
        "platform_release": platform.release(),
        "platform_system": platform.system(),
        "platform_version": platform.version(),
        "python_full_version": runtime_version,
        "python_version": ".".join(runtime_version.split(".")[:2]),
        "sys_platform": sys.platform,
    },
}))
'''

        try:
            result = subprocess.run(
                [str(self.python), "-c", script, package or "", module or ""],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return [], (f"Could not inspect installed package files: {exc}",)

        if result.returncode != 0:
            detail = result.stderr.strip() or f"exit code {result.returncode}"
            return [], (f"Could not inspect installed package files: {detail}",)

        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return [], ("Could not read package file information from runtime Python.",)

        entries = payload.get("distributions", [])
        roots = payload.get("roots", [])
        marker_environment = payload.get("marker_environment", {})
        by_name = {
            canonicalize_name(entry["name"]): entry
            for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("name"), str)
        }

        pending = list(roots)
        visited: set[str] = set()
        shared_objects: set[str] = set()
        found: list[str] = []

        while pending:
            name = pending.pop()
            normalized = canonicalize_name(name)
            if normalized in visited:
                continue
            visited.add(normalized)

            distribution = by_name.get(normalized)
            if distribution is None:
                continue

            found.append(distribution["name"])
            shared_objects.update(distribution.get("files", []))
            pending.extend(
                self._active_requirement_names(
                    distribution.get("requires", []),
                    marker_environment,
                )
            )

        if not found:
            target = package or module or "service package"
            return [], (
                f"No installed Python distribution found for {target!r}; "
                "automatic native-library scan was skipped.",
            )

        return [Path(path) for path in sorted(shared_objects)], ()

    def _missing_shared_libraries(
        self,
        shared_object: Path,
    ) -> tuple[list[str], str | None]:
        try:
            with shared_object.open("rb") as file:
                if file.read(4) != b"\x7fELF":
                    return [], None
        except OSError as exc:
            return [], f"Could not read native library {shared_object}: {exc}"

        if self._readelf is not None:
            command = [self._readelf, "-d", str(shared_object)]
            parser = self._parse_readelf_output
        elif self._objdump is not None:
            command = [self._objdump, "-p", str(shared_object)]
            parser = self._parse_objdump_output
        else:
            return [], None

        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return [], f"Timed out inspecting native library: {shared_object}"
        except OSError as exc:
            return [], f"Could not inspect native library {shared_object}: {exc}"

        if result.returncode != 0:
            detail = result.stderr.strip() or f"exit code {result.returncode}"
            return [], f"Could not inspect native library {shared_object}: {detail}"

        needed, search_paths = parser(result.stdout)
        missing = [
            name
            for name in needed
            if not self._shared_library_available(
                name,
                origin=shared_object.parent,
                search_paths=search_paths,
            )
        ]
        return list(dict.fromkeys(missing)), None

    @classmethod
    def _parse_readelf_output(cls, output: str) -> tuple[list[str], list[str]]:
        needed: list[str] = []
        search_paths: list[str] = []
        for line in output.splitlines():
            needed_match = cls._READELF_NEEDED.search(line)
            if needed_match:
                needed.append(needed_match.group(1))
                continue

            path_match = cls._READELF_PATH.search(line)
            if path_match:
                search_paths.extend(
                    entry for entry in path_match.group(1).split(":") if entry
                )

        return list(dict.fromkeys(needed)), list(dict.fromkeys(search_paths))

    @classmethod
    def _parse_objdump_output(cls, output: str) -> tuple[list[str], list[str]]:
        needed: list[str] = []
        search_paths: list[str] = []
        for line in output.splitlines():
            needed_match = cls._OBJDUMP_NEEDED.match(line)
            if needed_match:
                needed.append(needed_match.group(1))
                continue

            path_match = cls._OBJDUMP_PATH.match(line)
            if path_match:
                search_paths.extend(path_match.group(1).split(":"))

        return list(dict.fromkeys(needed)), list(dict.fromkeys(search_paths))
