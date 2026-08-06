import os
import shutil
from datetime import datetime
from db_operations import load_env_value, load_other_variables
from statistics_operations import human_readable_size
from loguru import logger


def _summarize_folder(path):
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


def list_backups():
    """List the full mirror plus every incremental snapshot (Month/Day/Time
    folders under BASE_DIR), newest first."""
    base_dir = load_env_value('BASE_DIR')
    full_name = load_env_value('FULL_NAME').rstrip('/')
    full_backup = load_other_variables('full_backup')

    backups = []

    if os.path.isdir(full_backup):
        file_count, total_size = _summarize_folder(full_backup)
        backups.append({
            "id": "full",
            "label": "Full Backup (latest)",
            "file_count": file_count,
            "size": human_readable_size(total_size),
            "mtime": os.path.getmtime(full_backup),
        })

    if not os.path.isdir(base_dir):
        return backups

    skip = {full_name, 'Restored'}
    for month in sorted(os.listdir(base_dir)):
        if month in skip:
            continue
        month_path = os.path.join(base_dir, month)
        if not os.path.isdir(month_path):
            continue
        for day in sorted(os.listdir(month_path)):
            day_path = os.path.join(month_path, day)
            if not os.path.isdir(day_path):
                continue
            for time_folder in sorted(os.listdir(day_path)):
                snapshot_path = os.path.join(day_path, time_folder)
                if not os.path.isdir(snapshot_path):
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
    keeping the newest ones. Never touches the full backup — that's a
    continuously-synced mirror, not a snapshot. A falsy/zero/negative
    max_snapshots means "keep everything" (no cleanup).
    """
    try:
        max_snapshots = int(max_snapshots)
    except (TypeError, ValueError):
        return []
    if max_snapshots <= 0:
        return []

    snapshots = [b for b in list_backups() if b["id"] != "full"]
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
    validated to stay inside BASE_DIR. Returns None if invalid, unknown, or
    an attempt to escape BASE_DIR."""
    base_dir = load_env_value('BASE_DIR')

    if backup_id == "full":
        path = load_other_variables('full_backup')
    else:
        path = os.path.join(base_dir, backup_id)

    base_real = os.path.realpath(base_dir)
    path_real = os.path.realpath(path)
    if path_real != base_real and not path_real.startswith(base_real + os.sep):
        logger.warning(f"Rejected restore path outside BASE_DIR: {backup_id}")
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
