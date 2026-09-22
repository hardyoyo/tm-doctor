#!/usr/bin/env python3
"""Read-only diagnostics for macOS Time Machine."""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TextColumn
from rich.table import Table
from rich.text import Text

VERSION = version("tm-doctor")

TRANSACTION_PATTERN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}-\d{6})"
    r"\.(?P<kind>previous|interrupted)$"
)

BACKUP_PATTERN = re.compile(r"(?P<timestamp>\d{4}-\d{2}-\d{2}-\d{6})\.backup$")

DEFAULT_WIDTH = 78

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
    destination_filesystem: str | None = None
    disk_sleep: int | None = None
    heavy_unexcluded_paths: list[Path] = field(default_factory=list)
    usb_info: UsbConnectionInfo | None = None
    spotlight_indexed: bool | None = None


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


def _parse_destination_block(values: dict[str, str]) -> Destination | None:
    destination_id = values.get("ID")
    if not destination_id:
        return None
    mount_point = values.get("Mount Point")
    return Destination(
        name=values.get("Name"),
        kind=values.get("Kind"),
        destination_id=destination_id,
        mount_point=Path(mount_point) if mount_point else None,
    )


def get_destinations() -> list[Destination]:
    """Return all configured Time Machine destinations."""

    result = run_command("tmutil", "destinationinfo")

    if result.returncode != 0:
        raise click.ClickException(
            f"Unable to query Time Machine destination:\n{result.stderr.strip()}"
        )

    destinations: list[Destination] = []
    current: dict[str, str] = {}

    for line in result.stdout.splitlines():
        if line.startswith("="):
            if current:
                dest = _parse_destination_block(current)
                if dest:
                    destinations.append(dest)
                current = {}
            continue

        key, separator, value = line.partition(":")
        if not separator:
            continue
        current[key.strip()] = value.strip()

    if current:
        dest = _parse_destination_block(current)
        if dest:
            destinations.append(dest)

    return destinations


def get_destination() -> Destination:
    """Return the configured Time Machine destination."""

    result = run_command("tmutil", "destinationinfo")

    if result.returncode != 0:
        raise click.ClickException(
            f"Unable to query Time Machine destination:\n{result.stderr.strip()}"
        )

    values: dict[str, str] = {}

    for line in result.stdout.splitlines():
        key, separator, value = line.partition(":")

        if not separator:
            continue

        values[key.strip()] = value.strip()

    destination_id = values.get("ID")

    if not destination_id:
        raise click.ClickException("No configured Time Machine destination was found.")

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

    return [Path(line.strip()) for line in result.stdout.splitlines() if line.strip()]


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
            if not (path.name.endswith(".previous") or path.name.endswith(".interrupted")):
                continue

            transaction = parse_transaction(path)

            if transaction is not None:
                transactions.append(transaction)

    except OSError as exc:
        raise click.ClickException(f"Unable to inspect {destination}: {exc}") from exc

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


_HEAVY_PATH_CANDIDATES: list[Path] = [
    # Cloud sync
    Path.home() / "Library" / "CloudStorage",
    Path.home() / "Dropbox",
    Path.home() / "OneDrive",
    Path.home() / "Google Drive",
    # Virtual machines
    Path.home() / "Parallels",
    Path.home() / "Documents" / "Virtual Machines.localized",
    # Docker / container runtimes
    Path.home() / "Library" / "Containers" / "com.docker.docker" / "Data",
    Path.home() / ".colima",
    Path.home() / ".lima",
    Path.home() / "Library" / "Application Support" / "OrbStack",
    # Xcode build artifacts
    Path.home() / "Library" / "Developer" / "Xcode" / "DerivedData",
    Path.home() / "Library" / "Developer" / "Xcode" / "iOS DeviceSupport",
    Path.home() / "Library" / "Developer" / "CoreSimulator" / "Devices",
    # Package manager caches
    Path.home() / ".cargo" / "registry",
    Path.home() / ".npm",
    Path.home() / ".gradle" / "caches",
    Path.home() / ".m2" / "repository",
    Path.home() / "Library" / "Caches" / "Homebrew",
    # Gaming
    Path.home() / "Library" / "Application Support" / "Steam" / "steamapps",
    # Microsoft Office / Outlook (large SQLite stores, rewritten on every sync)
    Path.home() / "Library" / "Group Containers" / "UBF8T346G9.Office",
    Path.home() / "Library" / "Containers" / "com.microsoft.Outlook",
]


