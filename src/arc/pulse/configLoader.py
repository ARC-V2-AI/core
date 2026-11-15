import logging
from pathlib import Path

import yaml

from arc.foundation.constants import SERVICE_CONFIG
from arc.foundation.service import ServiceConfig, ServiceNode, ServiceTree
from arc.foundation.system_dependencies import SystemDependency

logger = logging.getLogger(__name__)


class ConfigNotFoundError(FileNotFoundError):
    pass


class ConfigLoader:
    def __init__(self, autofix: bool) -> None:
        self._path: Path = SERVICE_CONFIG
        self._autofix: bool = autofix

        logger.debug(f"Config loader initialized with path: {self._path}")

    def locate(self):
        """Locate the services.arc.yaml file."""
        _path = self._path
        if not _path.exists():
            logger.error(f"Service configuration not found should be at: {_path}")

            if not self._autofix:
                raise ConfigNotFoundError(
                    "Could not find the 'services.arc.yaml' file.\n"
                    "This file is needed for system startup!"
                )

            logger.warning(
                f"Creating new empty 'services.arc.yaml' at: {_path}. Due to autofix."
            )

            _path.parent.mkdir(parents=True, exist_ok=True)
            _ = _path.write_text("services:\n\n", encoding="utf-8")

        return self._path

    def load(self) -> dict[str, ServiceConfig]:
        """Load the service configurations."""
        with self.locate().open("r", encoding="utf-8") as file:
            data = yaml.safe_load(file)

        services: dict[str, ServiceConfig] = {}

        if not data or data.get("services", {}) is None:
            logger.warning(
                "No data in service configuration! This leads to no running services."
            )
            return services

        for name, cfg in data.get("services", {}).items():
            services[name] = ServiceConfig(
                name=name,
                module=cfg["module"],
                restart=cfg.get("restart", "never"),
                health=cfg.get("health", "ignore"),
                depends_on=cfg.get("depends", []),
                package=cfg.get("package"),
                system_dependencies=[
                    SystemDependency.model_validate(item)
                    for item in (cfg.get("system_dependencies") or [])
                ],
            )

        return services

    @staticmethod
    def sort_to_tree(
        services: dict[str, ServiceConfig],
    ) -> ServiceTree:
        levels: ServiceTree = []

        remaining = set(services.keys())
        completed = set()  # pyright: ignore[reportUnknownVariableType]

        while remaining:
            current_level: list[ServiceNode] = []

            for name in remaining:
                service = services[name]
                if all(dep in completed for dep in service.depends_on):
                    current_level.append((name, service))

            if not current_level:
                raise RuntimeError("Circular dependency detected or missing dependency")

            levels.append(sorted(current_level, key=lambda item: item[0]))
            completed.update(name for name, _ in current_level)  # pyright: ignore[reportUnknownMemberType]
            remaining -= {name for name, _ in current_level}

        logger.debug(
            "Startup order: %s",
            [[name for name, _ in level] for level in levels],
        )

        return levels

    def run(self) -> ServiceTree:
        """Fetch the service tree from the service configuration."""
        logger.info("Building service startup tree")
        services = self.load()
        service_tree = self.sort_to_tree(services)
        logger.info("Service startup tree ready")
        return service_tree
