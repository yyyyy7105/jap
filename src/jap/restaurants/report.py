from __future__ import annotations

from datetime import datetime

from rich.console import Console
from rich.table import Table

from .models import CheckResult, ScanInfo, Slot, describe_windows

console = Console()


def _windows(r) -> str:
    return describe_windows(r)


def print_results(
    results: list[CheckResult],
    new_keys: set[str] | None = None,
    show_all: bool = True,
    scan: ScanInfo | None = None,
    console=console,
) -> None:
    """Print the round's summary and open slots to `console` (a rich Console, or a
    watch.Output, which also appends to the shared log)."""
    console.rule(f"[bold]Restaurant availability[/] · {(scan.started if scan else datetime.now()):%Y-%m-%d %H:%M:%S}")
    summary = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    summary.add_column("Restaurant")
    summary.add_column("Platform")
    summary.add_column("Pax", justify="right")
    summary.add_column("Scanned range (JST)")
    summary.add_column("Result")
    for res in results:
        r = res.restaurant
        if res.error:
            status = f"[red]error[/] {res.error}"
        elif res.slots:
            n_new = sum(1 for s in res.slots if new_keys and s.key in new_keys)
            status = f"[green]{len(res.slots)} open[/]" + (f" [bold yellow]({n_new} new)[/]" if n_new else "")
        else:
            status = "[dim]none[/]"
        rng = scan.ranges.get(r.name) if scan else None
        summary.add_row(r.name, r.platform, str(r.party_size), rng or _windows(r), status)
    console.print(summary)
    if scan:
        console.print(f"[dim]Scan ran {scan.when}[/]")

    slots: list[Slot] = sorted(s for res in results for s in res.slots)
    if new_keys is not None and not show_all:
        slots = [s for s in slots if s.key in new_keys]
    if not slots:
        return
    t = Table(title="Open slots", header_style="bold")
    t.add_column("")
    t.add_column("Date")
    t.add_column("Time")
    t.add_column("Restaurant")
    t.add_column("Pax", justify="right")
    t.add_column("Info")
    t.add_column("Book")
    for s in slots:
        flag = "[bold yellow]NEW[/]" if new_keys and s.key in new_keys else ""
        link = f"[link={s.url}]open[/link]" if s.url else ""
        t.add_row(flag, f"{s.date:%a %Y-%m-%d}", f"{s.time:%H:%M}", s.restaurant, str(s.party_size), s.detail, link)
    console.print(t)