def get_destination_filesystem(destination: Destination) -> str | None:
    if not destination.mounted or destination.mount_point is None:
        return None
    result = run_command("diskutil", "info", str(destination.mount_point))
    if result.returncode != 0:
        return None
    match = re.search(r"File System Personality:\s+(.+)", result.stdout)
    if match:
        return match.group(1).strip()
    return None


def get_disk_sleep_setting() -> int | None:
    result = run_command("pmset", "-g")
    if result.returncode != 0:
        return None
    match = re.search(r"disksleep\s+(\d+)", result.stdout)
    if match:
        return int(match.group(1))
    return None


def get_destination_spotlight_indexed(destination: Destination) -> bool | None:
    if not destination.mounted or destination.mount_point is None:
        return None
    result = run_command("mdutil", "-s", str(destination.mount_point))
    if result.returncode != 0:
        return None
    text = result.stdout.lower()
    if "indexing enabled" in text:
        return True
    if "indexing disabled" in text:
        return False
    return None


def get_heavy_unexcluded_paths() -> list[Path]:
    unexcluded: list[Path] = []
    for candidate in _HEAVY_PATH_CANDIDATES:
        if not candidate.exists() or candidate.is_symlink():
            continue
        result = run_command("tmutil", "isexcluded", str(candidate))
        if result.returncode == 0 and result.stdout.startswith("[Included]"):
            unexcluded.append(candidate)
    return unexcluded


@dataclass(frozen=True)
class UsbConnectionInfo:
    """USB connection details for a destination drive."""

    hub_depth: int
    negotiated_speed: int | None  # Device Speed value from ioreg
    speed_capability: int | None  # bcdUSB value from ioreg (e.g. 0x0300 = USB 3.0)


# Device Speed values reported by ioreg
_USB_SPEED_NAMES: dict[int, str] = {
    0: "Low Speed (1.5 Mbps)",
    1: "Full Speed (12 Mbps)",
    2: "High Speed (480 Mbps / USB 2.0)",
    3: "SuperSpeed (5 Gbps / USB 3.0)",
    4: "SuperSpeedPlus (10 Gbps / USB 3.1+)",
}

# bcdUSB threshold for USB 3.x capability
_USB3_BCD_MIN = 0x0300  # 768 decimal


def _usb_connection_info_from_ioreg(
    ioreg_output: str, media_name: str
) -> UsbConnectionInfo | None:
    """
    Parse `ioreg -p IOUSB -l -w 0` output to find a device by name.

    Returns hub depth and speed properties, or None if the device cannot
    be located in the tree.
    """

    @dataclass
    class _Node:
        depth: int
        name: str
        product: str | None = None
        device_speed: int | None = None
        bcd_usb: int | None = None

    nodes: list[_Node] = []
    current: _Node | None = None

    for line in ioreg_output.splitlines():
        # ioreg tree lines can be "  +-o Name" or "  | +-o Name" (branch continuation).
        # Use [\s|]* to consume both spaces and pipe characters as depth indicators.
        node_m = re.match(r"^([\s|]*)\+-o\s+(\S+)", line)
        if node_m:
            depth = len(node_m.group(1)) // 2
            name = node_m.group(2).split("@")[0]
            current = _Node(depth=depth, name=name)
            nodes.append(current)
            continue
        if current is None:
            continue
        prod_m = re.search(r'"kUSBProductString"\s*=\s*"([^"]+)"', line)
        if prod_m:
            current.product = prod_m.group(1)
            continue
        speed_m = re.search(r'"Device Speed"\s*=\s*(\d+)', line)
        if speed_m:
            current.device_speed = int(speed_m.group(1))
            continue
        bcd_m = re.search(r'"bcdUSB"\s*=\s*(\d+)', line)
        if bcd_m:
            current.bcd_usb = int(bcd_m.group(1))

    # Match target device: product string contains the media name or vice-versa,
    # with common "Media" / "Disk" suffixes stripped.
    search_name = re.sub(r"\s+(media|disk)$", "", media_name.strip(), flags=re.IGNORECASE).lower()
    target_idx = None
    for i, node in enumerate(nodes):
        product = node.product  # only match nodes that have an actual product string
        if product is None:
            continue
        candidate = product.lower()
        if search_name in candidate or candidate in search_name:
            target_idx = i
            break

    if target_idx is None:
        return None

    hub_count = 0
    current_depth = nodes[target_idx].depth
    for node in reversed(nodes[:target_idx]):
        if node.depth < current_depth:
            current_depth = node.depth
            label = (node.product or node.name).lower()
            if "hub" in label and "root" not in label and "simulation" not in label:
                hub_count += 1

    target = nodes[target_idx]
    return UsbConnectionInfo(
        hub_depth=hub_count,
        negotiated_speed=target.device_speed,
        speed_capability=target.bcd_usb,
    )


