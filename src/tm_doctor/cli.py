#!/usr/bin/env python3
"""Read-only diagnostics for macOS Time Machine."""

from __future__ import annotations

import json
import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TextColumn
from rich.table import Table
from rich.text import Text


VERSION = version("tm-doctor")

TRANSACTION_PATTERN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}-\d{6})"
    r"\.(?P<kind>previous|interrupted)$"
)

BACKUP_PATTERN = re.compile(
    r"(?P<timestamp>\d{4}-\d{2}-\d{2}-\d{6})\.backup$"
)

DEFAULT_WIDTH=78

console = Console()


@dataclass(frozen=True)
class Destination:
    """A configured Time Machine destination."""

    name: str | None
    kind: str | None
    destination_id: str
    mount_point: Path | None

    @property
    def mounted(self) -> bool:
        """Return whether the destination currently has a mount point."""
        return self.mount_point is not None


@dataclass(frozen=True)
class TransactionObject:
    """A Time Machine transaction object."""

    path: Path
    timestamp: datetime
    kind: str
    snapshot_state: str | None


@dataclass(frozen=True)
class BackupStatus:
    """Current Time Machine backup status."""

    running: bool
    phase: str | None
    percent: float | None
    time_remaining: float | None


@dataclass(frozen=True)
class DoctorReport:
    """Results of the read-only Time Machine examination."""

    destination: Destination
    backups: list[Path] | None
    latest_backup: Path | None
    transactions: list[TransactionObject] | None
    backup_status: BackupStatus


