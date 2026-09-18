# TODO

Living roadmap for tm-doctor. Owned by Claude as a status document.

---

## v0.1 -- Complete what's started

### Foundation (broken or missing today)

- [x] Add `[project.optional-dependencies]` `dev` extras to pyproject.toml
      (pytest, ruff -- so `pip install -e '.[dev]'` actually works)
- [x] Replace placeholder GitHub URLs in pyproject.toml
- [x] Fix ruff style violation: `DEFAULT_WIDTH=78` -> `DEFAULT_WIDTH = 78`
- [x] Create `tests/` directory

### Tests (unit, subprocess mocked)

- [x] `parse_backup_timestamp` -- valid path, malformed path
- [x] `parse_transaction` -- `.previous`, `.interrupted`, unknown suffix
- [x] `parse_tmutil_status` -- running backup, idle, unavailable
- [x] `transaction_stats` -- counts and state distribution
- [x] `get_destination` -- parse a real tmutil destinationinfo fixture
- [x] `get_backups` -- parse a real tmutil listbackups fixture

### Capability gaps

- [x] Support multiple destinations (`tmutil destinationinfo` can return more
      than one; parse and report all of them)
- [x] Add `--json` flag: serialize `DoctorReport` to JSON without touching
      diagnostic logic
- [x] Add an Advice panel rendered after the Overall block when actionable
      guidance is available. First trigger: `Kind == "Local"` + severely
      degraded `.interrupted` count. Advice text should be cautious --
      "if your connection path is complex (e.g. through a dock or USB hub),
      try connecting the drive directly to see if interruptions decrease."
      Do not assert a dock is present; we cannot observe that.

---

## v0.2 -- Restore verification

### Tracer bullet (thin slice, all layers, prove the architecture first)

- [ ] `mount_snapshot(destination, snapshot_name, mountpoint)` -- wraps
      `mount_apfs -o rdonly,nobrowse -s <snapshot> <volume> <mountpoint>`
- [ ] `discover_source_volume(mountpoint)` -- find the backed-up source dir
      inside the snapshot (do not hardcode "Data" or "Macintosh HD - Data")
- [ ] `map_source_path(source_path, snapshot_root, source_volume)` -- resolve
      a user-supplied path to its location inside the mounted snapshot
- [ ] `restore_file(src, dest)` -- rsync a single file to a temp destination
- [ ] `verify_restored_file(original, restored)` -- compare size + mtime
- [ ] `unmount_snapshot(mountpoint)` -- clean teardown
- [ ] Wire up as `tm-doctor restore <path> [--destination <id>]` subcommand
- [ ] Render: show what was restored, where, and whether verification passed

### Fill-in (after tracer is green)

- [ ] Directory restoration (rsync tree)
- [ ] Progress reporting during rsync
- [ ] Never overwrite existing destination without `--overwrite` flag
- [ ] `--json` output for restore results
- [ ] Tests: mock subprocess calls for mount/unmount/rsync

---

## v0.3 -- APFS filesystem verification

### Tracer bullet

- [ ] `unlock_volume(volume)` -- `diskutil apfs unlockVolume <v> -nomount`
      (encrypted volumes only; detect and skip if unencrypted)
- [ ] `run_fsck(device)` -- `fsck_apfs -n /dev/r<device>` (read-only, never -y)
- [ ] `parse_fsck_output(raw)` -- domain model for errors/warnings/clean
- [ ] `render_fsck_result(result)` -- healthy / attention / incomplete
- [ ] Wire up as `tm-doctor fsck [--destination <id>]` subcommand

### Fill-in

- [ ] Handle already-mounted volumes
- [ ] Integrate fsck status into main `tm-doctor` report (opt-in flag only --
      unmounting may be disruptive)
- [ ] `--json` output
- [ ] Tests

---

## Backlog

Unscheduled -- not committed to any version:

- [ ] Historical snapshot browser (`tm-doctor snapshots`)
- [ ] Multiple destination rotation health (are all destinations up to date?)
- [ ] Backup size trending over time
- [ ] `tm-doctor --watch` live status (poll tmutil status)
- [ ] Restore from a specific historical backup (not just latest)
