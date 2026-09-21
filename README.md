# Time Machine Doctor

[![CI](https://github.com/hardyoyo/tm-doctor/actions/workflows/ci.yml/badge.svg)](https://github.com/hardyoyo/tm-doctor/actions/workflows/ci.yml)

A diagnostic and recovery toolkit for macOS Time Machine.

Time Machine is very good at making backups, but macOS exposes surprisingly
little information about whether those backups are healthy, what they contain,
or whether they can actually be restored.

`tm-doctor` aims to answer questions like:

- Is Time Machine configured and available?
- When was the last successful backup?
- Is a backup currently running, and what phase is it in?
- Does the backup destination show signs of broken housekeeping?
- Can a historical backup actually be mounted and read?
- Can a file or directory be restored and verified?
- Is the underlying APFS filesystem healthy?

The goal is not to replace Time Machine. The goal is to make Time Machine
observable and testable.

## Status

Early development.

Version 0.1 performs read-only diagnostics only. It does not modify, repair,
mount, unmount, or delete anything.

Current diagnostics include:

- Time Machine destination discovery
- mounted/offline destination status
- retained backup inventory
- latest backup
- current backup phase and progress
- `.previous` and `.interrupted` transaction-object analysis
- `com.apple.backupd.SnapshotState` distribution
- detection of unusually large housekeeping backlogs

Future work is expected to include:

- APFS filesystem verification
- historical snapshot browsing
- restore testing
- verified restores using read-only APFS snapshots and `rsync`
- JSON output
- improved health assessments

## Installation

Requires macOS and Python 3.11 or newer.

Clone the repository and create a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

For development:

```bash
pip install -e '.[dev]'
```

Then:

```bash
tm-doctor
```

## Example

```text
Time Machine Doctor v0.1.0

╭──────────────────────── Time Machine ────────────────────────╮
│ Destination  HJPtimeMachine                                 │
│ Kind         Local                                          │
│ Status       Mounted                                        │
│ Latest       2026-08-18 14:47:25                            │
│ Backups      50                                             │
╰─────────────────────────────────────────────────────────────╯

                       Housekeeping
┏━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━┓
┃ Type         ┃ Count ┃ State 1 ┃ State 16 ┃ Oldest              ┃
┡━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━┩
│ .previous    │ 1,433 │       1 │    1,432 │ 2026-02-06 04:31:48 │
│ .interrupted │ 1,401 │       0 │    1,401 │ 2026-02-05 17:48:41 │
└──────────────┴───────┴─────────┴──────────┴─────────────────────┘

⚠ Housekeeping appears severely degraded.
```

## Safety

Backup software deserves an unusually conservative approach.

The default behavior of `tm-doctor` should be read-only. Diagnostic commands
must not silently modify a Time Machine destination.

In particular:

- historical APFS snapshots should be mounted read-only
- filesystem verification should default to non-repairing checks
- destructive or repair operations must never happen implicitly
- an unavailable check must not be reported as a successful check
- private/undocumented Apple metadata should be reported as observed rather
  than assigned invented semantics

A backup should not be considered proven merely because Time Machine reports
that it completed. A successful restore is stronger evidence.

## Platform

`tm-doctor` targets modern APFS-based Time Machine on macOS.

It is not intended to support the older HFS+ `Backups.backupdb` architecture.

## Dependencies

Runtime:

- [Click](https://click.palletsprojects.com/)
- [Rich](https://rich.readthedocs.io/)

macOS tools used include:

- `tmutil`
- `plutil`
- `xattr`
- `diskutil`
- `pmset`
- `ioreg`

Future functionality may also use:

- `fsck_apfs`
- `mount_apfs`
- `rsync`

## License

MIT

## Further reading and prior art

`tm-doctor` builds on macOS's existing Time Machine and APFS machinery rather
than implementing a backup system of its own.

The following resources have been particularly useful while investigating
modern APFS Time Machine behavior.

### Howard Oakley / The Eclectic Light Company

Howard Oakley has written extensively about APFS, Time Machine, snapshots, and
the macOS unified log.

- [The Time Machine Mechanic (T2M2)](https://eclecticlight.co/downloads/) —
  analyzes Time Machine activity in the unified log and surfaces errors that
  aren't readily visible in the standard Time Machine UI.

- [Understand and check Time Machine backups to APFS](https://eclecticlight.co/2024/10/03/understand-and-check-time-machine-backups-to-apfs/) —
  overview of how modern Time Machine backups operate and how to assess them.

- [Time Machine to APFS: maintenance and repair](https://eclecticlight.co/2021/03/25/time-machine-to-apfs-maintenance-and-repair/) —
  discussion of APFS Time Machine backup maintenance, snapshots, and tools for
  inspecting backup contents.

- [Going beyond T2M2 with Mints](https://eclecticlight.co/2021/09/29/going-beyond-t2m2-with-mints-grokking-time-machine-to-apfs/) —
  deeper examination of Time Machine's unified-log activity.

- [APFS command tools](https://eclecticlight.co/2024/04/22/apfs-command-tools/) —
  useful reference for working with APFS from the command line, including
  filesystem verification and encrypted volumes.

### Nathaniel Wu's APFS Time Machine restore script

Nathaniel Wu documented a practical command-line method for restoring from
modern APFS Time Machine backups:

- [Restore from an APFS Time Machine backup](https://gist.github.com/Nathaniel-Wu/21cd075f0534cc5c4bb24314a9c9b11e)

The general technique is:

```text
discover Time Machine destination
        ↓
select historical APFS snapshot
        ↓
mount snapshot read-only with mount_apfs
        ↓
locate the backed-up source volume
        ↓
restore with rsync
        ↓
unmount
```

This is important prior art for the restore functionality planned for
`tm-doctor`.

The script should be treated as a demonstration of the mechanism rather than
a library or specification. In particular, `tm-doctor` should discover volume
names rather than assuming `Macintosh HD - Data`, and should use conservative
mount/unmount and cleanup behavior.

### Apple command-line tools

Several macOS tools expose the underlying mechanisms used by this project:

- `tmutil` — Time Machine configuration, backup inventory, status, and control
- `diskutil` — APFS volumes, snapshots, mounting, unlocking, and verification
- `mount_apfs` — including read-only mounting of named APFS snapshots
- `fsck_apfs` — APFS filesystem verification and repair
- `plutil` — useful for parsing the property-list-like output of `tmutil status`
- `xattr` — inspection of Time Machine's extended metadata

The local macOS manual pages are often the best reference for the exact
versions of these tools installed on a particular Mac:

```bash
man tmutil
man diskutil
man mount_apfs
man fsck_apfs
man xattr
```

### BackupLoupe

[BackupLoupe](https://www.soma-zone.com/BackupLoupe/) is a commercial graphical
browser and analysis tool for Time Machine backups. It is useful prior art for
the kinds of questions users need to answer about backup contents and history,
although `tm-doctor` is intended to provide scriptable diagnostics and
demonstrable restore verification from the command line.

### A note on undocumented behavior

Some Time Machine implementation details are private Apple interfaces.

For example, Time Machine backup transaction objects may contain extended
attributes such as:

```text
com.apple.backupd.SnapshotState
com.apple.backupd.SnapshotStartDate
com.apple.backupd.SnapshotCompletionDate
```

These are useful diagnostic observations, but their internal values should not
be assigned semantic names unless supported by authoritative documentation or
strong independent evidence.

Where this project relies on experimentally observed behavior, the code and
documentation should say so.
