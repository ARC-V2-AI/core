from __future__ import annotations

import asyncio
import sys
from importlib.metadata import version as package_version

import cyclopts
from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table

from arc.boot.boot import main as boot
from arc.cli.status import request_status
from arc.forge.forge import (
    Forge,
    ForgeError,
    ServiceAlreadyInstalledError,
    ServiceNotFoundError,
)

app = cyclopts.App(version=package_version("arc-v2-core"))
console = Console()


def _handle_cli_error(exc: Exception, *, debug: bool) -> None:
    if debug:
        console.print_exception()
        return

    if isinstance(exc, ServiceAlreadyInstalledError):
        console.print(f"[red]Error:[/red] {exc}")
        return

    if isinstance(exc, ServiceNotFoundError):
        console.print(f"[red]Error:[/red] {exc}")
        return

    if isinstance(exc, ForgeError):
        console.print(f"[red]Forge error:[/red] {exc}")
        return

    if isinstance(exc, FileNotFoundError):
        console.print(f"[red]Error:[/red] {exc}")
        return

    if isinstance(exc, PermissionError):
        console.print(f"[red]Permission error:[/red] {exc}")
        return

    console.print(f"[red]Error:[/red] {exc}")


def _run(command) -> None:
    try:
        command()
    except Exception as exc:
        _handle_cli_error(exc, debug=False)
        raise SystemExit(1) from None


@app.default
async def start(
    *,
    autofix: bool = False,
    debug: bool = False,
) -> None:
    try:
        await boot(
            autofix=autofix,
        )
    except Exception as exc:
        _handle_cli_error(
            exc,
            debug=debug,
        )
        raise SystemExit(1) from None


@app.command
def install(
    source: str,
    *,
    autofix: bool = False,
    debug: bool = False,
) -> None:
    console.print()
    console.print("[bold cyan]ARC-V2[/bold cyan] [dim]· FORGE[/dim]")
    console.print("[dim]────────────────────────────────────────────────────[/dim]")
    console.print("[bold]Installing service[/bold]")
    console.print(f"[cyan]{source}[/cyan]")
    console.print(
        "[dim]Installation time depends on the package. Packages with native "
        "dependencies may need to compile and can take several minutes.[/dim]"
    )
    console.print("[dim]Please keep this terminal open until installation finishes.[/dim]")
    console.print()

    try:
        forge = Forge.from_env(
            autofix=autofix,
        )

        spec = forge.install(source)

    except KeyboardInterrupt:
        console.print("[yellow]Installation interrupted by user.[/yellow]")
        raise SystemExit(130) from None
    except Exception as exc:
        _handle_cli_error(
            exc,
            debug=debug,
        )
        raise SystemExit(1) from None

    console.print(
        f"[bold green]✓ Installed[/bold green] '{spec.id}' "
        f"[dim]({spec.version})[/dim]"
    )


@app.command
def update(
    *,
    autofix: bool = False,
    debug: bool = False,
) -> None:
    console.print()
    console.print("[bold cyan]ARC-V2[/bold cyan] [dim]· FORGE[/dim]")
    console.print("[dim]────────────────────────────────────────────────────[/dim]")
    console.print("[bold]Updating installed ARC components[/bold]")
    console.print(
        "[dim]All locked services are resolved together so uv can select "
        "a consistent set of declared dependencies.[/dim]"
    )
    console.print()

    try:
        forge = Forge.from_env(autofix=autofix)
        updates = forge.update()
    except KeyboardInterrupt:
        console.print("[yellow]Update interrupted by user.[/yellow]")
        raise SystemExit(130) from None
    except Exception as exc:
        _handle_cli_error(exc, debug=debug)
        raise SystemExit(1) from None

    if not updates:
        console.print("[dim]No ARC services are installed.[/dim]")
        return

    table = Table(title="Updated ARC Services", header_style="bold")
    table.add_column("Service", no_wrap=True)
    table.add_column("Previous", no_wrap=True)
    table.add_column("Current", no_wrap=True)

    for service_id, previous, current in updates:
        table.add_row(service_id, previous, current)

    console.print(table)
    console.print("[bold green]✓ ARC components updated[/bold green]")


@app.command
def remove(
    service_id: str,
    *,
    autofix: bool = False,
    debug: bool = False,
) -> None:
    try:
        forge = Forge.from_env(
            autofix=autofix,
        )

        forge.remove(service_id)

    except Exception as exc:
        _handle_cli_error(
            exc,
            debug=debug,
        )
        raise SystemExit(1) from None

    console.print(f"[green]Removed[/green] '{service_id}'")


@app.command(name="list")
def list_services(
    *,
    native: bool = False,
    autofix: bool = False,
    debug: bool = False,
) -> None:
    """List installed services, or the official ARC service catalog."""
    try:
        forge = Forge.from_env(autofix=autofix)
        if native:
            _list_native_services(forge, debug=debug)
            return

        services = forge.list()

    except Exception as exc:
        _handle_cli_error(
            exc,
            debug=debug,
        )
        raise SystemExit(1) from None

    if not services:
        console.print("[dim]No ARC services installed.[/dim]")
        return

    table = Table(
        title="Installed ARC Services",
        header_style="bold",
    )

    table.add_column("ID", no_wrap=True)
    table.add_column("Version", no_wrap=True)
    table.add_column("Package", no_wrap=True)
    table.add_column("Module", no_wrap=True)

    for spec in services:
        table.add_row(
            spec.id,
            spec.version,
            spec.package,
            spec.module,
        )

    console.print(table)


