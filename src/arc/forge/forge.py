from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path
from typing import Any

import yaml

from arc.forge.lock_manager import LockFile
from arc.forge.runtime import RuntimeBootstrap, VirtualEnvManager
from arc.forge.types import SERVICE_REGISTRY, ResolvedSource, ServiceSpec
from arc.foundation.constants import (
    SERVICE_CONFIG,
    SERVICE_LOCK,
    SERVICE_RUNTIME_DIR,
)
from arc.foundation.system_dependencies import SystemDependencyChecker

logger = logging.getLogger(__name__)


class ForgeError(RuntimeError):
    """Base exception for Forge errors."""


class ServiceNotFoundError(ForgeError):
    pass


class ServiceAlreadyInstalledError(ForgeError):
    pass


class InvalidServiceError(ForgeError):
    pass


class InstallationError(ForgeError):
    pass


class Forge:
    def __init__(
        self,
        runtime: VirtualEnvManager,
        lock: LockFile,
        services_config: Path,
        autofix: bool = False,
    ) -> None:
        self.runtime: VirtualEnvManager = runtime
        self.lock: LockFile = lock
        self.services_config: Path = services_config
        self._autofix: bool = autofix

        self.bootstrap: RuntimeBootstrap = RuntimeBootstrap(runtime)
        self.system_dependencies = SystemDependencyChecker(runtime.python)

    def list(self) -> list[ServiceSpec]:
        data = self.lock.load()
        services: list[ServiceSpec] = []

        for package in data.get("package", []):  # pyright: ignore[reportUnknownMemberType, reportGeneralTypeIssues]
            if not isinstance(package, dict):
                continue

            service = package.get("service")
            if not isinstance(service, dict):
                continue

            try:
                services.append(
                    ServiceSpec(
                        id=service["id"],
                        name=service.get("name", package["name"]),
                        version=package["version"],
                        package=package["name"],
                        description=service.get("description", ""),
                        module=service["module"],
                        restart=service.get("restart", "never"),
                        health=service.get("health", "ignore"),
                        depends=service.get("depends", []),
                        system_dependencies=service.get("system_dependencies", []),
                    )
                )
            except (KeyError, TypeError) as exc:
                logger.warning(
                    "Ignoring invalid service lock entry: %r (%s)",
                    package,
                    exc,
                )

        return sorted(services, key=lambda spec: spec.id)

    @classmethod
    def from_env(cls, autofix: bool = False):
        runtime = VirtualEnvManager(SERVICE_RUNTIME_DIR)
        lock = LockFile(SERVICE_LOCK)
        return cls(runtime, lock, SERVICE_CONFIG, autofix)

    @staticmethod
    def _resolve_source(source: str | Path) -> ResolvedSource:
        value = str(source)
        path = Path(value).expanduser()

        if path.exists():
            if not path.is_dir():
                raise ForgeError(f"Local source exists but is not a directory: {path}")
            return ResolvedSource(
                kind="local",
                location=str(path.resolve()),
                path=path.resolve(),
            )

        if value.startswith(("https://", "http://", "git@")):
            return ResolvedSource(kind="git", location=value)

        return ResolvedSource(kind="registry", location=value)

    @staticmethod
    def _checkout_git(url: str) -> Path:
        if shutil.which("git") is None:
            raise ForgeError(
                "Git is required to install Git sources but was not found."
            )

        temporary = Path(tempfile.mkdtemp(prefix="arc-forge-"))
        try:
            subprocess.run(
                ["git", "clone", "--", url, str(temporary)],
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            shutil.rmtree(temporary, ignore_errors=True)
            raise ForgeError(f"Failed to clone repository: {url}") from exc
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

        return temporary

    def _resolve_project(self, source: ResolvedSource) -> ResolvedSource:
        if source.kind == "local":
            assert source.path is not None
            return source

        if source.kind == "registry":
            url = SERVICE_REGISTRY.get(source.location)
            if url is None:
                raise ForgeError(f"Service '{source.location}' is not registered.")
            source = source.model_copy(
                update={"kind": "git", "location": url}
            )

        if source.kind == "git":
            source.path = self._checkout_git(source.location)
            return source

        raise ForgeError(f"Unsupported source type: {source.kind}")

    @staticmethod
    def _read_service_spec(project_dir: Path) -> ServiceSpec:
        pyproject = project_dir / "pyproject.toml"
        if not pyproject.is_file():
            raise ForgeError(f"Missing pyproject.toml: {pyproject}")

        try:
            with pyproject.open("rb") as file:
                data = tomllib.load(file)
        except tomllib.TOMLDecodeError as exc:
            raise ForgeError(f"Invalid pyproject.toml: {pyproject}") from exc
        except OSError as exc:
            raise ForgeError(f"Could not read {pyproject}: {exc}") from exc

        project = data.get("project")
        if not isinstance(project, dict):
            raise ForgeError(f"Missing [project] section in {pyproject}")

        arc = data.get("tool", {}).get("arc", {}).get("service")
        if not isinstance(arc, dict):
            raise ForgeError(f"Missing [tool.arc.service] in {pyproject}")

        try:
            return ServiceSpec(
                id=arc["id"],
                name=arc.get("name", project["name"]),
                version=project["version"],
                package=project["name"],
                description=arc.get("description", project.get("description", "")),
                module=arc["module"],
                restart=arc.get("restart", "never"),
                health=arc.get("health", "ignore"),
                depends=arc.get("depends", []),
                system_dependencies=arc.get("system_dependencies", []),
            )
        except KeyError as exc:
            raise ForgeError(
                f"Missing required service metadata field: {exc.args[0]}"
            ) from exc

    def _resolve_spec(
        self,
        source: ResolvedSource,
    ) -> tuple[ResolvedSource, ServiceSpec]:
        resolved = self._resolve_project(source)
        if resolved.path is None:
            raise ForgeError("Source could not be resolved to a project directory.")

        spec = self._read_service_spec(resolved.path)
        self._validate_spec(spec)
        return resolved, spec

    @staticmethod
    def _validate_spec(spec: ServiceSpec) -> None:
        if not spec.id.strip():
            raise ForgeError("Service ID cannot be empty.")
        if spec.id in spec.depends:
            raise ForgeError(f"Service '{spec.id}' cannot depend on itself.")
        if len(set(spec.depends)) != len(spec.depends):
            raise ForgeError(f"Service '{spec.id}' contains duplicate dependencies.")
        if spec.restart not in {"always", "on-failure", "never"}:
            raise ForgeError(f"Invalid restart policy for '{spec.id}': {spec.restart}")
        if spec.health not in {"ignore", "restart", "stop"}:
            raise ForgeError(f"Invalid health policy for '{spec.id}': {spec.health}")

    @staticmethod
    def _install_target(source: ResolvedSource) -> str:
        if source.path is None:
            raise ForgeError("No local project path available for installation.")
        return str(source.path)

    def _load_services_config(self) -> dict[str, Any]:
        if not self.services_config.exists():
            logger.warning(
                "Expected 'services.arc.yaml' file at: %s, but it does not exist",
                self.services_config,
            )
            return {"services": {}}

        try:
            with self.services_config.open("r", encoding="utf-8") as file:
                data = yaml.safe_load(file)
        except yaml.YAMLError as exc:
            raise ForgeError(
                f"Invalid services configuration: {self.services_config}"
            ) from exc
        except OSError as exc:
            raise ForgeError(f"Could not read services configuration: {exc}") from exc

        if data is None:
            return {"services": {}}
        if not isinstance(data, dict):
            raise ForgeError("services.arc.yaml must contain a mapping.")
        if data.get("services") is None:
            data["services"] = {}
        if not isinstance(data["services"], dict):
            raise ForgeError("'services' must be a mapping.")

        return data

    def _register_service(self, spec: ServiceSpec) -> None:
        data = self._load_services_config()
        services = data["services"]
        existing = services.get(spec.id)

        if existing is not None:
            if self._autofix:
                self._remove_service_config(spec.id)
                logger.warning(
                    "Removing old service config entry for service: %s | %s. Due to autofix.",
                    spec.name,
                    spec.id,
                )
            else:
                raise ForgeError(
                    f"Service '{spec.id}' is already registered. To override run with --autofix"
                )

        services[spec.id] = {
            "package": spec.package,
            "module": spec.module,
            "restart": spec.restart,
            "health": spec.health,
            "depends": spec.depends,
            "system_dependencies": [
                dependency.model_dump()
                for dependency in spec.system_dependencies
            ],
        }

        self.services_config.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.services_config.with_suffix(".yaml.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as file:
                yaml.safe_dump(data, file, sort_keys=False)
            temporary.replace(self.services_config)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise ForgeError(f"Could not write service configuration: {exc}") from exc

    def _remove_service_config(self, service_id: str) -> None:
        data = self._load_services_config()
        services = data["services"]
        if service_id not in services:
            return

        del services[service_id]
        self.services_config.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.services_config.with_suffix(".yaml.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as file:
                yaml.safe_dump(data, file, sort_keys=False)
            temporary.replace(self.services_config)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise ForgeError(f"Could not update service configuration: {exc}") from exc

    @staticmethod
    def _lock_record(
        spec: ServiceSpec,
        source: ResolvedSource,
        *,
        editable: bool,
    ):
        if source.kind == "local":
            source_data = {
                "type": "path",
                "path": source.location,
                "editable": editable,
            }
        else:
            source_data = {
                "type": source.kind,
                "url": source.location,
            }

        return {
            "name": spec.package,
            "version": spec.version,
            "source": source_data,
            "service": {
                "id": spec.id,
                "name": spec.name,
                "description": spec.description,
                "module": spec.module,
                "restart": spec.restart,
                "health": spec.health,
                "depends": spec.depends,
                "system_dependencies": [
                    dependency.model_dump()
                    for dependency in spec.system_dependencies
                ],
            },
        }

    def _find_locked_service(self, service_id: str):
        data = self.lock.load()
        for package in data.get("package", []):  # pyright: ignore[reportGeneralTypeIssues]
            service = package.get("service")
            if isinstance(service, dict) and service.get("id") == service_id:
                return package
        return None

    def install(
        self,
        source: str | Path,
        *,
        editable: bool = False,
    ) -> ServiceSpec:
        self.bootstrap.ensure(self._autofix)
        resolved_source = self._resolve_source(source)
        resolved_source, spec = self._resolve_spec(resolved_source)
        install_target = self._install_target(resolved_source)

        try:
            if resolved_source.kind == "git" and editable:
                self.runtime.install_git(
                    resolved_source.location,
                    editable=True,
                )
            else:
                self.runtime.install(
                    install_target,
                    editable=editable,
                )

            report = self.system_dependencies.check(
                package=spec.package,
                module=spec.module,
                declared=spec.system_dependencies,
            )
            if report.has_findings:
                logger.warning(
                    "%s",
                    report.format(service_name=spec.id, stage="install",color="yellow"),
                )

            self._register_service(spec)
            self.lock.upsert(
                self._lock_record(
                    spec,
                    resolved_source,
                    editable=editable,
                )
            )

        finally:
            if resolved_source.kind == "git" and resolved_source.path is not None:
                shutil.rmtree(resolved_source.path, ignore_errors=True)

        return spec

    def remove(self, service_id: str) -> None:
        record = self._find_locked_service(service_id)
        if record is None:
            raise KeyError(f"Unknown ARC service: {service_id}")

        package_name = record.get("name")
        if not package_name:
            raise ForgeError(f"Invalid lock record for service '{service_id}'.")

        self.runtime.remove(package_name)
        self._remove_service_config(service_id)
        self.lock.remove(package_name)

    def info(self, service_id: str) -> ServiceSpec:
        for spec in self.list():
            if spec.id == service_id:
                return spec
        raise ServiceNotFoundError(f"Service '{service_id}' is not installed.")
