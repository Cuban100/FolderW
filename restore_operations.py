import os
import re
import json
import shutil
from datetime import datetime
from db_operations import load_env_value, load_other_variables, get_files_changed_by_label
from statistics_operations import human_readable_size
from loguru import logger

# Must match rsync_incremental.py's COMPLETION_MARKER -- duplicated rather
# than imported to avoid a circular import (rsync_incremental.py already
# imports cleanup_old_snapshots from this module). Written into a snapshot
# only after rsync finishes successfully, so its presence is how a
# complete, restorable snapshot is told apart from a partial/interrupted
# one left behind by a crash, kill, or power loss mid-run.
COMPLETION_MARKER = '.folderw_complete'

# Marks a snapshot produced by compile_latest_snapshot() (merged from
# every existing snapshot's latest-per-path files, Full Backup excluded)
# rather than a real backup run -- list_backups() checks for this to
# label it "Merged - <date>" instead of a plain date, so it's never
# mistaken for a normal snapshot in the Restore listing.
COMPILED_MARKER = '.folderw_compiled'


def _snapshot_created_at(snapshot_path):
    """The real creation time of a snapshot, for sorting -- read from
    COMPLETION_MARKER's own CONTENT (an ISO timestamp, written once by
    mark_snapshot_complete() and never rewritten after), not the
    directory's mtime. Confirmed live, the hard way: a directory's mtime
    updates on ANY change to its contents -- not just at creation --
    so anything that later writes into a snapshot folder (a retroactive
    cleanup pass, the stats cache being computed on first page view, a
    user note) silently reshuffles "newest first" ordering after the
    fact. cleanup_old_snapshots() trusting that corrupted ordering is
    exactly how a real, needed snapshot got deleted by retention while
    an actually-older one survived. Falls back to mtime only if the
    marker is missing/unparseable (shouldn't happen -- callers already
    require its presence to list a snapshot at all).
    """
    marker_path = os.path.join(snapshot_path, COMPLETION_MARKER)
    try:
        with open(marker_path, 'r') as f:
            return datetime.fromisoformat(f.read().strip()).timestamp()
    except (OSError, ValueError) as e:
        logger.warning(f"Could not read creation time from {marker_path}, falling back to mtime: {e}")
        return os.path.getmtime(snapshot_path)


# Cached (file_count, total_size) for a snapshot, as JSON. A snapshot is
# now a complete point-in-time tree (hundreds of thousands of files, not
# a small delta) -- recomputing this via a live os.walk on every /restore
# page load measured 14+ seconds for just 3 snapshots and scales linearly
# with both snapshot count and size (MAX_SNAPSHOTS can be dozens). Written
# once by rsync_incremental.py right after a backup completes (see
# summarize_folder() below, called from there too), so the common case is
# an instant file read here. Snapshots that predate this cache (or a
# migrated legacy one) self-heal on first view: computed live once, then
# written here so every subsequent load is fast too.
STATS_CACHE_FILE = '.folderw_stats'

# A free-text note a user can attach to a snapshot after the fact (e.g.
# "changed the whole FolderW setup right before this one") -- most useful
# on manual snapshots, where "what was different about this one" isn't
# otherwise recorded anywhere. Plain text, not JSON: simple to read/write/
# edit, and there's nothing else to store alongside it.
NOTE_FILE = '.folderw_note'

# Internal bookkeeping files that sit at a snapshot's own root -- never
# real backed-up data, so never counted, listed, searched, or copied into
# Restored/ output. One shared tuple instead of repeating the names at
# every exclusion site, so adding another later can't be missed at one
# of them. FolderW.png/.directory only ever land inside a snapshot for
# full_backup specifically (its own branding, physically written there
# now that it's a real directory -- see rsync_incremental.py's
# ensure_backup_folder_icon()), but harmless to exclude everywhere.
_INTERNAL_FILES = (COMPLETION_MARKER, STATS_CACHE_FILE, NOTE_FILE, COMPILED_MARKER, 'FolderW.png', '.directory')

def _chmod_775(path):
    # rsync's own --chmod=775 (see rsync_incremental.py) only ever applies
    # to files it actually transfers -- these internal bookkeeping files
    # are written directly by Python (open()/shutil.copy2()), which
    # respects the process umask instead, landing at 664 by default.
    # Confirmed live: a snapshot showing its real content at 775 but its
    # own .folderw_complete/.folderw_stats/.folderw_note at 664 -- an
    # inconsistency with "every file in the backup is 775", not just
    # these markers specifically. Non-fatal: a permission oddity here
    # isn't worth failing an otherwise-successful backup over.
    try:
        os.chmod(path, 0o775)
    except OSError as e:
        logger.warning(f"Could not chmod {path} to 775: {e}")