def _list_native_services(forge: Forge, *, debug: bool) -> None:
    """Display official services and optionally install selected entries."""
    native_services = forge.list_native()
    if not native_services:
        console.print("[dim]No official ARC services are currently published.[/dim]")
        return

    table = Table(
        title="Official ARC Services",
        header_style="bold",
    )
    table.add_column("#", justify="right", no_wrap=True)
    table.add_column("Service", no_wrap=True)
    table.add_column("Version", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Repository", overflow="fold")

    available: list[tuple[str, str]] = []
    for service_id, repository, installed_spec in native_services:
        if installed_spec is None:
            selection = str(len(available) + 1)
            version_text = "—"
            status = "[yellow]○ Available[/yellow]"
            available.append((service_id, repository))
        else:
            selection = "—"
            version_text = installed_spec.version
            status = "[green]✓ Installed[/green]"

        table.add_row(selection, service_id, version_text, status, repository)

    console.print(table)

    if not available:
        console.print("[dim]All official ARC services are already installed.[/dim]")
        return

    if not sys.stdin.isatty():
        console.print(
            "[dim]Install an available service with "
            "`arc install <service-id>`.[/dim]"
        )
        return

    console.print(
        "[dim]Select one or more available services by number, separated by commas. "
        "Enter 'q' to cancel.[/dim]"
    )
    selection = Prompt.ask("Install", console=console, default="q").strip().lower()
    if selection in {"", "q", "quit", "cancel"}:
        return

    selected_indices: list[int] = []
    try:
        for value in selection.split(","):
            index = int(value.strip())
            if index < 1 or index > len(available):
                raise ValueError
            if index not in selected_indices:
                selected_indices.append(index)
    except ValueError:
        console.print(
            f"[red]Invalid selection.[/red] Choose numbers from 1 to {len(available)}, "
            "separated by commas, or enter 'q' to cancel."
        )
        return

    for index in selected_indices:
        service_id, _repository = available[index - 1]
        console.print()
        console.print(f"[bold]Installing {service_id}[/bold]")
        try:
            spec = forge.install(service_id)
        except KeyboardInterrupt:
            console.print("[yellow]Installation interrupted by user.[/yellow]")
            raise SystemExit(130) from None
        except Exception as exc:
            _handle_cli_error(exc, debug=debug)
            console.print(f"[red]Could not install {service_id}.[/red]")
            continue

        console.print(
            f"[bold green]✓ Installed[/bold green] '{spec.id}' "
            f"[dim]({spec.version})[/dim]"
        )


@app.command
def info(
    service_id: str,
    *,
    debug: bool = False,
) -> None:
    try:
        forge = Forge.from_env()
        spec = forge.info(service_id)

    except Exception as exc:
        _handle_cli_error(
            exc,
            debug=debug,
        )
        raise SystemExit(1) from None

    table = Table.grid(
        padding=(0, 2),
    )

    table.add_column(
        style="bold",
        no_wrap=True,
    )
    table.add_column()

    table.add_row(
        "ID",
        spec.id,
    )
    table.add_row(
        "Name",
        spec.name,
    )
    table.add_row(
        "Version",
        spec.version,
    )
    table.add_row(
        "Package",
        spec.package,
    )
    table.add_row(
        "Module",
        spec.module,
    )
    table.add_row(
        "Description",
        spec.description or "—",
    )
    table.add_row(
        "Restart",
        spec.restart,
    )
    table.add_row(
        "Health",
        spec.health,
    )
    table.add_row(
        "Depends",
        ", ".join(spec.depends) or "none",
    )

    console.print(f"[bold]Service: {spec.id}[/bold]")
    console.print(table)


@app.command
def status() -> None:
    try:
        response = asyncio.run(request_status())
    except FileNotFoundError:
        console.print("[yellow]ARC is not running.[/yellow]")
        return
    except (
        ConnectionRefusedError,
        ConnectionResetError,
        OSError,
    ) as exc:
        console.print(f"[red]Could not connect to ARC:[/red] {exc}")
        raise SystemExit(1) from None

    if not response.get("ok"):
        console.print(f"[red]Error:[/red] {response.get('error', 'unknown error')}")
        raise SystemExit(1)

    services = response.get(
        "services",
        [],
    )

    if not services:
        console.print("[dim]No services registered.[/dim]")
        return

    table = Table(
        title="ARC Services",
        header_style="bold",
    )

    table.add_column(
        "ID",
        no_wrap=True,
    )
    table.add_column(
        "State",
        no_wrap=True,
    )
    table.add_column(
        "PID",
        no_wrap=True,
    )
    table.add_column(
        "Restarts",
        no_wrap=True,
    )

    for service in services:
        table.add_row(
            service["id"],
            service["state"],
            str(service["pid"] or "—"),
            str(service["restart_count"]),
        )

    console.print(table)


if __name__ == "__main__":
    app()