def run_command(
    *args: str,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command and capture its output."""

    return subprocess.run(
        args,
        input=stdin,
        text=True,
        capture_output=True,
        check=False,
    )


def get_destination() -> Destination:
    """Return the configured Time Machine destination."""

    result = run_command("tmutil", "destinationinfo")

    if result.returncode != 0:
        raise click.ClickException(
            "Unable to query Time Machine destination:\n"
            f"{result.stderr.strip()}"
        )

    values: dict[str, str] = {}

    for line in result.stdout.splitlines():
        key, separator, value = line.partition(":")

        if not separator:
            continue

        values[key.strip()] = value.strip()

    destination_id = values.get("ID")

    if not destination_id:
        raise click.ClickException(
            "No configured Time Machine destination was found."
        )

    mount_point = values.get("Mount Point")

    return Destination(
        name=values.get("Name"),
        kind=values.get("Kind"),
        destination_id=destination_id,
        mount_point=Path(mount_point) if mount_point else None,
    )


def get_backups() -> list[Path] | None:
    """
    Return backups reported by Time Machine.

    None means the backup inventory could not be queried. An empty list means
    the query succeeded but no backups were returned.
    """

    result = run_command("tmutil", "listbackups")

    if result.returncode != 0:
        return None

    return [
        Path(line.strip())
        for line in result.stdout.splitlines()
        if line.strip()
    ]


def get_latest_backup() -> Path | None:
    """Return the latest backup reported by Time Machine."""

    result = run_command("tmutil", "latestbackup")

    if result.returncode != 0:
        return None

    output = result.stdout.strip()

    return Path(output) if output else None


def parse_backup_timestamp(path: Path | None) -> datetime | None:
    """Extract a Time Machine timestamp from a backup path."""

    if path is None:
        return None

    for part in reversed(path.parts):
        match = BACKUP_PATTERN.match(part)

        if match:
            return datetime.strptime(
                match.group("timestamp"),
                "%Y-%m-%d-%H%M%S",
            )

    return None


def read_snapshot_state(path: Path) -> str | None:
    """Read Time Machine's private SnapshotState extended attribute."""

    result = run_command(
        "xattr",
        "-p",
        "com.apple.backupd.SnapshotState",
        str(path),
    )

    if result.returncode != 0:
        return None

    # SnapshotState values can be NUL terminated.
    return result.stdout.rstrip("\x00\r\n")


def parse_transaction(path: Path) -> TransactionObject | None:
    """Convert a Time Machine transaction filename into structured data."""

    match = TRANSACTION_PATTERN.match(path.name)

    if match is None:
        return None

    timestamp = datetime.strptime(
        match.group("timestamp"),
        "%Y-%m-%d-%H%M%S",
    )

    return TransactionObject(
        path=path,
        timestamp=timestamp,
        kind=match.group("kind"),
        snapshot_state=read_snapshot_state(path),
    )


def get_transactions(
    destination: Path,
) -> list[TransactionObject]:
    """Inspect transaction objects at the destination root."""

    transactions: list[TransactionObject] = []

    try:
        entries = destination.iterdir()

        for path in entries:
            # Avoid is_dir(), stat(), and other unnecessary metadata calls.
            # A degraded Time Machine destination can contain thousands of
            # transaction objects, making metadata-heavy enumeration costly.
            if not (
                path.name.endswith(".previous")
                or path.name.endswith(".interrupted")
            ):
                continue

            transaction = parse_transaction(path)

            if transaction is not None:
                transactions.append(transaction)

    except OSError as exc:
        raise click.ClickException(
            f"Unable to inspect {destination}: {exc}"
        ) from exc

    return sorted(
        transactions,
        key=lambda transaction: transaction.timestamp,
    )


def parse_tmutil_status() -> dict[str, Any]:
    """
    Convert `tmutil status` output into structured data.

    tmutil emits a heading followed by an old-style property list. Removing
    the heading allows macOS plutil to convert the body to JSON.
    """

    result = run_command("tmutil", "status")

    if result.returncode != 0:
        return {}

    lines = result.stdout.splitlines()

    if len(lines) < 2:
        return {}

    plist_body = "\n".join(lines[1:])

    converted = run_command(
        "plutil",
        "-convert",
        "json",
        "-o",
        "-",
        "-",
        stdin=plist_body,
    )

    if converted.returncode != 0:
        return {}

    try:
        return json.loads(converted.stdout)
    except json.JSONDecodeError:
        return {}


def parse_float(value: Any) -> float | None:
    """Convert an arbitrary value to float when possible."""

    if value is None:
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def get_backup_status() -> BackupStatus:
    """Return the current Time Machine backup status."""

    status = parse_tmutil_status()

    running = str(status.get("Running", "0")) == "1"

    phase_value = status.get("BackupPhase")
    phase = str(phase_value) if phase_value is not None else None

    progress = status.get("Progress")

    if not isinstance(progress, dict):
        progress = {}

    percent = parse_float(progress.get("Percent"))
    time_remaining = parse_float(progress.get("TimeRemaining"))

    return BackupStatus(
        running=running,
        phase=phase,
        percent=percent,
        time_remaining=time_remaining,
    )


def collect_report() -> DoctorReport:
    """Collect all v1 diagnostic information."""

    destination = get_destination()
    backup_status = get_backup_status()

    backups: list[Path] | None = None
    latest_backup: Path | None = None
    transactions: list[TransactionObject] | None = None

    if destination.mounted:
        assert destination.mount_point is not None

        backups = get_backups()
        latest_backup = get_latest_backup()

        with console.status(
            "[bold]Inspecting Time Machine housekeeping metadata...[/bold]"
        ):
            transactions = get_transactions(destination.mount_point)

    return DoctorReport(
        destination=destination,
        backups=backups,
        latest_backup=latest_backup,
        transactions=transactions,
        backup_status=backup_status,
    )


def format_timestamp(timestamp: datetime | None) -> str:
    """Format a timestamp for terminal output."""

    if timestamp is None:
        return "Unavailable"

    return timestamp.strftime("%Y-%m-%d %H:%M:%S")


def format_duration(seconds: float | None) -> str:
    """Format seconds as a compact human-readable duration."""

    if seconds is None or seconds < 0:
        return "Unavailable"

    total_seconds = int(seconds)
    minutes, remaining_seconds = divmod(total_seconds, 60)
    hours, remaining_minutes = divmod(minutes, 60)

    if hours:
        return f"{hours}h {remaining_minutes}m"

    if minutes:
        return f"{minutes}m {remaining_seconds}s"

    return f"{remaining_seconds}s"


def render_summary(report: DoctorReport) -> None:
    """Render destination and backup summary."""

    destination = report.destination
    latest = parse_backup_timestamp(report.latest_backup)

    text = Text()

    text.append("Destination  ", style="bold")
    text.append(destination.name or "Unknown")
    text.append("\n")

    text.append("Kind         ", style="bold")
    text.append(destination.kind or "Unknown")
    text.append("\n")

    text.append("Status       ", style="bold")

    if destination.mounted:
        text.append("Mounted", style="green")
    else:
        text.append("Not mounted", style="yellow")

    text.append("\n")

    if destination.mount_point is not None:
        text.append("Mount point  ", style="bold")
        text.append(str(destination.mount_point))
        text.append("\n")

    text.append("Latest       ", style="bold")
    text.append(format_timestamp(latest))
    text.append("\n")

    text.append("Backups      ", style="bold")

    if report.backups is None:
        text.append("Unavailable", style="yellow")
    else:
        text.append(f"{len(report.backups):,}")

    console.print(
        Panel(
            text,
            title="Time Machine",
            border_style="blue",
            width=DEFAULT_WIDTH,
        )
    )


def render_backup_status(status: BackupStatus) -> None:
    """Render current backup activity."""

    if not status.running:
        console.print(
            "[green]✓[/green] No backup currently running."
        )
        console.print()
        return

    phase = status.phase or "Unknown"

    text = Text()
    text.append("Phase  ", style="bold")
    text.append(phase)

    console.print(
        Panel(
            text,
            title="Current Backup",
            border_style="cyan",
            width=DEFAULT_WIDTH,
        )
    )

    if status.percent is not None and status.percent >= 0:
        percent = min(max(status.percent, 0.0), 1.0)

        progress = Progress(
            TextColumn("  "),
            BarColumn(bar_width=40),
            TextColumn("{task.percentage:>5.1f}%"),
            console=console,
        )

        progress.add_task(
            "backup",
            total=100,
            completed=percent * 100,
        )

        with progress:
            progress.refresh()

        if status.time_remaining is not None:
            console.print(
                f"  ETA: {format_duration(status.time_remaining)}"
            )

    elif phase == "ThinningPostBackup":
        console.print(
            "  [yellow]"
            "Post-backup housekeeping is in progress."
            "[/yellow]"
        )

    console.print()


def transaction_stats(
    transactions: list[TransactionObject],
    kind: str,
) -> tuple[list[TransactionObject], Counter[str]]:
    """Return transactions and state counts for one type."""

    selected = [
        transaction
        for transaction in transactions
        if transaction.kind == kind
    ]

    states: Counter[str] = Counter(
        transaction.snapshot_state or "unknown"
        for transaction in selected
    )

    return selected, states


def render_housekeeping(report: DoctorReport) -> int:
    """
    Render housekeeping information.

    Returns:
        0 when no severe accumulation is detected.
        1 when inspection is unavailable or severe accumulation is detected.
    """

    if not report.destination.mounted:
        console.print(
            Panel(
                (
                    "The Time Machine destination is not mounted.\n\n"
                    "Housekeeping metadata cannot be inspected "
                    "while the destination is offline."
                ),
                title="Housekeeping",
                border_style="yellow",
                width=DEFAULT_WIDTH,
            )
        )

        return 1

    if report.transactions is None:
        console.print(
            Panel(
                "Housekeeping metadata could not be inspected.",
                title="Housekeeping",
                border_style="yellow",
                width=DEFAULT_WIDTH,
            )
        )

        return 1

    table = Table(title="Housekeeping")

    table.add_column("Type")
    table.add_column("Count", justify="right")
    table.add_column("State 1", justify="right")
    table.add_column("State 16", justify="right")
    table.add_column("Unknown", justify="right")
    table.add_column("Oldest")
    table.add_column("Newest")

    state_16_total = 0

    for kind in ("previous", "interrupted"):
        selected, states = transaction_stats(
            report.transactions,
            kind,
        )

        state_16_total += states["16"]

        oldest = selected[0].timestamp if selected else None
        newest = selected[-1].timestamp if selected else None

        table.add_row(
            f".{kind}",
            f"{len(selected):,}",
            f"{states['1']:,}",
            f"{states['16']:,}",
            f"{states['unknown']:,}",
            format_timestamp(oldest),
            format_timestamp(newest),
        )

    console.print(table)
    console.print()

    backup_count = (
        len(report.backups)
        if report.backups is not None
        else 0
    )

    severe_threshold = max(
        100,
        backup_count * 5,
    )

    if state_16_total > severe_threshold:
        message = Text()

        message.append(
            "Housekeeping appears severely degraded.\n\n",
            style="bold red",
        )

        message.append(
            f"{state_16_total:,} state-16 transaction objects "
            f"remain for {backup_count:,} retained backups.\n\n"
        )

        message.append(
            "SnapshotState is private Time Machine metadata; "
            "this tool does not assign an undocumented semantic "
            "meaning to state 16. The accumulation itself is the "
            "diagnostic signal."
        )

        console.print(
            Panel(
                message,
                title="⚠ Housekeeping Warning",
                border_style="red",
                width=DEFAULT_WIDTH,
            )
        )

        return 1

    if state_16_total:
        console.print(
            Panel(
                (
                    f"{state_16_total:,} state-16 transaction "
                    "objects were found. This is worth monitoring."
                ),
                title="Housekeeping Notice",
                border_style="yellow",
                width=DEFAULT_WIDTH,
            )
        )

        return 0

    console.print(
        Panel(
            "No state-16 transaction-object accumulation detected.",
            title="Housekeeping",
            border_style="green",
            width=DEFAULT_WIDTH,
        )
    )

    return 0


def render_overall_status(exit_status: int) -> None:
    """Render the overall diagnostic result."""

    if exit_status:
        console.print(
            "[bold red]Overall: ATTENTION NEEDED[/bold red]"
        )
    else:
        console.print(
            "[bold green]"
            "Overall: no severe problems detected"
            "[/bold green]"
        )


@click.command()
@click.version_option(VERSION)
def cli() -> None:
    """
    Inspect a macOS Time Machine destination.

    Version 0.1 performs read-only diagnostics. It does not modify,
    repair, mount, unmount, or delete anything.
    """

    console.print()
    console.print(
        "[bold blue]Time Machine Doctor[/bold blue] "
        f"[dim]v{VERSION}[/dim]"
    )
    console.print()

    report = collect_report()

    render_summary(report)
    console.print()

    render_backup_status(report.backup_status)

    exit_status = render_housekeeping(report)

    console.print()
    render_overall_status(exit_status)

    raise SystemExit(exit_status)


if __name__ == "__main__":
    cli()
