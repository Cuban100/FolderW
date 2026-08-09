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

Restoring from this page **never overwrites `SRC_DIR`**. Files are always copied into a new, timestamped folder at `BASE_DIR/Restored/<timestamp>_<backup>/` for you to review and move back into place yourself — a wrong pick can't clobber current data. (Find File, below, is the one deliberate exception to this.)

### Full Restore vs. Only Snapshot

A Differential snapshot (or a Merged one, see below) only ever contains a *delta* — not a complete, standalone copy of `SRC_DIR`. Restoring one by itself only gives you back what changed, not everything else. For any backup where that applies, Restore offers two options:

- **Full Restore (default)** — copies the full backup first, then that snapshot's files on top, overwriting anything that changed. The result is a complete point-in-time copy.
- **Only Snapshot** (the dropdown next to Full Restore) — copies just that snapshot's own delta, nothing from the full backup. Useful if you specifically want to see what changed in that one session, not a full tree.

Incremental snapshots don't show this choice at all — each one is already a complete, independently restorable tree (see [Backup Method](#backup-method) below), so there's nothing to combine.

### Compiling Snapshots

**Compile Latest Snapshot** (button above the backup list) merges every existing snapshot (the full backup excluded) into one new "Merged" snapshot, oldest to newest, so later changes win over earlier ones for the same file. The result is the latest version of every file that's ever changed across all your snapshots — not a complete copy of `SRC_DIR`, just a consolidated view of the changes, so it behaves like any other delta-only backup: Restore still offers Full Restore vs. Only Snapshot for it.

## Find File

The **Find File** page (`/find-file`) searches for a file or folder by name across *every* backup and snapshot at once, instead of one at a time — useful when you know what you're looking for but not which snapshot(s) actually have it. Each result shows which backup/snapshot it was found in and offers two restore options:

- **Restore to Recovery Folder (default)** — same behavior as the main Restore page: copies just that one item into a new, empty, timestamped folder under `BASE_DIR/Restored/`. Never touches `SRC_DIR`.
- **Restore to Source** (the dropdown next to it) — copies that one item directly back into `SRC_DIR`, at the exact path it came from. This is genuinely destructive if the wrong result gets picked — it overwrites whatever's currently there. As a safety net, anything that already exists at that exact path is moved aside first (renamed with a `.folderw-overwritten-<timestamp>` suffix) rather than deleted outright, so a mistaken restore can still be undone by hand — but it's still a real write into your live source directory, not a copy off to the side, so use it deliberately.

Both options work for a Differential (or Merged) snapshot's own delta *and*, if the file hasn't changed since, transparently fall back to the full backup's copy of it — same logic the main Restore page uses for Full Restore.

## Snapshot Retention

Set **"Snapshots to Keep"** (in the setup GUI or the web Settings page) to a number, and after every backup FolderW deletes the oldest snapshots beyond that count — keeping only the most recent N. Leave it blank to keep every snapshot forever (the default).

This only ever deletes `Month/Day/Time` folders under `BASE_DIR/FULL_NAME/Snapshots`; the full backup is never part of this retention pool at all (not just protected within it) — it's a one-time backup, not something new runs add to.

Deleting a file from `SRC_DIR` behaves differently depending on which backup you look at:

- **Full backup:** frozen after its one-time creation, so it keeps whatever it captured then, regardless of later deletions from `SRC_DIR`.
- **Incremental snapshots:** deletions are never recorded in a snapshot — a deleted file simply stops appearing in *future* snapshots, but stays intact in whichever earlier snapshot last captured it, so you can still recover it from there via Restore.
- **Differential snapshots:** since each one only contains what's new or changed relative to the full backup, a file deleted from `SRC_DIR` simply won't appear in that snapshot's delta — it's still recoverable from the full backup itself.
