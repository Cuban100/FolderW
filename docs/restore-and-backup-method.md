[← Back to README](../README.md)

## Restore

The **Restore** page (`/restore` in the dashboard) lists two kinds of backups:

- **Full Backup:** created once, on the very first backup ever, at `BASE_DIR/FULL_NAME/Full Backup` — a complete, standalone copy of `SRC_DIR` at that moment. Frozen after that: no later run ever touches it again, regardless of which Backup Method is active.
- **Snapshots:** one entry per backup session after the initial full backup, stored under `BASE_DIR/FULL_NAME/Snapshots/<Month>/<Day>/<Time>/` (e.g. `August 06, 1:09-PM`). What each one contains depends on the [Backup Method](#backup-method) that was active when it ran — a complete tree (Incremental) or only the changed files (Differential).

`FULL_NAME` (set in the setup GUI or the web Settings page as "Full Backup Folder Name") is the container folder created inside `BASE_DIR` that holds both `Full Backup/` and `Snapshots/` — e.g. with `FULL_NAME=Documents`, everything lives under `BASE_DIR/Documents/`.

## Backup Method

FolderW supports two strategies for every backup after the initial one (set in the setup GUI or the web Settings page as "Backup Method"):

- **Incremental** — each snapshot compares against the *previous* snapshot and is a complete, independently restorable copy: unchanged files are hardlinked in at no extra disk cost, changed files are written fresh. Snapshots stay small and roughly constant in size no matter how long the system has been running. This is how Time Machine and Timeshift work.
- **Differential (default)** — each snapshot compares directly against the *original* full backup and contains only the files that are new or changed since it — a true delta, not a complete copy. Snapshots grow larger over time as more changes accumulate since the original; restoring a specific point in time needs the full backup plus that one snapshot together. This is the industry-standard definition of a differential backup.

For any backup you can either:

- **Restore Entire Backup** — copies everything in it, or
- **Browse / Search** — drills into that one backup, with a search box, and lets you select individual files to restore.

Restoring **never overwrites `SRC_DIR`**. Files are always copied into a new, timestamped folder at `BASE_DIR/Restored/<timestamp>_<backup>/` for you to review and move back into place yourself — a wrong pick can't clobber current data.

## Snapshot Retention

Set **"Snapshots to Keep"** (in the setup GUI or the web Settings page) to a number, and after every backup FolderW deletes the oldest snapshots beyond that count — keeping only the most recent N. Leave it blank to keep every snapshot forever (the default).

This only ever deletes `Month/Day/Time` folders under `BASE_DIR/FULL_NAME/Snapshots`; the full backup is never part of this retention pool at all (not just protected within it) — it's a one-time backup, not something new runs add to.

Deleting a file from `SRC_DIR` behaves differently depending on which backup you look at:

- **Full backup:** frozen after its one-time creation, so it keeps whatever it captured then, regardless of later deletions from `SRC_DIR`.
- **Incremental snapshots:** deletions are never recorded in a snapshot — a deleted file simply stops appearing in *future* snapshots, but stays intact in whichever earlier snapshot last captured it, so you can still recover it from there via Restore.
- **Differential snapshots:** since each one only contains what's new or changed relative to the full backup, a file deleted from `SRC_DIR` simply won't appear in that snapshot's delta — it's still recoverable from the full backup itself.