_MONTH_NAMES = {
    'January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December',
}
# Trailing (-\d+)? accepts a disambiguation suffix (e.g. "1:46-AM-2"):
# rsync_incremental.py's _unique_new_snapshot_path() appends one when two
# snapshot-creating calls land in the same clock-minute, which happens in
# the normal course of a legacy-install migration immediately followed by
# that install's first real backup (kept in lockstep with that function).
_TIME_FOLDER_RE = re.compile(r'^\d{1,2}:\d{2}-(AM|PM)(-\d+)?$')


def _is_month_folder(name):
    return name in _MONTH_NAMES


def _is_day_folder(name):
    return name.isdigit() and 1 <= int(name) <= 31


def _is_time_folder(name):
    return bool(_TIME_FOLDER_RE.match(name))


def summarize_folder(path):
    """Full recursive (file_count, total_size) for path -- expensive on a
    tree this size (hundreds of thousands of files), so callers displaying
    a snapshot's stats should go through _cached_summarize_snapshot()
    instead where possible. Still used directly for one-off cases (a fresh
    backup's own completion, restore_backup()'s post-copy summary of a
    small Restored/ folder) where there's no repeated-page-load cost to
    avoid.
    """
    file_count = 0
    total_size = 0
    for dirpath, _, filenames in os.walk(path):
        for f in filenames:
            # Internal bookkeeping, not real backed-up data -- shouldn't
            # count toward a snapshot's displayed file count/size, and
            # only ever sits at a snapshot's own root, never in a subdir.
            if f in _INTERNAL_FILES and dirpath == path:
                continue
            fp = os.path.join(dirpath, f)
            try:
                total_size += os.path.getsize(fp)
                file_count += 1
            except OSError:
                pass
    return file_count, total_size


def get_snapshot_note(snapshot_path):
    try:
        with open(os.path.join(snapshot_path, NOTE_FILE), 'r') as f:
            return f.read().strip()
    except OSError:
        return ''


def set_snapshot_note(snapshot_path, note):
    note = (note or '').strip()
    note_path = os.path.join(snapshot_path, NOTE_FILE)
    if not note:
        # Empty note means "remove it" -- no point keeping a blank file
        # around, and this is also what lets a note actually be cleared
        # rather than only ever replaced with different text.
        try:
            os.remove(note_path)
        except FileNotFoundError:
            pass
        return
    with open(note_path, 'w') as f:
        f.write(note)
    _chmod_775(note_path)


def _cached_summarize_snapshot(snapshot_path):
    """(file_count, total_size) for a snapshot, via STATS_CACHE_FILE when
    present -- an instant file read instead of a full tree walk, which is
    what makes /restore's listing page fast regardless of how large or how
    many snapshots exist. Self-healing: a snapshot missing the cache (one
    that predates this feature, or a migrated legacy directory) is walked
    live exactly once here, then the result is written for every
    subsequent call to read instead.
    """
    cache_path = os.path.join(snapshot_path, STATS_CACHE_FILE)
    try:
        with open(cache_path, 'r') as f:
            cached = json.load(f)
        return cached['file_count'], cached['total_size']
    except (OSError, ValueError, KeyError):
        pass
    file_count, total_size = summarize_folder(snapshot_path)
    try:
        with open(cache_path, 'w') as f:
            json.dump({'file_count': file_count, 'total_size': total_size}, f)
        _chmod_775(cache_path)
    except OSError as e:
        logger.warning(f"Could not write stats cache for {snapshot_path}: {e}")
    return file_count, total_size


def mark_snapshot_complete(snapshot_path):
    """Called by rsync_incremental.py right after a snapshot finishes
    successfully (a fresh backup, or the legacy-directory migration).
    Writes the completion marker (see COMPLETION_MARKER) and proactively
    computes+caches this snapshot's stats (see STATS_CACHE_FILE) so the
    /restore page never has to walk it live on a first view -- the cost
    (a few seconds even on a ~400K-file tree, per summarize_folder's own
    os.walk) is absorbed here, once, into a backup run that's already
    taking minutes, rather than paid by whoever next loads the page.
    """
    marker_path = os.path.join(snapshot_path, COMPLETION_MARKER)
    with open(marker_path, 'w') as f:
        f.write(datetime.now().isoformat())
    _chmod_775(marker_path)
    _cached_summarize_snapshot(snapshot_path)


