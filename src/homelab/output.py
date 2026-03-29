from __future__ import annotations

from rich.console import Console

console = Console()
error_console = Console(stderr=True)


def print_header(message: str) -> None:
    console.print(f"=== {message} ===", style="bold")


def print_action(message: str) -> None:
    console.print(f"==> {message}", style="cyan")


def print_sub(message: str) -> None:
    console.print(f"    {message}")


def print_ok(message: str) -> None:
    console.print(f"    [green]\N{CHECK MARK}[/green] {message}")


def print_warn(message: str) -> None:
    console.print(f"    [yellow]\N{MULTIPLICATION X} Warning:[/yellow] {message}")


def print_error(message: str) -> None:
    error_console.print(f"    [red]\N{MULTIPLICATION X} Error:[/red] {message}")
