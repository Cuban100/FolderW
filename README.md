<p align="center"> <img src="https://github.com/Cuban100/FolderW/blob/main/logo.png" alt="FolderW Logo" width="200" height="200" style="vertical-align:middle; margin-right: 100px;"> </p>
<h1 align="center">FolderW</h1>
<p align="center">A Python incremental/differential backup tool for Linux — Tkinter setup GUI, a web dashboard, and event-driven (watchdog) or scheduled backups via <code>rsync</code>.</p>

> **Platform support:** FolderW currently works on **Linux only**. It relies on `rsync` and other Linux-specific tooling, and hasn't been tested on macOS or Windows.

> **Source scope:** For now, `SRC_DIR` must be a local path — either on the machine's internal drive or an external/USB drive, as long as it's already mounted before FolderW starts. Network shares and remote/cloud sources aren't currently supported.

## Features

1. **Graphical User Interface (GUI):** Tkinter-based setup — select directories, enable/disable event-driven backups, no config file editing required.
2. **Environment Configuration:** Settings stored in a `.env` file, created and managed through the setup GUI or the web Settings page.
3. **Virtual Environment Management:** `install.sh` sets up a Python virtual environment and installs dependencies automatically.
4. **Automated Backups:** Scheduled or event-driven (watches the source directory for changes), via `rsync`.
5. **Logging and Error Handling:** Comprehensive logging and robust error handling throughout.
6. **Dark-Themed GUI:** `ttkthemes` gives the Tkinter setup window a polished dark theme.
7. **Autostart on Boot:** Optional systemd user service so the dashboard starts on login/boot. See [Operations](docs/operations.md#autostart-on-boot-systemd).
8. **Restore:** Dashboard page listing the full backup and every snapshot — restore everything, or browse/search and hand-pick individual files. See [Restore & Backup Method](docs/restore-and-backup-method.md).
9. **Snapshot Retention:** Optional cap on how many snapshots to keep, oldest deleted automatically. The full backup is never part of this pool.
10. **Backup History:** Dashboard page listing every backup run ever recorded — timestamp, type, snapshot, files changed, status.
11. **Branded Backup Folder:** Both the full backup and its container folder get their own custom folder icon, recognizable at a glance instead of a plain folder.

    <p align="center"> <img src="https://github.com/Cuban100/FolderW/blob/main/folder-icon.png" alt="FolderW branded backup folder icon" width="320"> </p>

12. **Merged Snapshots:** "Compile Latest Snapshot" combines every existing snapshot's latest-per-path files into one new, independently restorable snapshot.
13. **Backup Hooks:** Optional pre-backup and post-backup scripts, run around every backup — each with a "Test Script" button on its own dashboard page to verify it before trusting it to run for real.
14. **File Exclusions:** Comma-separated patterns to skip during backup, set on the Settings page. Nothing is excluded unless you say so — no hidden defaults.

## Quick Start

```bash
git clone https://github.com/Cuban100/FolderW.git
cd FolderW
chmod +x install.sh
./install.sh
```

That runs the Tkinter setup wizard to configure your source/destination and generate `.env`. See [Usage](docs/usage.md) for what happens next.

## Documentation

- **[Usage](docs/usage.md)** — key components, how the setup wizard and dashboard fit together
- **[Operations](docs/operations.md)** — updating, uninstalling, autostart on boot (systemd)
- **[Login & Notifications](docs/login-and-notifications.md)** — dashboard password, desktop + external notifications
- **[Restore & Backup Method](docs/restore-and-backup-method.md)** — how restoring works, Incremental vs Differential, snapshot retention, merging snapshots
- **[What Gets Backed Up](docs/backup-scope.md)** — excluding your own files, nothing excluded by default
- **[Backup Hooks](docs/backup-hooks.md)** — running your own scripts before/after each backup
- **[Permissions](docs/permissions.md)** — why `rsync` runs as root, and what ownership/mode backed-up files get

---

FolderW aims to streamline backup operations — a reliable, user-friendly solution for both regular and event-driven file backups.
