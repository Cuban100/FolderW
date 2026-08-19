<p align="center"> <img src="https://github.com/Cuban100/FolderW/blob/main/logo.png" alt="FolderW Logo" width="200" height="200" style="vertical-align:middle; margin-right: 100px;"> </p>
<h1 align="center">FolderW</h1>
<p align="center">A Python incremental/differential backup tool for Linux — Tkinter setup GUI, a web dashboard, and event-driven (watchdog) or scheduled backups via <code>rsync</code>.</p>
<p align="center"><em>The name: <strong>Folder</strong> + <strong>W</strong>atchdog — the "W" is for the watchdog that watches your source folder for changes.</em></p>

> **Platform support:** FolderW currently works on **Linux only**. It relies on `rsync` and other Linux-specific tooling, and hasn't been tested on macOS or Windows. `install.sh` checks for `rsync` and `tkinter` and installs them automatically if missing (apt/dnf/yum/pacman/zypper/apk supported) — no manual prerequisite install needed.

> **Source scope:** For now, `SRC_DIR` must be a local path — either on the machine's internal drive or an external/USB drive, as long as it's already mounted before FolderW starts. Network shares and remote/cloud sources aren't currently supported.

<p align="center"> <img src="https://github.com/Cuban100/FolderW/blob/main/screenshot-dashboard.png" alt="FolderW dashboard screenshot" width="800"> </p>

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
15. **One-Click Update:** The dashboard detects when there are new commits upstream and shows what changed — click Update Now to pull, update dependencies, and restart, no terminal needed. See [Operations](docs/operations.md#updating).
16. **Recovery History:** Dashboard page listing every restore ever run — timestamp, source snapshot, restore type, files, destination.
17. **Find File:** Search for a file or folder by name across every backup and snapshot at once — see exactly which one(s) it's in, and restore just that item to a new Recovery Folder or straight back into your source directory.
18. **Watchdog Delay:** Adjustable slider (Settings, event-driven mode only) for how long the watchdog waits after a change before backing up, and the ceiling on how long continuous activity can postpone one — reflected live on the dashboard.
19. **Cloud Backup:** Optionally mirror your source folder to any [rclone](https://rclone.org)-supported remote (Google Drive, Amazon, Dropbox, Backblaze, and 40+ others) right after each successful local backup — scheduled, watchdog, or manual. Only the current state syncs, not the local Snapshot history (cloud storage doesn't understand the hardlinks that make Snapshots cheap locally, so this keeps the cloud copy fast and small instead of re-uploading every snapshot as a full independent copy). Add a new remote and connect it with a "Log In via Browser" button — no terminal or manual `rclone config` needed for supported services (Google Drive, Dropbox, OneDrive, Box, pCloud); FolderW never handles cloud credentials itself, it just drives rclone's own login flow.
20. **English/Spanish Language Switching:** Switch the entire dashboard between English and Spanish from the footer, no restart needed — a global setting for the whole install.
21. **Cloud Sync Stats:** A dedicated Cloud Sync section on both the Dashboard and Statistics pages — files transferred, data transferred, duration, average speed, and a history of recent syncs, separate from local backup stats.

## FolderW vs. Timeshift

FolderW's snapshot mechanism (full backup + `--link-dest` snapshots, unchanged files hardlinked instead of copied) is directly inspired by [Timeshift](https://github.com/linuxmint/timeshift). The difference is scope: Timeshift is built for whole-system rollback (`/`, typically excluding `/home`); FolderW is built to watch and back up an arbitrary folder, with a web dashboard on top. That leads to a few things Timeshift doesn't do:

| | FolderW | Timeshift |
|---|---|---|
| Backup target | Any folder you choose | The system (root filesystem) |
| Access | Web dashboard, reachable from any device on the network | Local GUI/CLI only |
| Trigger | Event-driven watchdog (backs up shortly after a change) or scheduled | Scheduled or manual only |
| Browse snapshots | Browse any snapshot's contents right in the dashboard, drill into folders | No built-in file browser |
| Restore | Restore everything, or browse/search and hand-pick individual files | Whole-snapshot restore |
| Find File | Search a file/folder by name across every snapshot at once | Not built in |
| Hooks | Pre/post-backup scripts, testable from the dashboard | Not built in |
| Notifications | Desktop + external (see [Login & Notifications](docs/login-and-notifications.md)) | Not built in |
| History | Every backup and every restore ever run, logged and browsable | Not built in |
| Cloud Backup | Optional mirror to any rclone-supported remote after each backup | Not built in |

Timeshift still wins for its actual use case — full OS state, boot-time restore, no setup beyond installing it. FolderW is for the case Timeshift isn't aimed at: keep watch over a specific folder and make it easy to see, search, and restore from anywhere.

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
- **[Restore & Backup Method](docs/restore-and-backup-method.md)** — how restoring works, Incremental vs Differential, snapshot retention, merging snapshots, finding a file across every snapshot
- **[What Gets Backed Up](docs/backup-scope.md)** — excluding your own files, nothing excluded by default
- **[Backup Hooks](docs/backup-hooks.md)** — running your own scripts before/after each backup
- **[Permissions](docs/permissions.md)** — why `rsync` runs as root, and what ownership/mode backed-up files get

---

FolderW aims to streamline backup operations — a reliable, user-friendly solution for both regular and event-driven file backups.