def get_destination_usb_info(destination: Destination) -> UsbConnectionInfo | None:
    """
    Returns USB connection info for the destination drive.
    None = not a USB device or could not determine.

    The mount point is typically an APFS volume (e.g. disk7s2), so we must
    walk up to the physical disk (disk7) to find Protocol and Media Name.
    """
    if not destination.mounted or destination.mount_point is None:
        return None

    vol_di = run_command("diskutil", "info", str(destination.mount_point))
    if vol_di.returncode != 0:
        return None

    dev_m = re.search(r"Device Identifier:\s+(disk\d+)", vol_di.stdout)
    if not dev_m:
        return None
    physical_disk = re.sub(r"s\d+$", "", dev_m.group(1))

    phys_di = run_command("diskutil", "info", physical_disk)
    if phys_di.returncode != 0:
        return None

    protocol_m = re.search(r"Protocol:\s+(.+)", phys_di.stdout)
    if not protocol_m or "USB" not in protocol_m.group(1):
        return None

    media_m = re.search(r"Device / Media Name:\s+(.+)", phys_di.stdout)
    if not media_m:
        return None
    media_name = media_m.group(1).strip()

    ioreg = run_command("ioreg", "-p", "IOUSB", "-l", "-w", "0")
    if ioreg.returncode != 0:
        return None

    return _usb_connection_info_from_ioreg(ioreg.stdout, media_name)


def collect_report(destination: Destination) -> DoctorReport:
    """Collect all v1 diagnostic information for one destination."""

    backup_status = get_backup_status()

    backups: list[Path] | None = None
    latest_backup: Path | None = None
    transactions: list[TransactionObject] | None = None

    destination_filesystem: str | None = None
    disk_sleep: int | None = None
    heavy_unexcluded_paths: list[Path] = []
    usb_info: UsbConnectionInfo | None = None
    spotlight_indexed: bool | None = None

    if destination.mounted:
        assert destination.mount_point is not None

        backups = get_backups()
        latest_backup = get_latest_backup()

        with console.status("[bold]Inspecting Time Machine housekeeping metadata...[/bold]"):
            transactions = get_transactions(destination.mount_point)

        destination_filesystem = get_destination_filesystem(destination)
        disk_sleep = get_disk_sleep_setting()
        heavy_unexcluded_paths = get_heavy_unexcluded_paths()
        usb_info = get_destination_usb_info(destination)
        spotlight_indexed = get_destination_spotlight_indexed(destination)

    return DoctorReport(
        destination=destination,
        backups=backups,
        latest_backup=latest_backup,
        transactions=transactions,
        backup_status=backup_status,
        destination_filesystem=destination_filesystem,
        disk_sleep=disk_sleep,
        heavy_unexcluded_paths=heavy_unexcluded_paths,
        usb_info=usb_info,
        spotlight_indexed=spotlight_indexed,
    )


def report_to_dict(report: DoctorReport) -> dict[str, Any]:
    """Serialize a DoctorReport to a JSON-compatible dict."""

    dest = report.destination
    return {
        "destination": {
            "name": dest.name,
            "kind": dest.kind,
            "destination_id": dest.destination_id,
            "mount_point": str(dest.mount_point) if dest.mount_point else None,
            "mounted": dest.mounted,
        },
        "backup_status": {
            "running": report.backup_status.running,
            "phase": report.backup_status.phase,
            "percent": report.backup_status.percent,
            "time_remaining": report.backup_status.time_remaining,
        },
        "backups": [str(p) for p in report.backups] if report.backups is not None else None,
        "latest_backup": str(report.latest_backup) if report.latest_backup else None,
        "transactions": [
            {
                "path": str(t.path),
                "timestamp": t.timestamp.isoformat(),
                "kind": t.kind,
                "snapshot_state": t.snapshot_state,
            }
            for t in report.transactions
        ]
        if report.transactions is not None
        else None,
        "destination_filesystem": report.destination_filesystem,
        "disk_sleep": report.disk_sleep,
        "heavy_unexcluded_paths": [str(p) for p in report.heavy_unexcluded_paths],
        "usb_hub_depth": report.usb_info.hub_depth if report.usb_info else None,
        "usb_negotiated_speed": report.usb_info.negotiated_speed if report.usb_info else None,
        "usb_speed_capability": report.usb_info.speed_capability if report.usb_info else None,
        "spotlight_indexed": report.spotlight_indexed,
    }


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
        console.print("[green]✓[/green] No backup currently running.")
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
            console.print(f"  ETA: {format_duration(status.time_remaining)}")

    elif phase == "ThinningPostBackup":
        console.print("  [yellow]Post-backup housekeeping is in progress.[/yellow]")

    console.print()


