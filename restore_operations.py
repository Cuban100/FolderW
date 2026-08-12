import os
import re
import json
import shutil
from datetime import datetime
from db_operations import load_env_value, load_other_variables, get_files_changed_by_label
from statistics_operations import human_readable_size, get_folder_size_bytes_du
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

# Internal bookkeeping files that sit at a snapshot's own root. Counted,
# listed, searched, and restored the same as everything else now -- a
# restore should give back exactly what's physically in the backup, no
# silent omissions. Still used by compile_latest_snapshot() alone, below:
# copying each source snapshot's own (now-stale, conflicting) marker into
# a newly merged snapshot wouldn't mean anything -- the merged snapshot
# writes its own fresh COMPILED_MARKER once it's done instead.
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
            "is_delta_only": False,
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
                    "is_delta_only": is_compiled or load_env_value('BACKUP_METHOD') != 'incremental',
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
    plus every current snapshot, in bytes.

    NOT a sum of each snapshot's own cached size (_cached_summarize_
    snapshot(), via list_backups()) -- that's each snapshot's own logical
    content size, correct for "how much would restoring just this one
    give me," but summing them massively overcounts a hardlinked
    destination. Incremental mode (--link-dest) means most files in any
    given snapshot are hardlinks sharing the exact same disk blocks as
    the same file in the previous snapshot -- os.path.getsize() (what
    summarize_folder() uses) reports each hardlink's full logical size
    every time, with no way to know it's already been counted once per
    other snapshot referencing it. Found live: 34 incremental snapshots
    of a ~100MB source reported as ~4GB and climbing with every run,
    while `du` on the real destination folder showed ~100MB the whole
    time. A single `du` pass over the whole destination tree (not one
    per snapshot) is what actually gets this right -- GNU du counts a
    multiply-hardlinked file's disk usage exactly once, regardless of
    how many separate directory entries (snapshots, here) point to it,
    as long as they're all included in the same invocation.
    """
    full_backup = load_other_variables('full_backup')
    destination_root = os.path.dirname(full_backup)
    size = get_folder_size_bytes_du(destination_root, apparent_size=False)
    return size if size is not None else 0


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


def search_all_backups(query, limit=200):
    """Search every backup (full backup + every snapshot, oldest and
    newest alike) for a file or folder whose relative path contains
    `query` (case-insensitive substring, same matching rule as
    list_files_in_backup) -- the point being to answer "which
    snapshot(s) is this actually in" without knowing in advance, not
    just search within one already-chosen snapshot.

    Unlike list_files_in_backup, this matches directories too (a
    restorable unit can be a whole folder, not just a single file), and
    doesn't exclude FolderW's own internal files -- consistent with
    restore no longer excluding them either (see _INTERNAL_FILES' own
    docstring).

    Returns (matches, truncated). Newest-snapshot-first (matches
    list_backups()' own order), capped at `limit` total matches across
    all backups combined -- a broad query (e.g. a common word) against
    many snapshots could otherwise return an unbounded result set.
    """
    query_lower = query.lower()
    matches = []
    truncated = False
    for backup in list_backups():
        if len(matches) >= limit:
            truncated = True
            break
        backup_path = backup["path"]
        for dirpath, dirnames, filenames in os.walk(backup_path):
            entries = [(name, False) for name in filenames] + [(name, True) for name in sorted(dirnames)]
            for name, is_dir in entries:
                rel = os.path.relpath(os.path.join(dirpath, name), backup_path)
                if query_lower not in rel.lower():
                    continue
                matches.append({
                    "backup_id": backup["id"],
                    "backup_label": backup["label"],
                    "is_delta_only": backup["is_delta_only"],
                    "rel_path": rel,
                    "is_dir": is_dir,
                })
                if len(matches) >= limit:
                    truncated = True
                    break
            if len(matches) >= limit:
                break
    return matches, truncated


def restore_single_path(backup_id, rel_path, target_dir, safety_backup=True):
    """Restore exactly one file or folder (by its relative path within
    the backup) to target_dir/rel_path -- the cross-snapshot search's
    per-result restore action. Unlike restore_backup(), target_dir isn't
    necessarily a fresh BASE_DIR/Restored/... folder; it can be SRC_DIR
    itself ("Restore to Source"), which is genuinely destructive if the
    wrong result gets picked.

    safety_backup: if something already exists at the exact destination
    path, move it aside first (suffixed with a timestamp) instead of
    silently overwriting/merging over it. Restoring straight back into
    SRC_DIR could otherwise destroy the one thing a mistaken click was
    trying to recover in the first place -- pass True there. Pass False
    for the Recovery Folder destination: always a brand-new, empty,
    timestamped folder, so there's never anything to collide with, and
    the rename-aside would just be noise.
    """
    backup_path = get_backup_path(backup_id)
    if backup_path is None:
        raise ValueError(f"Backup not found: {backup_id}")
    backup_real = os.path.realpath(backup_path)

    # Containment check needs the fully resolved path (catches rel_path
    # trying to escape the backup root via ../ or a symlink) -- but the
    # actual copy source must stay the literal, unresolved path, or a
    # symlink at rel_path would already be silently dereferenced here
    # before _copy_entry() ever gets a chance to preserve it as one (see
    # restore_backup()'s selected_paths branch -- same reasoning).
    literal_src = os.path.join(backup_path, rel_path)
    real_src = os.path.realpath(literal_src)
    if real_src != backup_real and not real_src.startswith(backup_real + os.sep):
        raise ValueError(f"Rejected restore of path outside backup: {rel_path}")
    src = literal_src

    if not os.path.lexists(src):
        # Not in this backup's own physical delta -- for a differential
        # or compiled snapshot that just means this path hasn't changed
        # since the full backup, so its current, correct content is
        # still there (same fallback restore_backup() uses).
        full_backup = load_other_variables('full_backup')
        if os.path.isdir(full_backup):
            full_literal_src = os.path.join(full_backup, rel_path)
            full_backup_real = os.path.realpath(full_backup)
            full_real_src = os.path.realpath(full_literal_src)
            if (full_real_src == full_backup_real or full_real_src.startswith(full_backup_real + os.sep)) and os.path.lexists(full_literal_src):
                src = full_literal_src
    if not os.path.lexists(src):
        raise ValueError(f"{rel_path} not found in '{backup_id}' or the full backup.")

    dst = os.path.join(target_dir, rel_path)
    if safety_backup and os.path.lexists(dst):
        aside_path = f"{dst}.folderw-overwritten-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        os.rename(dst, aside_path)
        logger.info(f"Moved existing {dst} aside to {aside_path} before restoring over it.")

    os.makedirs(os.path.dirname(dst), exist_ok=True)
    _copy_entry(src, dst)
    return dst


def _copy_entry(src, dst):
    """Copy one filesystem entry -- symlink-safe. A symlink is recreated
    as a symlink (its literal target string preserved), never followed.

    Confirmed live, the hard way: os.path.isdir() returns False for a
    symlink whose target can't be resolved from that location (e.g. a
    Python venv's lib64 -> lib, when lib itself is excluded from the
    backup -- exactly the case that crashed a real restore), so it fell
    through to shutil.copy2(), which actually tries to open() through
    the link and dies with a bare FileNotFoundError. Checking
    os.path.islink() first, before os.path.isdir(), avoids that
    entirely -- and shutil.copytree(symlinks=True) below prevents the
    same crash for any symlink NESTED inside a directory being copied,
    not just one at the top level.
    """
    if os.path.islink(src):
        if os.path.lexists(dst):
            os.remove(dst)
        os.symlink(os.readlink(src), dst)
    elif os.path.isdir(src):
        shutil.copytree(src, dst, dirs_exist_ok=True, symlinks=True)
    else:
        shutil.copy2(src, dst)


def _copy_backup_contents(src_root, dest_root):
    """Copy every top-level entry of src_root into dest_root, overwriting
    anything already there of the same name -- the shared mechanism
    restore_backup() uses to layer a snapshot's files on top of Full
    Backup's. Copies FolderW's own internal files (_INTERNAL_FILES) too,
    same as everything else -- restore is a straight copy of what's
    physically in the backup, nothing held back.
    """
    for entry in os.listdir(src_root):
        _copy_entry(os.path.join(src_root, entry), os.path.join(dest_root, entry))


def _is_delta_only(backup_id, backup_path):
    """Whether this backup's own physical content is a delta -- it needs
    Full Backup layered underneath it for a genuinely complete point-in-
    time restore -- rather than an already-complete standalone tree.
    True for a Differential-mode snapshot or a Merged/Compiled one
    (compile_latest_snapshot(), deliberately built without Full Backup);
    False for Full Backup itself or any Incremental-mode snapshot
    (Incremental's --link-dest hardlinks every unchanged file in, so
    it's already a complete tree on its own).

    Shared by list_backups() (exposes this so the Restore page only
    offers the Full Restore / Only Snapshot choice where it actually
    means something different) and restore_backup() (decides the
    default when the caller doesn't force one via combine_with_full).
    """
    if backup_id == "full":
        return False
    if os.path.exists(os.path.join(backup_path, COMPILED_MARKER)):
        return True
    return load_env_value('BACKUP_METHOD') != 'incremental'


def restore_backup(backup_id, selected_paths=None, combine_with_full=True):
    """Copy an entire backup/snapshot -- an exact mirror of it -- or just
    the given relative file paths within it, into a new timestamped
    folder under BASE_DIR/Restored/. Never writes into SRC_DIR, so a bad
    pick can't clobber current data.

    combine_with_full (default True -- "Full Restore" on the Restore
    page): for a delta-only backup (see _is_delta_only()), Full Backup
    is restored first as the base, then this backup's own files copied
    on top, overwriting anything the base already wrote -- matching
    compile_latest_snapshot()'s own "later write wins" logic, and
    README's already-documented restore model ("restoring a specific
    point in time needs the full backup plus that snapshot together").
    Pass False ("Only Snapshot") to restore exactly and only this
    backup's own physical content -- e.g. to inspect precisely what
    changed, or a smaller/faster restore when the full point-in-time
    state isn't needed. Has no effect on Full Backup itself or an
    Incremental snapshot, which are already complete either way.
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

    full_backup = load_other_variables('full_backup')
    needs_full_base = (
        combine_with_full
        and _is_delta_only(backup_id, backup_path)
        and os.path.isdir(full_backup)
        and os.path.exists(os.path.join(full_backup, COMPLETION_MARKER))
    )
    full_backup_real = os.path.realpath(full_backup) if needs_full_base else None

    if not selected_paths:
        if needs_full_base:
            _copy_backup_contents(full_backup, dest_root)
        _copy_backup_contents(backup_path, dest_root)
    else:
        for rel_path in selected_paths:
            # The containment check below needs the FULLY resolved path
            # (catches a rel_path trying to escape the backup root via
            # ../ or a symlink) -- but the actual copy must use the
            # literal, unresolved path, or a symlink at rel_path would
            # already be silently dereferenced by realpath() before
            # _copy_entry() ever gets a chance to preserve it as one.
            literal_src = os.path.join(backup_path, rel_path)
            real_src = os.path.realpath(literal_src)
            if real_src != backup_real and not real_src.startswith(backup_real + os.sep):
                logger.warning(f"Rejected restore of path outside backup: {rel_path}")
                continue
            src = literal_src
            # lexists, not exists: a symlink whose target can't be
            # resolved (see _copy_entry()'s docstring) still needs to be
            # detected as "something is here" -- exists() would say no.
            if not os.path.lexists(src) and needs_full_base:
                # Not in the delta -- for a differential/compiled
                # snapshot that just means this file hasn't changed
                # since the full backup, so its current, correct
                # content is still there.
                full_literal_src = os.path.join(full_backup, rel_path)
                full_real_src = os.path.realpath(full_literal_src)
                if (full_real_src == full_backup_real or full_real_src.startswith(full_backup_real + os.sep)) and os.path.lexists(full_literal_src):
                    src = full_literal_src
            if not os.path.lexists(src):
                continue
            dst = os.path.join(dest_root, rel_path)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            _copy_entry(src, dst)

    file_count, _ = summarize_folder(dest_root)
    logger.success(f"Restored {file_count} file(s) from {backup_id} to {dest_root}")
    return dest_root, file_count
