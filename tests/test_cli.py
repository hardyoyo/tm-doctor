"""Unit tests for tm_doctor.cli parsing functions."""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from tm_doctor.cli import (
    BackupStatus,
    Destination,
    DoctorReport,
    TransactionObject,
    _interrupted_advice_applies,
    collect_report,
    get_backups,
    get_destination,
    get_destinations,
    parse_backup_timestamp,
    parse_tmutil_status,
    parse_transaction,
    report_to_dict,
    transaction_stats,
)


# ---------------------------------------------------------------------------
# parse_backup_timestamp
# ---------------------------------------------------------------------------


def test_parse_backup_timestamp_valid():
    path = Path("/Volumes/TM/Backups.backupdb/2024-03-15-120000.backup")
    result = parse_backup_timestamp(path)
    assert result == datetime(2024, 3, 15, 12, 0, 0)


def test_parse_backup_timestamp_none():
    assert parse_backup_timestamp(None) is None


def test_parse_backup_timestamp_no_match():
    path = Path("/some/path/without-a-timestamp")
    assert parse_backup_timestamp(path) is None


def test_parse_backup_timestamp_nested_path():
    path = Path("/Volumes/TM/Macintosh HD/2025-01-01-060000.backup/Data")
    result = parse_backup_timestamp(path)
    assert result == datetime(2025, 1, 1, 6, 0, 0)


# ---------------------------------------------------------------------------
# parse_transaction
# ---------------------------------------------------------------------------


def _make_transaction_path(name: str) -> Path:
    return Path("/Volumes/TM") / name


@patch("tm_doctor.cli.read_snapshot_state", return_value="1")
def test_parse_transaction_previous(mock_state):
    path = _make_transaction_path("2024-03-15-120000.previous")
    result = parse_transaction(path)
    assert result is not None
    assert result.kind == "previous"
    assert result.timestamp == datetime(2024, 3, 15, 12, 0, 0)
    assert result.snapshot_state == "1"


@patch("tm_doctor.cli.read_snapshot_state", return_value="16")
def test_parse_transaction_interrupted(mock_state):
    path = _make_transaction_path("2024-06-01-080000.interrupted")
    result = parse_transaction(path)
    assert result is not None
    assert result.kind == "interrupted"
    assert result.snapshot_state == "16"


def test_parse_transaction_unknown_suffix():
    path = _make_transaction_path("2024-03-15-120000.something")
    assert parse_transaction(path) is None


def test_parse_transaction_no_timestamp():
    path = _make_transaction_path("notavalidname.previous")
    assert parse_transaction(path) is None


# ---------------------------------------------------------------------------
# parse_tmutil_status
# ---------------------------------------------------------------------------


TMUTIL_STATUS_RUNNING = """\
Backup session status:
{
    Running = 1;
    BackupPhase = Copying;
    Progress = {
        Percent = "0.42";
        TimeRemaining = 120;
    };
}
"""

TMUTIL_STATUS_IDLE = """\
Backup session status:
{
    Running = 0;
}
"""

TMUTIL_STATUS_THINNING = """\
Backup session status:
{
    Running = 1;
    BackupPhase = ThinningPostBackup;
}
"""


def _make_completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def test_parse_tmutil_status_running():
    with patch("tm_doctor.cli.run_command") as mock_run:
        def side_effect(*args, **kwargs):
            if args[0] == "tmutil":
                return _make_completed(TMUTIL_STATUS_RUNNING)
            # plutil conversion -- simulate it
            lines = kwargs.get("stdin", "").strip()
            # parse plist-style manually for test
            data = {"Running": 1, "BackupPhase": "Copying", "Progress": {"Percent": "0.42", "TimeRemaining": 120}}
            return _make_completed(json.dumps(data))
        mock_run.side_effect = side_effect
        result = parse_tmutil_status()
    assert result.get("Running") == 1
    assert result.get("BackupPhase") == "Copying"


def test_parse_tmutil_status_unavailable():
    with patch("tm_doctor.cli.run_command") as mock_run:
        mock_run.return_value = _make_completed("", returncode=1)
        result = parse_tmutil_status()
    assert result == {}


def test_parse_tmutil_status_too_short():
    with patch("tm_doctor.cli.run_command") as mock_run:
        mock_run.return_value = _make_completed("only one line")
        result = parse_tmutil_status()
    assert result == {}


# ---------------------------------------------------------------------------
# transaction_stats
# ---------------------------------------------------------------------------


def _tx(kind: str, state: str | None) -> TransactionObject:
    return TransactionObject(
        path=Path(f"/Volumes/TM/2024-01-01-120000.{kind}"),
        timestamp=datetime(2024, 1, 1, 12, 0, 0),
        kind=kind,
        snapshot_state=state,
    )


def test_transaction_stats_filters_by_kind():
    txs = [
        _tx("previous", "1"),
        _tx("previous", "16"),
        _tx("interrupted", "1"),
    ]
    selected, states = transaction_stats(txs, "previous")
    assert len(selected) == 2
    assert states["1"] == 1
    assert states["16"] == 1


