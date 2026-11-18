from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from collections import deque
from importlib.metadata import version
from pathlib import Path

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.text import Text

from arc.forge.types import SERVICE_RUNNER_REGISTRY
from arc.foundation.constants import SERVICE_RUNTIME_DIR

logger = logging.getLogger(__name__)
console = Console()


_BUILD_MARKERS = (
    "building wheel",
    "building editable",
    "running build",
    "build_ext",
    "cmake",
    "ninja",
    "meson",
    "compiling",
    "gcc",
    "g++",
    "clang",
    "c++",
    "cxx",
    "linking",
)

_NOISE_MARKERS = (
    "cache hit",
    "cache miss",
    "cache entry",
    "cache directory",
    "using cached",
    "cached at",
)


def _is_debug_output(line: str) -> bool:
    """Hide routine resolver/cache diagnostics from the interactive output."""
    normalized = line.strip().lower()

    return (
        normalized.startswith(("debug ", "trace "))
        or any(marker in normalized for marker in _NOISE_MARKERS)
    )


class VirtualEnvError(RuntimeError):
    pass


class VirtualEnvManager:
    def __init__(
        self,
        venv_dir: str | Path = SERVICE_RUNTIME_DIR,
    ) -> None:
        self.venv_dir = Path(venv_dir).expanduser().resolve()

    @property
    def python(self) -> Path:
        return self.venv_dir / "bin" / "python"

    def _require_uv(self) -> None:
        if shutil.which("uv") is None:
            raise VirtualEnvError("uv is not installed or not available in PATH")

    def _require_runtime(self) -> None:
        if not self.python.is_file():
            raise VirtualEnvError(f"Invalid ARC runtime: {self.venv_dir}")

    def _run(
        self,
        args: list[str],
        *,
        task: str | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run a command, optionally streaming its output with live progress."""
        if task is None:
            try:
                return subprocess.run(
                    args,
                    check=True,
                    text=True,
                    capture_output=True,
                    env=env,
                )
            except FileNotFoundError as exc:
                raise VirtualEnvError(f"Executable not found: {args[0]}") from exc
            except subprocess.CalledProcessError as exc:
                details = (exc.stderr or exc.stdout or "").strip()
                raise VirtualEnvError(
                    f"Command failed ({exc.returncode}): "
                    f"{' '.join(args)}\n{details}"
                ) from exc

        output_tail: deque[str] = deque(maxlen=40)
        process: subprocess.Popen[str] | None = None
        building_native = False

        try:
            with Progress(
                SpinnerColumn(style="cyan"),
                TextColumn("[bold cyan]{task.description}[/bold cyan]"),
                TimeElapsedColumn(),
                console=console,
                transient=True,
                refresh_per_second=10,
            ) as progress:
                task_id = progress.add_task(task, total=None)

                process = subprocess.Popen(
                    args,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=env,
                )

                if process.stdout is None:
                    raise VirtualEnvError("Could not capture command output.")

                with process.stdout:
                    for raw_line in process.stdout:
                        line = raw_line.rstrip("\r\n")
                        if not line:
                            continue

                        output_tail.append(line)
                        lowered = line.lower()

                        if not building_native and any(
                            marker in lowered for marker in _BUILD_MARKERS
                        ):
                            building_native = True
                            progress.update(
                                task_id,
                                description="Building native dependencies",
                            )

                        # Keep the live output useful: retain build/compiler messages,
                        # but omit routine cache and verbose resolver diagnostics.
                        if not _is_debug_output(line):
                            progress.console.print(
                                Text("  ", style="dim") + Text.from_ansi(line),
                                soft_wrap=True,
                            )

                returncode = process.wait()

        except FileNotFoundError as exc:
            raise VirtualEnvError(f"Executable not found: {args[0]}") from exc
        except BaseException:
            if process is not None and process.poll() is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass

                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            raise

        if returncode != 0:
            details = "\n".join(output_tail)
            raise VirtualEnvError(
                f"Command failed ({returncode}): {' '.join(args)}"
                + (f"\nLast output:\n{details}" if details else "")
            )

        return subprocess.CompletedProcess(
            args=args,
            returncode=returncode,
            stdout="",
            stderr="",
        )

    @property
    def venv_exists(self):
        return self.python.is_file()

    def create(self) -> None:
        if self.venv_exists:
            return

        self._require_uv()

        self.venv_dir.parent.mkdir(
            parents=True,
            exist_ok=True,
            mode=0o700,
        )

        if self.venv_dir.exists() and not self.venv_dir.is_dir():
            raise VirtualEnvError(f"Runtime path is not a directory: {self.venv_dir}")

        logger.warning(
            "Creating new venv in: %s",
            self.venv_dir,
        )

        self._run(
            [
                "uv",
                "venv",
                str(self.venv_dir),
            ],
            task="Preparing ARC runtime",
        )

        self._require_runtime()

    def ensure_venv(self, autofix: bool = False):
        if not self.venv_exists:
            if autofix:
                self.create()
            else:
                raise OSError(
                    f"Runtime venv: '{self.python}' doese not exist or is not a file."
                )

    def is_installed(self, package: str) -> bool:
        """Return whether a package is installed in the runtime venv."""
        self._require_runtime()

        result = subprocess.run(
            [
                str(self.python),
                "-c",
                (
                    "from importlib.metadata import version, "
                    "PackageNotFoundError; "
                    f"\ntry: print(version({package!r}))\n"
                    "except PackageNotFoundError: raise SystemExit(1)"
                ),
            ],
            text=True,
            capture_output=True,
        )

        return result.returncode == 0

    def installed_version(self, package: str) -> str | None:
        """Return the installed package version, or None."""
        self._require_runtime()

        result = subprocess.run(
            [
                str(self.python),
                "-c",
                (
                    "from importlib.metadata import version, "
                    "PackageNotFoundError; "
                    f"\ntry: print(version({package!r}))\n"
                    "except PackageNotFoundError: raise SystemExit(1)"
                ),
            ],
            text=True,
            capture_output=True,
        )

        if result.returncode != 0:
            return None

        return result.stdout.strip()

    def list_installed(self) -> dict[str, str]:
        """Return installed packages as {name: version}."""
        self._require_runtime()

        result = self._run(
            [
                "uv",
                "pip",
                "list",
                "--python",
                str(self.python),
                "--format",
                "json",
            ]
        )

        packages = json.loads(result.stdout)

        return {package["name"]: package["version"] for package in packages}

    def is_up_to_date(
        self,
        package: str,
        required_version: str | None = None,
    ) -> bool:
        """
        Return whether the installed version satisfies the requested version.

        When required_version is None, only checks whether the package exists.
        """
        installed = self.installed_version(package)

        if installed is None:
            return False

        if required_version is None:
            return True

        from packaging.specifiers import SpecifierSet
        from packaging.version import Version

        return Version(installed) in SpecifierSet(required_version)

    def install_git(
        self,
        url: str,
        *,
        ref: str | None = None,
    ) -> None:
        self.create()

        source = f"git+{url}"

        if ref:
            source = f"{source}@{ref}"

        args = [
            "uv",
            "pip",
            "install",
            "--python",
            str(self.python),
        ]

        args.append(source)

        self._run(args, task="Resolving and installing dependencies")

    def install(
        self,
        package: str,
    ) -> None:
        self.create()

        args = [
            "uv",
            "pip",
            "install",
            "--python",
            str(self.python),
        ]

        args.append(package)

        self._run(args, task="Resolving and installing dependencies")

    def update(self, package: str) -> None:
        self._require_uv()
        self._require_runtime()

        self._run(
            [
                "uv",
                "pip",
                "install",
                "--upgrade",
                "--python",
                str(self.python),
                package,
            ],
            task=f"Updating {package}",
        )

    def sync_project(
        self,
        project_dir: str | Path,
        *,
        upgrade: bool = False,
    ) -> None:
        """Sync a uv project into ARC's shared runtime without editable links.

        ``--active`` targets the ARC runtime venv supplied through VIRTUAL_ENV.
        ``--inexact`` retains packages installed for other services, while
        ``--no-editable`` makes service code self-contained in the runtime.
        The project-mode command also honours the project's ``tool.uv.sources``
        and named index configuration.
        """
        self.create()
        project_path = Path(project_dir).expanduser().resolve()
        if not (project_path / "pyproject.toml").is_file():
            raise VirtualEnvError(f"Missing project pyproject.toml: {project_path}")

        environment = os.environ.copy()
        environment["VIRTUAL_ENV"] = str(self.venv_dir)
        environment["PATH"] = os.pathsep.join(
            [str(self.venv_dir / "bin"), environment.get("PATH", "")]
        )

        args = [
            "uv",
            "sync",
            "--active",
            "--inexact",
            "--no-editable",
            "--project",
            str(project_path),
        ]
        if upgrade:
            args.append("--upgrade")

        self._run(
            args,
            task=("Updating ARC components" if upgrade else "Installing ARC service"),
            env=environment,
        )

    def remove(self, package: str) -> None:
        self._require_uv()
        self._require_runtime()

        self._run(
            [
                "uv",
                "pip",
                "uninstall",
                "--python",
                str(self.python),
                package,
            ],
            task=f"Removing {package}",
        )


SERVICE_RUNNER_PACKAGE = "arc-v2-service-runner"


class RuntimeBootstrap:
    def __init__(
        self,
        runtime: VirtualEnvManager,
    ) -> None:
        self.runtime: VirtualEnvManager = runtime

    @staticmethod
    def runner_requirement() -> str:
        """Return the runner source pinned to the Core's installed API version."""
        framework_version = version(SERVICE_RUNNER_PACKAGE)
        source = SERVICE_RUNNER_REGISTRY.get(SERVICE_RUNNER_PACKAGE)
        if source is None:
            raise RuntimeError(f"No source registered for {SERVICE_RUNNER_PACKAGE!r}")
        return f"git+{source}@v{framework_version}"

    def ensure(self, autofix: bool = False) -> None:
        self.runtime.ensure_venv(autofix)

        framework_version = version(SERVICE_RUNNER_PACKAGE)
        source = SERVICE_RUNNER_REGISTRY.get(SERVICE_RUNNER_PACKAGE)
        if source is None:
            raise RuntimeError(f"No source registered for {SERVICE_RUNNER_PACKAGE!r}")

        if self.runtime.is_up_to_date(
            SERVICE_RUNNER_PACKAGE,
            f"=={framework_version}",
        ):
            return

        self.runtime.install_git(
            source,
            ref=f"v{framework_version}",
        )