def list_backups():
    """List every backup, newest first: the one-time initial full backup
    (id "full", a real, frozen directory -- created once and never
    touched again, see rsync_incremental.py) plus every incremental/
    differential snapshot (Month/Day/Time folders under BASE_DIR/
    Snapshots) since it. Unlike the previous design, full_backup is a
    genuinely separate, distinct backup now (not a mirror of whichever
    snapshot is newest), so it needs its own listing entry -- it's the
    fixed baseline every differential snapshot is measured against, and
    is itself a complete, restorable point-in-time copy.
    """
    snapshots_root = load_other_variables('snapshots_root')
    full_backup = load_other_variables('full_backup')

    backups = []

    if os.path.isdir(full_backup) and os.path.exists(os.path.join(full_backup, COMPLETION_MARKER)):
        file_count, total_size = _cached_summarize_snapshot(full_backup)
        backups.append({
            "id": "full",
            "label": "Full Backup",
            "file_count": file_count,
            "files_changed": get_files_changed_by_label("Full Backup"),
            "size": human_readable_size(total_size),
            "size_bytes": total_size,
            "mtime": _snapshot_created_at(full_backup),
            "path": full_backup,
            "note": get_snapshot_note(full_backup),
        })

    if not os.path.isdir(snapshots_root):
        for b in backups:
            del b["mtime"]
        return backups

    # Folder *names* under snapshots_root are still validated against
    # FolderW's own Month/Day/Time convention as defense in depth (e.g.
    # against a stray unrelated folder someone drops in there), even though
    # nesting snapshots under BASE_DIR/Snapshots/ already keeps this walk
    # away from any unrelated content sitting elsewhere under BASE_DIR.
    for month in sorted(os.listdir(snapshots_root)):
        if not _is_month_folder(month):
            continue
        month_path = os.path.join(snapshots_root, month)
        if not os.path.isdir(month_path):
            continue
        for day in sorted(os.listdir(month_path)):
            if not _is_day_folder(day):
                continue
            day_path = os.path.join(month_path, day)
            if not os.path.isdir(day_path):
                continue
            for time_folder in sorted(os.listdir(day_path)):
                if not _is_time_folder(time_folder):
                    continue
                snapshot_path = os.path.join(day_path, time_folder)
                if not os.path.isdir(snapshot_path):
                    continue
                if not os.path.exists(os.path.join(snapshot_path, COMPLETION_MARKER)):
                    # Missing marker means rsync never finished this one
                    # (crash, kill, power loss mid-run) -- not a valid,
                    # restorable backup, so it doesn't count toward
                    # retention (cleanup_old_snapshots) or show up here.
                    continue
                file_count, total_size = _cached_summarize_snapshot(snapshot_path)
                snapshot_id = f"{month}/{day}/{time_folder}"
                is_compiled = os.path.exists(os.path.join(snapshot_path, COMPILED_MARKER))
                if is_compiled:
                    created_dt = datetime.fromtimestamp(_snapshot_created_at(snapshot_path))
                    label = f"Merged - {created_dt.month}/{created_dt.day}/{created_dt.year}"
                else:
                    label = f"{month} {day}, {time_folder}"
                backups.append({
                    "id": snapshot_id,
                    "label": label,
                    "file_count": file_count,
                    # New/modified files in specifically this session, vs
                    # file_count above (every file in the complete point-
                    # in-time tree, thanks to --link-dest). None if this
                    # snapshot predates label consistently matching its own
                    # folder id (see record_backup_statistics()).
                    "files_changed": get_files_changed_by_label(snapshot_id),
                    "size": human_readable_size(total_size),
                    "size_bytes": total_size,
                    "mtime": _snapshot_created_at(snapshot_path),
                    # Real on-disk path -- shown on the Restore page so a
                    # snapshot can also be opened directly in the OS file
                    # manager, not just browsed in-app.
                    "path": snapshot_path,
                    "note": get_snapshot_note(snapshot_path),
                })

    # mtime only ever existed to sort by -- label already spells out the
    # same date/time (e.g. "August 08, 2:30-AM"), so there's no separate
    # display value to keep around after sorting.
    backups.sort(key=lambda b: b["mtime"], reverse=True)
    for b in backups:
        del b["mtime"]
    return backups


def total_destination_size_bytes():
    """True current footprint of the backup destination -- full backup
    plus every current snapshot, in bytes. Reuses list_backups()'s
    existing per-backup cache (_cached_summarize_snapshot(), one
    .folderw_stats file per backup) rather than a fresh recursive du of
    the whole destination -- only recomputes for a backup that doesn't
    have a cached size yet, same as the Restore page already relies on
    for its own listing to stay fast.
    """
    return sum(b["size_bytes"] for b in list_backups())


