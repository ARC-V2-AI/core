from __future__ import annotations

import asyncio
from importlib.metadata import version as package_version

import cyclopts
from rich.console import Console
from rich.panel import Panel
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
    editable: bool = False,
    autofix: bool = False,
    debug: bool = False,
) -> None:
    console.print(
        Panel(
            "[bold]Installing ARC service[/bold]\n"
            f"[cyan]{source}[/cyan]\n\n"
            "[dim]Dependency installation may compile native code and take several minutes. "
            "Keep this terminal open while the installation is running.[/dim]",
            title="[bold cyan]ARC-V2 · FORGE[/bold cyan]",
            border_style="cyan",
            padding=(1, 2),
        )
    )

    try:
        forge = Forge.from_env(
            autofix=autofix,
        )

        spec = forge.install(
            source,
            editable=editable,
        )

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
    debug: bool = False,
) -> None:
    try:
        forge = Forge.from_env()
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

    table.add_column(
        "ID",
        no_wrap=True,
    )
    table.add_column(
        "Version",
        no_wrap=True,
    )
    table.add_column(
        "Package",
        no_wrap=True,
    )
    table.add_column(
        "Module",
        no_wrap=True,
    )

    for spec in services:
        table.add_row(
            spec.id,
            spec.version,
            spec.package,
            spec.module,
        )

    console.print(table)


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
