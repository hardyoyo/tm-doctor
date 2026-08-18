# CLAUDE.md

## Project

`tm-doctor` is a macOS Time Machine diagnostic and recovery toolkit.

The project exists because Time Machine exposes useful low-level machinery but
does not give users a good way to answer the most important backup questions:

- Is it working?
- What is being backed up?
- Is the backup destination healthy?
- Can I actually restore my data?

The tool should make Time Machine observable, understandable, and testable.

## Design philosophy

### Prefer evidence over assumptions

Time Machine and APFS contain undocumented behavior.

When dealing with private Apple metadata, report what is observed rather than
inventing semantic meanings.

For example, we have experimentally observed
`com.apple.backupd.SnapshotState` values `"1"` and `"16"`.

We have observed a `.previous` object transition from state `"1"` to `"16"`
after the next backup.

Do NOT turn that observation into names such as `ACTIVE`, `STALE`,
`DELETE_PENDING`, etc. unless authoritative evidence establishes those
semantics.

### Diagnosis and repair are separate concerns

The normal operation of `tm-doctor` should be read-only.

A diagnostic command must never unexpectedly:

- delete backup objects
- alter ACLs or extended attributes
- repair an APFS filesystem
- remount a filesystem read/write
- erase or reformat storage

If repair functionality is ever added, it must be explicit and clearly
separated from diagnostic commands.

### Unknown is not healthy

If a diagnostic cannot run, report that it is unavailable or incomplete.

Do not turn:

```text
could not inspect backup
```

into:

```text
0 problems found
```

Health reporting should distinguish at least:

- healthy
- attention needed
- incomplete / unknown

### Prove recovery

"Latest backup completed successfully" is useful but insufficient evidence.

Where practical, the strongest health check is:

1. locate a historical backup
2. mount its APFS snapshot read-only
3. restore data to an independent location
4. verify the restored data

Restore verification should be a first-class capability of the project.

## Known APFS Time Machine behavior

Modern Time Machine destinations use APFS snapshots.

A historical backup snapshot can be mounted read-only using approximately:

```bash
mount_apfs \
    -o rdonly,nobrowse \
    -s com.apple.TimeMachine.<timestamp>.backup \
    <time-machine-volume> \
    <temporary-mountpoint>
```

The mounted snapshot contains a backup directory and a source-volume
directory, for example:

```text
<mountpoint>/
    2026-08-10-060624.backup/
        Data/
            Users/
                ...
```

Do not hardcode `Data` or `Macintosh HD - Data`. Discover the backed-up
source volume.

Nathaniel Wu's APFS Time Machine restore script is useful prior art. Its
general architecture is:

```text
tmutil destinationinfo
        ↓
tmutil listbackups
        ↓
mount_apfs read-only
        ↓
map source path into backup
        ↓
rsync
        ↓
unmount
```

Use the architecture as prior art, not as code that must be copied verbatim.

## Restore implementation

Prefer `rsync` for filesystem restoration.

During initial investigation:

- `tmutil restore` blocked when used against a manually mounted APFS snapshot
- `ditto` produced incomplete `.BC.T_*` temporary files in a restored Git
  repository
- `cp` / `cp -R` successfully recovered the same data
- Nathaniel Wu's restore implementation uses `rsync`

The user knows and trusts `rsync`.

Historical snapshots must be mounted read-only.

Never overwrite an existing destination without explicit user intent.

## Time Machine status

`tmutil status` emits:

```text
Backup session status:
{
    ...
}
```

The body is an old-style property list.

The heading can be removed and the remainder converted using macOS `plutil`:

```bash
tmutil status |
    sed '1d' |
    plutil -convert json -o - -
```

In Python, avoid depending on `sed`; remove the first line directly and pass
the remaining text to `plutil`.

Useful observed fields include:

```text
Running
BackupPhase
Progress.Percent
Progress.TimeRemaining
Progress.bytes
Progress.files
Progress.totalBytes
Progress.totalFiles
FractionOfProgressBar
```

Do not assume `Progress.Percent` equals `bytes / totalBytes` or
`files / totalFiles`. Real observations show that it does not.

Observed phases include:

```text
Copying
ThinningPostBackup
```

## Transaction objects

APFS Time Machine destinations may contain root-level objects ending in:

```text
.previous
.interrupted
```

These are Time Machine transaction/state objects.

Relevant extended attributes include:

```text
com.apple.backupd.SnapshotState
com.apple.backupd.SnapshotStartDate
com.apple.backupd.SnapshotCompletionDate
com.apple.backupd.SnapshotTotalBytesCopied
com.apple.timemachine.private.backup.state
```

Do not assume `.previous` or `.interrupted` is inherently erroneous.
Accumulation is the diagnostic signal.

One investigated broken destination contained approximately:

```text
49-50 retained backups
~1,400 .previous objects
~1,400 .interrupted objects
~2,800 state-16 transaction objects
```

Time Machine spent roughly an hour in `ThinningPostBackup` and reported
roughly the same number of `afpAccessDenied (-5000)` deletion errors.

This is useful evidence for detecting pathological housekeeping, but thresholds
should remain conservative until more healthy systems have been sampled.

## Filesystem health

APFS filesystem verification is potentially disruptive because the volume may
need to be unmounted.

Do not include filesystem verification as an implicit part of ordinary
diagnostics.

For encrypted APFS volumes, an offline read-only check may require:

```bash
diskutil apfs unlockVolume <volume> -nomount
sudo fsck_apfs -n /dev/r<volume>
```

`-n` is important: diagnostic verification must not perform repairs.

Never automatically escalate from verification to `fsck_apfs -y`,
`diskutil repairVolume`, erase, or reformat.

## CLI architecture

Use Click for CLI behavior.

Use Rich for human-readable terminal presentation.

Keep these concerns separate:

```text
macOS interrogation
        ↓
domain models / analysis
        ↓
rendering
```

CLI functions should orchestrate. They should not contain the core diagnostic
logic.

Prefer dataclasses or similarly explicit domain models for observations and
assessments.

Design the internal API so that future output formats such as:

```bash
tm-doctor --json
```

do not require rewriting diagnostic logic.

## Python style

Target Python 3.11 or newer.

Prefer:

- type annotations
- dataclasses
- pathlib
- small focused functions
- descriptive names
- standard library functionality where practical

Use subprocess argument lists rather than shell command strings.

Avoid `shell=True`.

Check subprocess return codes.

Distinguish "no results" from "could not query."

Do not introduce GNU command dependencies when Python or native macOS tools
can perform the operation.

## Dependencies

Runtime dependencies should remain modest.

Current intended runtime dependencies:

```text
click
rich
```

Before adding another dependency, consider whether the standard library or a
native macOS tool already provides the required functionality.

## Development approach

Develop incrementally.

Prefer a small working capability tested against real Time Machine behavior
over a large speculative implementation.

When adding a diagnostic:

1. establish what macOS actually reports
2. capture representative output
3. model that information
4. implement parsing
5. render it
6. test failure/unavailable states
7. only then derive a health assessment

Do not build repair behavior merely because a diagnostic identifies a problem.

## Current scope

Version 0.1 is read-only.

It currently focuses on:

- configured destination discovery
- mounted/offline state
- backup inventory
- latest backup
- current backup status
- transaction-object enumeration
- SnapshotState distribution
- housekeeping backlog detection

Restore testing and APFS verification are future capabilities.

Keep v0.1 understandable.