def transaction_stats(
    transactions: list[TransactionObject],
    kind: str,
) -> tuple[list[TransactionObject], Counter[str]]:
    """Return transactions and state counts for one type."""

    selected = [transaction for transaction in transactions if transaction.kind == kind]

    states: Counter[str] = Counter(
        transaction.snapshot_state or "unknown" for transaction in selected
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

    backup_count = len(report.backups) if report.backups is not None else 0

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


def render_checks(report: DoctorReport) -> None:
    """Render a summary panel of all automated checks and their results."""
    if not report.destination.mounted:
        return

    lines: list[str] = []

    fs = report.destination_filesystem
    if fs is None:
        lines.append("[yellow]?[/yellow]  Drive format: could not determine")
    elif "APFS" in fs:
        lines.append(f"[green]✓[/green]  Drive format: {fs}")
    else:
        lines.append(f"[red]✗[/red]  Drive format: {fs} (not APFS)")

    if report.disk_sleep is None:
        lines.append("[yellow]?[/yellow]  Disk sleep: could not determine")
    elif report.disk_sleep == 0:
        lines.append("[green]✓[/green]  Disk sleep: disabled")
    else:
        lines.append(f"[red]✗[/red]  Disk sleep: enabled ({report.disk_sleep} minute(s))")

    usb = report.usb_info
    if usb is None:
        lines.append("[yellow]?[/yellow]  USB connection: not USB or could not determine")
    else:
        hub_str = "directly connected" if usb.hub_depth == 0 else f"via {usb.hub_depth} hub(s)"
        speed_name = (
            _USB_SPEED_NAMES.get(usb.negotiated_speed, f"speed {usb.negotiated_speed}")
            if usb.negotiated_speed is not None
            else "unknown speed"
        )
        is_downgraded = (
            usb.speed_capability is not None
            and usb.speed_capability >= _USB3_BCD_MIN
            and usb.negotiated_speed is not None
            and usb.negotiated_speed < 3
        )
        marker = "[red]✗[/red]" if usb.hub_depth > 0 or is_downgraded else "[green]✓[/green]"
        lines.append(f"{marker}  USB connection: {hub_str}, {speed_name}")

    if report.spotlight_indexed is None:
        lines.append("[yellow]?[/yellow]  Spotlight indexing: could not determine")
    elif report.spotlight_indexed:
        lines.append("[red]✗[/red]  Spotlight indexing: enabled on backup volume")
    else:
        lines.append("[green]✓[/green]  Spotlight indexing: disabled on backup volume")

    if report.heavy_unexcluded_paths:
        names = ", ".join(p.name for p in report.heavy_unexcluded_paths)
        lines.append(f"[red]✗[/red]  Unexcluded heavy folders: {names}")
    else:
        lines.append("[green]✓[/green]  Heavy folders: none found unexcluded")

    console.print()
    console.print(
        Panel(
            "\n".join(lines),
            title="Checks",
            border_style="dim",
            width=DEFAULT_WIDTH,
        )
    )


def _interrupted_advice_applies(report: DoctorReport) -> bool:
    if report.destination.kind != "Local":
        return False
    if report.transactions is None:
        return False
    _, states = transaction_stats(report.transactions, "interrupted")
    interrupted_16 = states["16"]
    backup_count = len(report.backups) if report.backups is not None else 0
    threshold = max(100, backup_count * 5)
    if interrupted_16 <= threshold:
        return False
    # Suppress the hub/dock advice when we can confirm the drive is directly connected.
    # If usb_info is None (not USB, or could not determine), show the advice
    # conservatively. If hub_depth is 0 (directly connected), suppress it.
    hub_depth = report.usb_info.hub_depth if report.usb_info is not None else None
    return hub_depth is None or hub_depth > 0


def _state16_severe_applies(report: DoctorReport) -> bool:
    if report.transactions is None:
        return False
    _, prev_states = transaction_stats(report.transactions, "previous")
    _, int_states = transaction_stats(report.transactions, "interrupted")
    state_16_total = prev_states["16"] + int_states["16"]
    backup_count = len(report.backups) if report.backups is not None else 0
    threshold = max(100, backup_count * 5)
    return state_16_total > threshold


def _state16_findings(report: DoctorReport) -> list[str]:
    """Return actionable finding strings for state-16 causes, or empty list if none."""
    findings: list[str] = []

    fs = report.destination_filesystem
    if fs is not None and "APFS" not in fs:
        findings.append(
            f"Drive format: Your Time Machine destination is formatted as {fs} "
            f"(not APFS). On macOS Big Sur and newer, Time Machine requires APFS. "
            f"The current format causes a legacy sparsebundle wrapper that "
            f"frequently drops the connection mid-backup. If you have no critical "
            f"backup history to preserve, reformat the drive as APFS using "
            f"Disk Utility."
        )

    if report.disk_sleep is not None and report.disk_sleep > 0:
        findings.append(
            f"Hard disk sleep: Your Mac is set to put hard disks to sleep after "
            f"{report.disk_sleep} minute(s). This can cut power to the backup "
            f"drive mid-backup. Go to System Settings > Battery (or Energy Saver) "
            f"> Options and disable 'Put hard disks to sleep when possible'."
        )

    usb = report.usb_info
    if (
        usb is not None
        and usb.speed_capability is not None
        and usb.speed_capability >= _USB3_BCD_MIN
        and usb.negotiated_speed is not None
        and usb.negotiated_speed < 3
    ):
        actual = _USB_SPEED_NAMES.get(usb.negotiated_speed, f"speed {usb.negotiated_speed}")
        findings.append(
            f"USB connection speed: The drive is capable of USB 3.0 (5 Gbps) but "
            f"is currently connected at {actual}. A USB 2.0-only cable or adapter "
            f"is likely limiting the connection and may cause Time Machine to time "
            f"out during large backups. Replace it with a USB 3.0-compatible cable "
            f"or adapter."
        )

    if report.heavy_unexcluded_paths:
        home = Path.home()
        paths_display = "\n".join(
            "  ~/" + str(p.relative_to(home)) if p.is_relative_to(home) else "  " + str(p)
            for p in report.heavy_unexcluded_paths
        )
        findings.append(
            f"The following folders were confirmed present on this Mac and are "
            f"not excluded from Time Machine backups:\n{paths_display}\n"
            f"These paths change frequently and can cause backup conflicts or "
            f"interruptions. Run with --add-exclusions to add them automatically, "
            f"or add them manually in "
            f"System Settings > General > Time Machine > Options."
        )

    return findings


def _is_monitor_state(report: DoctorReport) -> bool:
    """True when state-16 is severe but all checked causes appear healthy."""
    return _state16_severe_applies(report) and not _state16_findings(report)


def render_advice(report: DoctorReport) -> None:
    if _interrupted_advice_applies(report):
        console.print()
        console.print(
            Panel(
                (
                    "A high number of interrupted backups was detected on a "
                    "local destination.\n\n"
                    "If your connection path is complex -- for example, through "
                    "a dock or USB hub -- consider connecting the drive directly "
                    "to your Mac to see whether interruptions decrease."
                ),
                title="Advice",
                border_style="blue",
                width=DEFAULT_WIDTH,
            )
        )

    severe = _state16_severe_applies(report)
    findings = _state16_findings(report)

    if findings:
        if severe:
            preamble = (
                "A high number of state-16 transaction objects was detected. "
                "The following conditions are likely contributing:\n\n"
            )
        else:
            preamble = (
                "The following conditions are known risk factors for Time "
                "Machine problems and are worth addressing:\n\n"
            )
        console.print()
        console.print(
            Panel(
                preamble + "\n\n".join(findings),
                title="Advice",
                border_style="blue",
                width=DEFAULT_WIDTH,
            )
        )
        return

    if severe:
        console.print()
        console.print(
            Panel(
                "A high number of state-16 transaction objects was detected, "
                "but the common causes were checked and appear healthy for this "
                "destination.\n\n"
                "The accumulation may reflect historical activity from before "
                "recent configuration changes. Monitor over the next several "
                "backup cycles to see whether the count decreases.",
                title="Advice",
                border_style="blue",
                width=DEFAULT_WIDTH,
            )
        )


def apply_exclusions(reports: list[DoctorReport]) -> None:
    all_paths: list[Path] = []
    for report in reports:
        all_paths.extend(report.heavy_unexcluded_paths)

    if not all_paths:
        console.print()
        console.print("[green]No unexcluded heavy folders found -- nothing to do.[/green]")
        return

    console.print()
    console.print("The following folders will be excluded from Time Machine:")
    for path in all_paths:
        console.print(f"  {path}")
    console.print()

    if not click.confirm("Add these exclusions?", default=False):
        console.print("Skipped.")
        return

    for path in all_paths:
        result = run_command("tmutil", "addexclusion", str(path))
        if result.returncode == 0:
            console.print(f"[green]Excluded:[/green] {path}")
        else:
            console.print(f"[red]Failed:[/red] {path} -- {result.stderr.strip()}")


def render_overall_status(exit_status: int, monitor: bool = False) -> None:
    """Render the overall diagnostic result."""

    if exit_status and monitor:
        console.print("[bold yellow]Overall: CONTINUE TO MONITOR[/bold yellow]")
    elif exit_status:
        console.print("[bold red]Overall: ATTENTION NEEDED[/bold red]")
    else:
        console.print("[bold green]Overall: no severe problems detected[/bold green]")


@click.group(invoke_without_command=True)
@click.version_option(VERSION)
@click.option("--json", "output_json", is_flag=True, default=False, help="Output results as JSON.")
@click.option(
    "--add-exclusions",
    is_flag=True,
    default=False,
    help="Prompt to add recommended folders to the Time Machine exclusion list.",
)
@click.pass_context
def cli(ctx: click.Context, output_json: bool, add_exclusions: bool) -> None:
    """
    Inspect a macOS Time Machine destination.

    Run without a subcommand to perform read-only diagnostics.
    Use the watch subcommand to monitor backup progress live.
    """

    if ctx.invoked_subcommand is not None:
        return

    destinations = get_destinations()
    overall_exit = 0

    if output_json:
        reports = []
        for destination in destinations:
            report = collect_report(destination)
            reports.append(report_to_dict(report))
        click.echo(json.dumps(reports, indent=2))
        raise SystemExit(0)

    console.print()
    console.print(f"[bold blue]Time Machine Doctor[/bold blue] [dim]v{VERSION}[/dim]")
    console.print()

    reports = [collect_report(dest) for dest in destinations]

    for report in reports:
        render_summary(report)
        console.print()

        render_backup_status(report.backup_status)

        exit_status = render_housekeeping(report)
        overall_exit = max(overall_exit, exit_status)

        console.print()

    monitor = overall_exit > 0 and all(_is_monitor_state(r) for r in reports)
    render_overall_status(overall_exit, monitor=monitor)

    for report in reports:
        render_checks(report)
        render_advice(report)

    if add_exclusions:
        apply_exclusions(reports)

    raise SystemExit(overall_exit)


def _build_watch_panel(destinations: list[Destination], status: BackupStatus) -> Panel:
    """Build the Live display panel for the watch command."""
    text = Text()

    for dest in destinations:
        text.append(dest.name or "Unknown", style="bold")
        text.append("  ")
        mount_label = "mounted" if dest.mounted else "not mounted"
        mount_style = "green" if dest.mounted else "yellow"
        text.append(mount_label, style=mount_style)
        text.append("\n")

    text.append("\n")

    if not status.running:
        text.append("No backup currently running", style="dim")
    else:
        text.append("Phase  ", style="bold")
        text.append(status.phase or "Unknown")

        if status.percent is not None and status.percent >= 0:
            pct = min(max(status.percent, 0.0), 1.0)
            filled = int(pct * 40)
            bar = "█" * filled + "░" * (40 - filled)
            text.append(f"\n  {bar} {pct * 100:.1f}%")

        if status.time_remaining is not None:
            text.append(f"\n  ETA: {format_duration(status.time_remaining)}", style="dim")

    return Panel(text, title="Time Machine Watch", border_style="blue", width=DEFAULT_WIDTH)


@cli.command()
@click.option(
    "--interval",
    "-i",
    default=3,
    show_default=True,
    help="Refresh interval in seconds.",
)
def watch(interval: int) -> None:
    """Watch Time Machine backup progress in real time."""

    destinations = get_destinations()
    status = get_backup_status()

    try:
        with Live(
            _build_watch_panel(destinations, status),
            refresh_per_second=1,
            console=console,
        ) as live:
            while True:
                time.sleep(interval)
                destinations = get_destinations()
                status = get_backup_status()
                live.update(_build_watch_panel(destinations, status))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
