from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path
from typing import Any

import yaml
import tomli_w

from arc.forge.lock_manager import LockFile
from arc.forge.runtime import (
    SERVICE_RUNNER_PACKAGE,
    RuntimeBootstrap,
    VirtualEnvManager,
)
from arc.forge.types import (
    OFFICIAL_COMPONENT_REGISTRY,
    ResolvedSource,
    ServiceSpec,
)
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

        for package in data.get("package", []):
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

    def list_native(self) -> list[tuple[str, str, ServiceSpec | None]]:
        """List official ARC services and their installed state.

        A service is considered installed when Forge has a corresponding
        service record in the ARC lock file. This distinguishes managed ARC
        services from unrelated distributions that happen to be present in
        the shared Python environment.
        """
        installed = {spec.id: spec for spec in self.list()}
        return [
            (service_id, repository, installed.get(service_id))
            for service_id, repository in sorted(OFFICIAL_COMPONENT_REGISTRY.items())
        ]

    @classmethod
    def from_env(cls, autofix: bool = False) -> Forge:
        runtime = VirtualEnvManager(SERVICE_RUNTIME_DIR)
        lock = LockFile(SERVICE_LOCK)
        return cls(runtime, lock, SERVICE_CONFIG, autofix)

    @staticmethod
    def _resolve_source(source: str | Path) -> ResolvedSource:
        value = str(source).strip()
        path = Path(value).expanduser()

        if path.exists():
            if not path.is_dir():
                raise ForgeError(f"Local source exists but is not a directory: {path}")
            return ResolvedSource(
                kind="local",
                location=str(path.resolve()),
                path=path.resolve(),
            )

        # Accept uv-style VCS prefixes as well as ordinary Git URLs.
        git_value = value[4:] if value.startswith("git+") else value
        if git_value.startswith(("https://", "http://", "ssh://", "git@")):
            return ResolvedSource(kind="git", location=git_value)

        if not value:
            raise ForgeError("A package name, Git URL, or local project path is required.")

        return ResolvedSource(kind="registry", location=value)

    @staticmethod
    def _checkout_git(url: str) -> Path:
        if shutil.which("git") is None:
            raise ForgeError("Git is required to install Git sources but was not found.")

        temporary = Path(tempfile.mkdtemp(prefix="arc-forge-"))
        try:
            subprocess.run(
                ["git", "clone", "--", url, str(temporary)],
                check=True,
                text=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as exc:
            shutil.rmtree(temporary, ignore_errors=True)
            details = (exc.stderr or exc.stdout or "").strip()
            raise ForgeError(
                f"Failed to clone repository: {url}"
                + (f"\n{details}" if details else "")
            ) from exc
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

        return temporary

    def _resolve_project(self, source: ResolvedSource) -> ResolvedSource:
        if source.kind == "local":
            if source.path is None:
                raise ForgeError("Local source has no project path.")
            return source

        if source.kind == "registry":
            url = OFFICIAL_COMPONENT_REGISTRY.get(source.location)
            if url is None:
                available = ", ".join(sorted(OFFICIAL_COMPONENT_REGISTRY))
                raise ForgeError(
                    f"Unknown official ARC component '{source.location}'. "
                    f"Available names: {available}. You can also pass a Git URL or local path."
                )
            source = source.model_copy(update={"kind": "git", "location": url})

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

        tool = data.get("tool", {})
        arc = tool.get("arc", {}).get("service") if isinstance(tool, dict) else None
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
        try:
            if resolved.path is None:
                raise ForgeError("Source could not be resolved to a project directory.")

            spec = self._read_service_spec(resolved.path)
            self._validate_spec(spec)
            return resolved, spec
        except Exception:
            # A metadata or validation error must not leave a temporary clone behind.
            if resolved.kind == "git" and resolved.path is not None:
                shutil.rmtree(resolved.path, ignore_errors=True)
            raise

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
    def _source_from_lock(record: dict[str, Any]) -> ResolvedSource:
        source = record.get("source")
        if not isinstance(source, dict):
            raise ForgeError(f"Package '{record.get('name', '<unknown>')}' has no source record.")

        source_type = source.get("type")
        if source_type == "path":
            location = source.get("path")
        elif source_type == "git":
            location = source.get("url")
        elif source_type == "registry":
            location = source.get("name") or source.get("location") or source.get("url")
        else:
            raise ForgeError(
                f"Unsupported source type '{source_type}' in lock entry "
                f"'{record.get('name', '<unknown>')}'."
            )

        if not isinstance(location, str) or not location:
            raise ForgeError(
                f"Invalid source location in lock entry '{record.get('name', '<unknown>')}'."
            )

        return Forge._resolve_source(location)

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

    def _write_services_config(self, data: dict[str, Any]) -> None:
        self.services_config.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.services_config.with_suffix(".yaml.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as file:
                yaml.safe_dump(data, file, sort_keys=False)
            temporary.replace(self.services_config)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise ForgeError(f"Could not write service configuration: {exc}") from exc

    @staticmethod
    def _service_config_entry(spec: ServiceSpec) -> dict[str, Any]:
        return {
            "package": spec.package,
            "module": spec.module,
            "restart": spec.restart,
            "health": spec.health,
            "depends": spec.depends,
            "system_dependencies": [
                dependency.model_dump() for dependency in spec.system_dependencies
            ],
        }

    def _register_service(self, spec: ServiceSpec) -> None:
        data = self._load_services_config()
        services = data["services"]
        existing = services.get(spec.id)

        if existing is not None:
            if self._autofix:
                logger.warning(
                    "Replacing existing service config entry for %s (%s) due to autofix.",
                    spec.name,
                    spec.id,
                )
            else:
                raise ServiceAlreadyInstalledError(
                    f"Service '{spec.id}' is already registered. To override run with --autofix"
                )

        services[spec.id] = self._service_config_entry(spec)
        self._write_services_config(data)

    def _sync_services_config(self, specs: list[ServiceSpec]) -> None:
        """Refresh the service metadata for all upgraded services in one write."""
        data = self._load_services_config()
        services = data["services"]
        for spec in specs:
            services[spec.id] = self._service_config_entry(spec)
        self._write_services_config(data)

    def _remove_service_config(self, service_id: str) -> None:
        data = self._load_services_config()
        services = data["services"]
        if service_id not in services:
            return

        del services[service_id]
        self._write_services_config(data)

    @staticmethod
    def _lock_record(
        spec: ServiceSpec,
        source: ResolvedSource,
    ) -> dict[str, Any]:
        if source.kind == "local":
            source_data = {
                "type": "path",
                "path": source.location,
            }
        elif source.kind == "git":
            source_data = {
                "type": "git",
                "url": source.location,
            }
        else:
            raise ForgeError(f"Cannot lock unresolved source type: {source.kind}")

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

    def _find_locked_service(self, service_id: str) -> dict[str, Any] | None:
        data = self.lock.load()
        for package in data.get("package", []):
            if not isinstance(package, dict):
                continue
            service = package.get("service")
            if isinstance(service, dict) and service.get("id") == service_id:
                return package
        return None

    @staticmethod
    def _merge_uv_source(
        sources: dict[str, Any],
        package_name: str,
        source: Any,
    ) -> None:
        existing = sources.get(package_name)
        if existing is None or existing == source:
            sources[package_name] = source
            return

        # uv permits platform-specific source lists. Merge identical-compatible
        # entries while refusing ambiguous mappings for the same dependency.
        if isinstance(existing, list) and isinstance(source, list):
            merged = list(existing)
            for item in source:
                if item not in merged:
                    merged.append(item)
            sources[package_name] = merged
            return

        raise ForgeError(
            f"Conflicting uv source definitions for dependency '{package_name}'. "
            "Align the ARC component pyproject.toml files before updating."
        )

    @staticmethod
    def _build_update_project(
        projects: list[tuple[ResolvedSource, ServiceSpec]],
        project_dir: Path,
        runner_requirement: str,
    ) -> Path:
        """Create a temporary uv project representing the complete service set."""
        dependencies: list[str] = []
        sources: dict[str, Any] = {}
        indexes: dict[str, dict[str, Any]] = {}

        for resolved, spec in projects:
            if resolved.path is None:
                raise ForgeError(f"Missing checkout path for service '{spec.id}'.")

            pyproject = resolved.path / "pyproject.toml"
            try:
                with pyproject.open("rb") as file:
                    data = tomllib.load(file)
            except OSError as exc:
                raise ForgeError(f"Could not read {pyproject}: {exc}") from exc
            except tomllib.TOMLDecodeError as exc:
                raise ForgeError(f"Invalid pyproject.toml: {pyproject}") from exc

            tool = data.get("tool", {})
            uv_config = tool.get("uv", {}) if isinstance(tool, dict) else {}
            if not isinstance(uv_config, dict):
                raise ForgeError(f"Invalid [tool.uv] configuration in {pyproject}")

            project_sources = uv_config.get("sources", {})
            if not isinstance(project_sources, dict):
                raise ForgeError(f"Invalid [tool.uv.sources] table in {pyproject}")

            for dependency, source in project_sources.items():
                if dependency == spec.package:
                    continue
                Forge._merge_uv_source(sources, dependency, source)

            project_indexes = uv_config.get("index", [])
            if isinstance(project_indexes, dict):
                project_indexes = [project_indexes]
            if not isinstance(project_indexes, list):
                raise ForgeError(f"Invalid [[tool.uv.index]] entries in {pyproject}")

            for index in project_indexes:
                if not isinstance(index, dict) or not isinstance(index.get("name"), str):
                    raise ForgeError(f"Invalid named uv index in {pyproject}")
                index_name = index["name"]
                existing_index = indexes.get(index_name)
                if existing_index is not None and existing_index != index:
                    raise ForgeError(
                        f"Conflicting definitions for uv index '{index_name}' "
                        "across ARC services."
                    )
                indexes[index_name] = index

            dependencies.append(f"{spec.package}=={spec.version}")
            Forge._merge_uv_source(
                sources,
                spec.package,
                {"path": str(resolved.path.resolve())},
            )

        # ARC Core and its service runner share a release/API contract. Pin the
        # runner to the version provided by this Core installation, overriding
        # an unpinned branch source declared by an individual service.
        if not runner_requirement.startswith("git+") or "@" not in runner_requirement:
            raise ForgeError("Invalid ARC service-runner source requirement.")
        runner_url, runner_ref = runner_requirement[4:].rsplit("@", 1)
        if not runner_ref.startswith("v"):
            raise ForgeError("ARC service-runner source must use a version tag.")
        runner_version = runner_ref[1:]
        dependencies.append(f"{SERVICE_RUNNER_PACKAGE}=={runner_version}")
        sources[SERVICE_RUNNER_PACKAGE] = {
            "git": runner_url,
            "tag": runner_ref,
        }

        manifest: dict[str, Any] = {
            "project": {
                "name": "arc-forge-update-set",
                "version": "0.0.0",
                "requires-python": ">=3.11",
                "dependencies": dependencies,
            },
            "tool": {
                "uv": {
                    "package": False,
                    "sources": sources,
                }
            },
        }
        if indexes:
            manifest["tool"]["uv"]["index"] = list(indexes.values())

        project_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = project_dir / "pyproject.toml"
        try:
            manifest_path.write_text(tomli_w.dumps(manifest), encoding="utf-8")
        except OSError as exc:
            raise ForgeError(f"Could not write temporary update manifest: {exc}") from exc

        return manifest_path

    def install(self, source: str | Path) -> ServiceSpec:
        self.bootstrap.ensure(self._autofix)
        resolved_source = self._resolve_source(source)
        resolved_source, spec = self._resolve_spec(resolved_source)
        aggregate_dir = Path(tempfile.mkdtemp(prefix="arc-forge-install-set-"))

        try:
            if resolved_source.path is None:
                raise ForgeError("Resolved service has no project directory.")

            # Resolve the service and its dependencies using its own uv source and
            # index configuration, while pinning service-runner to this Core API.
            self._build_update_project(
                [(resolved_source, spec)],
                aggregate_dir,
                self.bootstrap.runner_requirement(),
            )
            self.runtime.sync_project(aggregate_dir)

            report = self.system_dependencies.check(
                package=spec.package,
                module=spec.module,
                declared=spec.system_dependencies,
            )
            if report.has_findings:
                logger.warning(
                    "%s",
                    report.format(service_name=spec.id, stage="install", color="yellow"),
                )

            self._register_service(spec)
            self.lock.upsert(self._lock_record(spec, resolved_source))

        finally:
            shutil.rmtree(aggregate_dir, ignore_errors=True)
            if resolved_source.kind == "git" and resolved_source.path is not None:
                shutil.rmtree(resolved_source.path, ignore_errors=True)

        return spec

    def update(self) -> list[tuple[str, str, str]]:
        """Update every locked ARC service as one dependency-resolution operation.

        Each source is inspected first so the new service metadata and package
        identity can be validated before changing the runtime environment. All
        project directories are then passed to uv together, allowing uv to
        resolve the complete declared dependency graph in one operation.
        """
        lock_data = self.lock.load()
        package_records = lock_data.get("package", [])
        if not package_records:
            return []
        if not isinstance(package_records, list):
            raise ForgeError("The ARC lock file contains an invalid package list.")

        self.bootstrap.ensure(self._autofix)

        resolved_sources: list[ResolvedSource] = []
        projects: list[tuple[ResolvedSource, ServiceSpec]] = []
        specs: list[ServiceSpec] = []
        updated_by_name: dict[str, dict[str, Any]] = {}
        summary: list[tuple[str, str, str]] = []
        aggregate_dir = Path(tempfile.mkdtemp(prefix="arc-forge-update-set-"))

        try:
            for record in package_records:
                if not isinstance(record, dict):
                    raise ForgeError("The ARC lock file contains an invalid package entry.")

                service_record = record.get("service")
                if not isinstance(service_record, dict):
                    raise ForgeError(
                        f"Cannot update '{record.get('name', '<unknown>')}' because "
                        "its lock entry does not contain ARC service metadata."
                    )

                package_name = record.get("name")
                if not isinstance(package_name, str) or not package_name:
                    raise ForgeError("An ARC lock entry is missing its package name.")

                source = self._source_from_lock(record)
                resolved, spec = self._resolve_spec(source)
                resolved_sources.append(resolved)

                if spec.package != package_name:
                    raise ForgeError(
                        f"The source for '{package_name}' now declares package "
                        f"'{spec.package}'. Refusing an unsafe in-place update."
                    )

                previous_service_id = service_record.get("id")
                if previous_service_id != spec.id:
                    raise ForgeError(
                        f"Service ID changed from '{previous_service_id}' to '{spec.id}' "
                        f"for package '{package_name}'. This requires an explicit migration."
                    )

                if package_name in updated_by_name:
                    raise ForgeError(f"Duplicate package in ARC lock file: {package_name}")

                projects.append((resolved, spec))
                specs.append(spec)
                updated_by_name[package_name] = self._lock_record(spec, resolved)
                summary.append((spec.id, str(record.get("version", "unknown")), spec.version))

            # Keep the service runner aligned with the API version bundled with
            # the currently installed ARC Core while the service graph is solved.
            self._build_update_project(
                projects,
                aggregate_dir,
                self.bootstrap.runner_requirement(),
            )
            self.runtime.sync_project(aggregate_dir, upgrade=True)

            runner_version = self.bootstrap.runner_requirement().rsplit("@v", 1)[-1]
            installed_runner_version = self.runtime.installed_version(SERVICE_RUNNER_PACKAGE)
            if installed_runner_version != runner_version:
                raise ForgeError(
                    "The ARC service-runner version does not match this Core release: "
                    f"expected '{runner_version}', got '{installed_runner_version or 'not installed'}'."
                )

            for spec in specs:
                installed_version = self.runtime.installed_version(spec.package)
                if installed_version != spec.version:
                    raise ForgeError(
                        f"Updated package '{spec.package}' reports version "
                        f"'{installed_version or 'not installed'}', expected '{spec.version}'."
                    )

                report = self.system_dependencies.check(
                    package=spec.package,
                    module=spec.module,
                    declared=spec.system_dependencies,
                )
                if report.has_findings:
                    logger.warning(
                        "%s",
                        report.format(service_name=spec.id, stage="update", color="yellow"),
                    )

            # Persist service metadata and lock state only after uv succeeds and
            # every updated distribution reports the version from its source.
            self._sync_services_config(specs)
            new_records = [
                updated_by_name.get(record.get("name"), record)
                for record in package_records
            ]
            self.lock.replace_packages(new_records)

            return summary

        finally:
            shutil.rmtree(aggregate_dir, ignore_errors=True)
            for source in resolved_sources:
                if source.kind == "git" and source.path is not None:
                    shutil.rmtree(source.path, ignore_errors=True)

    def remove(self, service_id: str) -> None:
        record = self._find_locked_service(service_id)
        if record is None:
            raise ServiceNotFoundError(f"Unknown ARC service: {service_id}")

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
    