def test_transaction_stats_unknown_state():
    txs = [_tx("previous", None)]
    _, states = transaction_stats(txs, "previous")
    assert states["unknown"] == 1


def test_transaction_stats_empty():
    selected, states = transaction_stats([], "previous")
    assert selected == []
    assert states == Counter()


# ---------------------------------------------------------------------------
# get_destination
# ---------------------------------------------------------------------------

DESTINATIONINFO_OUTPUT = """\
Name          : My Backup Drive
Kind          : Local
Mount Point   : /Volumes/TM-Backup
ID            : 1A2B3C4D-1234-5678-ABCD-000000000001
"""

DESTINATIONINFO_NO_MOUNT = """\
Name          : Remote NAS
Kind          : Network
ID            : 1A2B3C4D-1234-5678-ABCD-000000000002
"""


def test_get_destination_mounted():
    with patch("tm_doctor.cli.run_command") as mock_run:
        mock_run.return_value = _make_completed(DESTINATIONINFO_OUTPUT)
        dest = get_destination()
    assert dest.name == "My Backup Drive"
    assert dest.kind == "Local"
    assert dest.destination_id == "1A2B3C4D-1234-5678-ABCD-000000000001"
    assert dest.mount_point == Path("/Volumes/TM-Backup")
    assert dest.mounted is True


def test_get_destination_not_mounted():
    with patch("tm_doctor.cli.run_command") as mock_run:
        mock_run.return_value = _make_completed(DESTINATIONINFO_NO_MOUNT)
        dest = get_destination()
    assert dest.mounted is False
    assert dest.mount_point is None


# ---------------------------------------------------------------------------
# get_backups
# ---------------------------------------------------------------------------

LISTBACKUPS_OUTPUT = """\
/Volumes/TM/Backups.backupdb/2024-01-01-060000.backup
/Volumes/TM/Backups.backupdb/2024-01-02-060000.backup
/Volumes/TM/Backups.backupdb/2024-01-03-060000.backup
"""


def test_get_backups_returns_paths():
    with patch("tm_doctor.cli.run_command") as mock_run:
        mock_run.return_value = _make_completed(LISTBACKUPS_OUTPUT)
        backups = get_backups()
    assert backups is not None
    assert len(backups) == 3
    assert backups[0] == Path("/Volumes/TM/Backups.backupdb/2024-01-01-060000.backup")


def test_get_backups_unavailable():
    with patch("tm_doctor.cli.run_command") as mock_run:
        mock_run.return_value = _make_completed("", returncode=1)
        result = get_backups()
    assert result is None


def test_get_backups_empty():
    with patch("tm_doctor.cli.run_command") as mock_run:
        mock_run.return_value = _make_completed("")
        result = get_backups()
    assert result == []


# ---------------------------------------------------------------------------
# get_destinations (plural)
# ---------------------------------------------------------------------------

DESTINATIONINFO_SINGLE = """\
====================================================
Name          : HJPtimeMachine2
Kind          : Local
Mount Point   : /Volumes/HJPtimeMachine2
ID            : 59C56E10-A759-462E-9539-3A2961202926

"""

DESTINATIONINFO_MULTI = """\
====================================================
Name          : Primary Backup
Kind          : Local
Mount Point   : /Volumes/Primary
ID            : AAAAAAAA-0000-0000-0000-000000000001

====================================================
Name          : Offsite NAS
Kind          : Network
ID            : BBBBBBBB-0000-0000-0000-000000000002

"""


def test_get_destinations_single():
    with patch("tm_doctor.cli.run_command") as mock_run:
        mock_run.return_value = _make_completed(DESTINATIONINFO_SINGLE)
        dests = get_destinations()
    assert len(dests) == 1
    assert dests[0].name == "HJPtimeMachine2"
    assert dests[0].kind == "Local"
    assert dests[0].mount_point == Path("/Volumes/HJPtimeMachine2")
    assert dests[0].destination_id == "59C56E10-A759-462E-9539-3A2961202926"


def test_get_destinations_multiple():
    with patch("tm_doctor.cli.run_command") as mock_run:
        mock_run.return_value = _make_completed(DESTINATIONINFO_MULTI)
        dests = get_destinations()
    assert len(dests) == 2
    assert dests[0].name == "Primary Backup"
    assert dests[0].mounted is True
    assert dests[1].name == "Offsite NAS"
    assert dests[1].mounted is False


def test_get_destinations_unavailable():
    with patch("tm_doctor.cli.run_command") as mock_run:
        mock_run.return_value = _make_completed("error output", returncode=1)
        with pytest.raises(Exception):
            get_destinations()


# ---------------------------------------------------------------------------
# collect_report
# ---------------------------------------------------------------------------

_MOUNTED_DEST = Destination(
    name="Test Drive",
    kind="Local",
    destination_id="TEST-0001",
    mount_point=Path("/Volumes/Test"),
)

_UNMOUNTED_DEST = Destination(
    name="Offline Drive",
    kind="Local",
    destination_id="TEST-0002",
    mount_point=None,
)


