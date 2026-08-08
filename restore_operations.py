import os
import re
import shutil
from datetime import datetime
from db_operations import load_env_value, load_other_variables
from statistics_operations import human_readable_size
from loguru import logger

# Must match rsync_incremental.py's COMPLETION_MARKER -- duplicated rather
# than imported to avoid a circular import (rsync_incremental.py already
# imports cleanup_old_snapshots from this module). Written into a snapshot
# only after rsync finishes successfully, so its presence is how a
# complete, restorable snapshot is told apart from a partial/interrupted
# one left behind by a crash, kill, or power loss mid-run.
COMPLETION_MARKER = '.folderw_complete'

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


def _summarize_folder(path):
    file_count = 0
    total_size = 0
    for dirpath, _, filenames in os.walk(path):
        for f in filenames:
            # Internal bookkeeping, not real backed-up data -- shouldn't
            # count toward a snapshot's displayed file count/size, and
            # only ever sits at a snapshot's own root, never in a subdir.
            if f == COMPLETION_MARKER and dirpath == path:
                continue
            fp = os.path.join(dirpath, f)
            try:
                total_size += os.path.getsize(fp)
                file_count += 1
            except OSError:
                pass
    return file_count, total_size


def list_backups():
    """List every incremental snapshot (Month/Day/Time folders under
    BASE_DIR/Snapshots), newest first. Each snapshot is now a complete,
    space-efficient point-in-time tree (rsync --link-dest against the
    previous one) rather than a delta of changed files -- so the newest
    snapshot already *is* "the current full backup" (full_backup is just a
    symlink to it). No separate synthetic "full" entry: keeping one
    alongside the normal enumeration would list that same physical
    directory twice, since full_backup's target is always one of the
    dated snapshots below.
    """
    snapshots_root = load_other_variables('snapshots_root')

    backups = []

    if not os.path.isdir(snapshots_root):
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
                file_count, total_size = _summarize_folder(snapshot_path)
                backups.append({
                    "id": f"{month}/{day}/{time_folder}",
                    "label": f"{month} {day}, {time_folder}",
                    "file_count": file_count,
                    "size": human_readable_size(total_size),
                    "mtime": os.path.getmtime(snapshot_path),
                })

    backups.sort(key=lambda b: b["mtime"], reverse=True)
    for b in backups:
        b["date"] = datetime.fromtimestamp(b["mtime"]).strftime('%Y-%m-%d %H:%M')
        del b["mtime"]
    return backups


def cleanup_old_snapshots(max_snapshots):
    """Delete the oldest incremental snapshots beyond max_snapshots,
    keeping the newest ones. full_backup (a symlink to the newest
    snapshot) is never touched directly -- as long as max_snapshots >= 1,
    the snapshot it points at is always among the newest kept, since
    list_backups() sorts newest-first and this only ever prunes the tail.
    A falsy/zero/negative max_snapshots means "keep everything" (no
    cleanup).
    """
    try:
        max_snapshots = int(max_snapshots)
    except (TypeError, ValueError):
        return []
    if max_snapshots <= 0:
        return []

    snapshots = list_backups()
    # list_backups() already sorts newest first
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


def list_files_in_backup(backup_path, limit=2000):
    """Recursively list files in a single backup/snapshot, capped at `limit`
    so a huge full-mirror backup can't render an unbounded page."""
    files = []
    truncated = False
    for dirpath, _, filenames in os.walk(backup_path):
        for f in sorted(filenames):
            if f == COMPLETION_MARKER and dirpath == backup_path:
                continue
            if len(files) >= limit:
                truncated = True
                break
            fp = os.path.join(dirpath, f)
            rel = os.path.relpath(fp, backup_path)
            try:
                size = os.path.getsize(fp)
            except OSError:
                size = 0
            files.append({"path": rel, "size": human_readable_size(size)})
        if truncated:
            break
    files.sort(key=lambda f: f["path"])
    return files, truncated


def restore_backup(backup_id, selected_paths=None):
    """Copy an entire backup, or specific relative file paths within it,
    into a new timestamped folder under BASE_DIR/Restored/. Never writes
    into SRC_DIR, so a bad pick can't clobber current data.
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
            if entry == COMPLETION_MARKER:
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

    file_count, _ = _summarize_folder(dest_root)
    logger.success(f"Restored {file_count} file(s) from {backup_id} to {dest_root}")
    return dest_root, file_count