def compile_latest_snapshot():
    """Merge every existing snapshot (Incremental or Differential --
    Full Backup deliberately excluded, per explicit request) into one
    new snapshot containing the latest version of every file that has
    appeared in ANY of them. Not a complete copy of SRC_DIR: a file
    only ever shows up in a snapshot in the first place if it changed
    at least once since the full backup, so this is a consolidated
    view of "everything that's changed," not a full restore point --
    unchanged-since-day-one files are only ever in Full Backup, which
    this deliberately never reads.

    Snapshots are already chronologically ordered by list_backups()
    (sorted by _snapshot_created_at() -- the completion marker's own
    recorded timestamp, not directory mtime, which can't be trusted as
    a creation-order signal here, see that function's docstring for
    why). Walking them oldest to newest and letting each snapshot's
    copy of a given relative path simply overwrite whatever an earlier
    snapshot wrote there is enough to guarantee the final result holds
    each path's latest version -- no per-file timestamp comparison
    needed, and no risk of the same mtime pitfall that broke retention
    ordering earlier.

    Hardlinked where possible (same filesystem, the common case, so
    this is fast regardless of total data size), falling back to a
    real copy only if that fails. Returns (new_snapshot_path,
    file_count), or (None, 0) if there are no snapshots to compile yet.
    """
    snapshots = [b for b in list_backups() if b["id"] != "full"]
    if not snapshots:
        return None, 0
    oldest_first = list(reversed(snapshots))  # list_backups() is newest-first

    snapshots_root = load_other_variables('snapshots_root')
    now = datetime.now()
    folder_name = f"{now.strftime('%B')}/{now.strftime('%d')}/{now.strftime('%I').lstrip('0')}:{now.strftime('%M')}-{now.strftime('%p')}"
    new_snapshot_path = os.path.join(snapshots_root, folder_name)
    suffix = 1
    while os.path.exists(new_snapshot_path):
        suffix += 1
        new_snapshot_path = os.path.join(snapshots_root, f"{folder_name}-{suffix}")
    os.makedirs(new_snapshot_path, exist_ok=True)

    for snap in oldest_first:
        snap_path = snap["path"]
        for dirpath, _, filenames in os.walk(snap_path):
            for f in filenames:
                if f in _INTERNAL_FILES and dirpath == snap_path:
                    continue
                src_fp = os.path.join(dirpath, f)
                rel = os.path.relpath(src_fp, snap_path)
                dest_fp = os.path.join(new_snapshot_path, rel)
                os.makedirs(os.path.dirname(dest_fp), exist_ok=True)
                if os.path.lexists(dest_fp):
                    # A later (more recent) snapshot's version of this
                    # same path -- replaces whatever an earlier one wrote.
                    os.remove(dest_fp)
                try:
                    os.link(src_fp, dest_fp)
                except OSError:
                    shutil.copy2(src_fp, dest_fp)
                    _chmod_775(dest_fp)

    compiled_marker_path = os.path.join(new_snapshot_path, COMPILED_MARKER)
    with open(compiled_marker_path, 'w') as f:
        f.write(datetime.now().isoformat())
    _chmod_775(compiled_marker_path)

    set_snapshot_note(new_snapshot_path, f"Compiled from {len(snapshots)} snapshot(s) -- the latest version of every file that changed since the full backup.")
    mark_snapshot_complete(new_snapshot_path)
    file_count, _ = _cached_summarize_snapshot(new_snapshot_path)
    return new_snapshot_path, file_count


def cleanup_old_snapshots(max_snapshots):
    """Delete the oldest snapshots (Month/Day/Time folders) beyond
    max_snapshots, keeping the newest ones. A falsy/zero/negative
    max_snapshots means "keep everything" (no cleanup).

    The initial full backup (id "full") is a separate, one-time, frozen
    baseline -- excluded from the retention pool entirely (not counted
    toward max_snapshots, never deleted here), regardless of how old it
    gets relative to everything else. It's differential mode's whole
    reason for existing (the fixed reference every differential snapshot
    is measured against), so it can't be pruned just for being the
    oldest thing on disk, which -- being created once, right at the
    start -- it always will be.
    """
    try:
        max_snapshots = int(max_snapshots)
    except (TypeError, ValueError):
        return []
    if max_snapshots <= 0:
        return []

    # list_backups() already sorts newest first; "full" excluded from
    # the retention pool entirely, not just protected within it.
    snapshots = [b for b in list_backups() if b["id"] != "full"]
    to_delete = snapshots[max_snapshots:]

    deleted = []
    for snapshot in to_delete:
        path = get_backup_path(snapshot["id"])
        if path is None:
            continue
        try:
            shutil.rmtree(path)
            deleted.append(snapshot["id"])
            logger.success(f"Deleted old snapshot (retention limit {max_snapshots}): {snapshot['id']}")
            _prune_empty_parents(path)
        except OSError as e:
            logger.error(f"Error deleting snapshot {snapshot['id']}: {e}")
    return deleted