def test_collect_report_uses_supplied_destination():
    with (
        patch("tm_doctor.cli.get_backup_status") as mock_status,
        patch("tm_doctor.cli.get_backups") as mock_backups,
        patch("tm_doctor.cli.get_latest_backup") as mock_latest,
        patch("tm_doctor.cli.get_transactions") as mock_tx,
    ):
        mock_status.return_value = BackupStatus(running=False, phase=None, percent=None, time_remaining=None)
        mock_backups.return_value = []
        mock_latest.return_value = None
        mock_tx.return_value = []

        report = collect_report(_MOUNTED_DEST)

    assert report.destination is _MOUNTED_DEST


def test_collect_report_skips_data_collection_when_unmounted():
    with (
        patch("tm_doctor.cli.get_backup_status") as mock_status,
        patch("tm_doctor.cli.get_backups") as mock_backups,
    ):
        mock_status.return_value = BackupStatus(running=False, phase=None, percent=None, time_remaining=None)

        report = collect_report(_UNMOUNTED_DEST)

    mock_backups.assert_not_called()
    assert report.backups is None


# ---------------------------------------------------------------------------
# report_to_dict
# ---------------------------------------------------------------------------

_STATUS_IDLE = BackupStatus(running=False, phase=None, percent=None, time_remaining=None)


def _make_report(destination=_MOUNTED_DEST, backups=None, latest_backup=None, transactions=None, backup_status=_STATUS_IDLE):
    return DoctorReport(
        destination=destination,
        backups=backups,
        latest_backup=latest_backup,
        transactions=transactions,
        backup_status=backup_status,
    )


def test_report_to_dict_destination_fields():
    report = _make_report()
    d = report_to_dict(report)
    dest = d["destination"]
    assert dest["name"] == "Test Drive"
    assert dest["kind"] == "Local"
    assert dest["destination_id"] == "TEST-0001"
    assert dest["mount_point"] == "/Volumes/Test"
    assert dest["mounted"] is True


def test_report_to_dict_backup_status():
    status = BackupStatus(running=True, phase="Copying", percent=0.42, time_remaining=90.0)
    report = _make_report(backup_status=status)
    d = report_to_dict(report)
    bs = d["backup_status"]
    assert bs["running"] is True
    assert bs["phase"] == "Copying"
    assert bs["percent"] == 0.42
    assert bs["time_remaining"] == 90.0


def test_report_to_dict_backups():
    backups = [Path("/Volumes/TM/2024-01-01-060000.backup"), Path("/Volumes/TM/2024-01-02-060000.backup")]
    report = _make_report(backups=backups, latest_backup=backups[-1])
    d = report_to_dict(report)
    assert d["backups"] == [str(p) for p in backups]
    assert d["latest_backup"] == str(backups[-1])


def test_report_to_dict_backups_unavailable():
    report = _make_report(backups=None, latest_backup=None)
    d = report_to_dict(report)
    assert d["backups"] is None
    assert d["latest_backup"] is None


def test_report_to_dict_transactions():
    txs = [
        _tx("previous", "1"),
        _tx("interrupted", "16"),
    ]
    report = _make_report(transactions=txs)
    d = report_to_dict(report)
    assert len(d["transactions"]) == 2
    t = d["transactions"][0]
    assert t["kind"] == "previous"
    assert t["snapshot_state"] == "1"
    assert t["timestamp"] == "2024-01-01T12:00:00"


def test_report_to_dict_transactions_unavailable():
    report = _make_report(transactions=None)
    d = report_to_dict(report)
    assert d["transactions"] is None


# ---------------------------------------------------------------------------
# _interrupted_advice_applies
# ---------------------------------------------------------------------------

_LOCAL_DEST = Destination(name="Drive", kind="Local", destination_id="X", mount_point=Path("/Volumes/X"))
_NETWORK_DEST = Destination(name="NAS", kind="Network", destination_id="Y", mount_point=Path("/Volumes/Y"))


def _report_with_interrupted(destination, interrupted_state16_count, backup_count):
    txs = [_tx("interrupted", "16") for _ in range(interrupted_state16_count)]
    backups = [Path(f"/Volumes/TM/2024-01-{i:02d}-060000.backup") for i in range(1, backup_count + 1)]
    return _make_report(destination=destination, transactions=txs, backups=backups)


def test_advice_applies_local_with_high_interrupted():
    report = _report_with_interrupted(_LOCAL_DEST, interrupted_state16_count=150, backup_count=20)
    assert _interrupted_advice_applies(report) is True


def test_advice_does_not_apply_network_destination():
    report = _report_with_interrupted(_NETWORK_DEST, interrupted_state16_count=150, backup_count=20)
    assert _interrupted_advice_applies(report) is False


def test_advice_does_not_apply_below_threshold():
    # threshold = max(100, 20*5) = 100; count of 50 is below it
    report = _report_with_interrupted(_LOCAL_DEST, interrupted_state16_count=50, backup_count=20)
    assert _interrupted_advice_applies(report) is False


def test_advice_does_not_apply_no_transactions():
    report = _make_report(destination=_LOCAL_DEST, transactions=None)
    assert _interrupted_advice_applies(report) is False