def _prune_empty_parents(path):
    """After deleting a Month/Day/Time leaf folder, remove now-empty
    Day/Month parent folders, without ever touching BASE_DIR itself."""
    base_real = os.path.realpath(load_env_value('BASE_DIR'))
    parent = os.path.dirname(path)
    while os.path.realpath(parent) != base_real:
        try:
            if os.listdir(parent):
                break
            os.rmdir(parent)
            parent = os.path.dirname(parent)
        except OSError:
            break


def get_backup_path(backup_id):
    """Resolve a backup id ('full' or 'Month/Day/Time') to an absolute path,
    validated to stay inside the full backup / snapshots root respectively.
    Returns None if invalid, unknown, or an attempt to escape those roots."""
    if backup_id == "full":
        root = load_other_variables('full_backup')
        path = root
    else:
        root = load_other_variables('snapshots_root')
        path = os.path.join(root, backup_id)

    root_real = os.path.realpath(root)
    path_real = os.path.realpath(path)
    if path_real != root_real and not path_real.startswith(root_real + os.sep):
        logger.warning(f"Rejected restore path outside its root: {backup_id}")
        return None
    if not os.path.isdir(path_real):
        return None
    return path_real


def list_files_in_backup(backup_path, search=None, limit=2000):
    """Recursively list files in a single backup/snapshot, capped at
    `limit` so a huge full-mirror snapshot (hundreds of thousands of
    files) can't render an unbounded page.

    Without a search term, this is just the alphabetically-first `limit`
    files -- fine for a quick look, but on a snapshot this size it's a
    small, fairly arbitrary slice. With a search term, matches are found
    by walking the *entire* tree (not stopping at the first `limit` hits
    encountered), so a search doesn't silently miss something that
    happens to sort later than the first 2000 matches -- this costs more
    time than the unfiltered case (no early exit), but that's the point.
    """
    files = []
    truncated = False
    search_lower = search.lower() if search else None
    for dirpath, _, filenames in os.walk(backup_path):
        for f in sorted(filenames):
            if f in _INTERNAL_FILES and dirpath == backup_path:
                continue
            fp = os.path.join(dirpath, f)
            rel = os.path.relpath(fp, backup_path)
            if search_lower and search_lower not in rel.lower():
                continue
            if len(files) >= limit:
                truncated = True
                if not search_lower:
                    break
                continue
            try:
                size = os.path.getsize(fp)
            except OSError:
                size = 0
            files.append({"path": rel, "size": human_readable_size(size)})
        if truncated and not search_lower:
            break
    files.sort(key=lambda f: f["path"])
    return files, truncated


def restore_backup(backup_id, selected_paths=None):
    """Copy an entire backup/snapshot -- an exact mirror of it -- or just
    the given relative file paths within it, into a new timestamped
    folder under BASE_DIR/Restored/. Never writes into SRC_DIR, so a bad
    pick can't clobber current data.
    """
    backup_path = get_backup_path(backup_id)
    if backup_path is None:
        raise ValueError(f"Backup not found: {backup_id}")

    base_dir = load_env_value('BASE_DIR')
    label = backup_id.replace('/', '-').replace(':', '-')
    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    dest_root = os.path.join(base_dir, "Restored", f"{timestamp}_{label}")
    os.makedirs(dest_root, exist_ok=True)

    backup_real = os.path.realpath(backup_path)

    if not selected_paths:
        for entry in os.listdir(backup_path):
            if entry in _INTERNAL_FILES:
                continue
            src = os.path.join(backup_path, entry)
            dst = os.path.join(dest_root, entry)
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
    else:
        for rel_path in selected_paths:
            src = os.path.realpath(os.path.join(backup_path, rel_path))
            if src != backup_real and not src.startswith(backup_real + os.sep):
                logger.warning(f"Rejected restore of path outside backup: {rel_path}")
                continue
            if not os.path.exists(src):
                continue
            dst = os.path.join(dest_root, rel_path)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)

    file_count, _ = summarize_folder(dest_root)
    logger.success(f"Restored {file_count} file(s) from {backup_id} to {dest_root}")
    return dest_root, file_count
