"""Differential-mode backup runner. --link-dest points at original_backup
(the very first snapshot ever completed, fixed forever) instead of the
most recent snapshot -- so every run here stores everything that's
changed since that one original, not just since the last snapshot.

Kept as a separate script from rsync_incremental.py, selected by the
user's BACKUP_METHOD setting (see main_backup.py/rsync_event_handler.py),
rather than a runtime flag inside it, so the two strategies stay
independently readable. Every other piece of machinery -- migration, the
actual rsync invocation, progress/notification/DB-recording logic -- is
shared via direct imports below, not duplicated.

Tradeoff vs incremental: a file that changed once and never again still
gets rewritten in every future differential run (it still differs from
the fixed original), so these grow larger over time than incremental
snapshots would. What differential does NOT buy back here, unlike in
traditional (non-hardlink) backup tools: simpler restores. Because both
modes use --link-dest, every snapshot from either script is already a
complete, independently-restorable tree on its own -- restoring never
depends on any other snapshot being present, regardless of which mode
built it.
"""
import os
import re
import sys
import time
import threading

from loguru import logger

from db_operations import (
    get_last_session_number, store_changes_in_db, load_other_variables,
    load_env_value, set_database_value,
)
from restore_operations import cleanup_old_snapshots, mark_snapshot_complete
from statistics_operations import get_folder_size_bytes_du, human_readable_size
from notifications import notify

from rsync_incremental import (
    _check_sudo_rsync_available, _migrate_legacy_full_backup,
    _unique_new_snapshot_path, _repoint_full_backup,
    _repoint_original_backup_if_unset, ensure_backup_folder_icon,
    rsync, parse_logfile, generate_incremental_folder,
    record_backup_statistics,
)

src_dir = load_env_value('SRC_DIR')
full_backup = load_other_variables('full_backup')
original_backup = load_other_variables('original_backup')
snapshots_root = load_other_variables('snapshots_root')
rsync_txt = load_other_variables('rsync_txt')
exclude_file = load_other_variables('exclude_file')
database = load_env_value('DATABASE')


def _original_snapshot_path():
    """The fixed --link-dest source for every differential run: the very
    first snapshot ever completed, or None on the very first-ever backup
    (nothing to diff against yet -- that first run is a full transfer
    regardless of mode, same as rsync_incremental.py's first run)."""
    if os.path.exists(original_backup):
        return os.path.realpath(original_backup)
    return None


if __name__ == "__main__":
    _check_sudo_rsync_available()
    _migrate_legacy_full_backup()

    # Fixed for the whole run, unlike rsync_incremental.py's previous_
    # snapshot -- always the original full backup, never the most recent
    # snapshot. That's the entire point of differential mode.
    original_snapshot = _original_snapshot_path()

    new_snapshot_path = _unique_new_snapshot_path(generate_incremental_folder())
    incremental_folder = os.path.relpath(new_snapshot_path, snapshots_root)
    os.makedirs(new_snapshot_path, exist_ok=True)
    logger.info(f"New differential snapshot destination: {new_snapshot_path} (link-dest: {original_snapshot})")

    set_database_value('BACKUP_PROGRESS_PERCENT', '')
    set_database_value('BACKUP_ETA', '')
    set_database_value('CURRENT_BACKUP_SIZE', 'Calculating…')
    set_database_value('BACKUP_PREPARING', '')
    set_database_value('BACKUP_START_TIME', str(time.time()))

    baselines = {'dest_baseline': None, 'source_total': None, 'transferred_offset': 0, 'last_transferred': 0}

    def _compute_dest_baseline():
        # The original full backup, not the previous snapshot -- that's
        # what this run's --link-dest actually compares against, so it's
        # the correct "what's already effectively there" anchor here.
        size = get_folder_size_bytes_du(original_snapshot) if original_snapshot else 0
        baselines['dest_baseline'] = size
        baselines['transferred_offset'] = baselines['last_transferred']
        logger.info(f"Destination baseline size: {size}")

    def _compute_source_total():
        baselines['source_total'] = get_folder_size_bytes_du(src_dir, exclude_from=exclude_file)
        logger.info(f"Source total size: {baselines['source_total']}")
        if baselines['source_total'] is not None:
            set_database_value('LAST_SRC_SIZE', human_readable_size(baselines['source_total']))

    threading.Thread(target=_compute_dest_baseline, daemon=True).start()
    threading.Thread(target=_compute_source_total, daemon=True).start()

    rsync_result = {'success': None}
    last_percent = None
    last_eta = None
    DB_WRITE_MIN_INTERVAL = 2
    last_db_write_time = 0.0
    for progress in rsync(new_snapshot_path, link_dest=original_snapshot, result_holder=rsync_result):
        match = re.search(r'^([\d,]+)\s+(\d+)%\s+\S+\s+(\d+:\d+:\d+)', progress)
        if not match:
            continue
        transferred_bytes = int(match.group(1).replace(',', ''))
        baselines['last_transferred'] = transferred_bytes
        eta = match.group(3)
        dest_baseline = baselines['dest_baseline']
        source_total = baselines['source_total']
        if dest_baseline is not None and source_total:
            current_total = dest_baseline + (transferred_bytes - baselines['transferred_offset'])
            percent = min(100, round(current_total / source_total * 100))
            percent_str = str(percent)
        else:
            percent_str = None
        now_ts = time.time()
        if (percent_str != last_percent or eta != last_eta) and now_ts - last_db_write_time >= DB_WRITE_MIN_INTERVAL:
            last_db_write_time = now_ts
            if percent_str != last_percent:
                last_percent = percent_str
                set_database_value('BACKUP_PROGRESS_PERCENT', percent_str or '')
            if eta != last_eta:
                last_eta = eta
                set_database_value('BACKUP_ETA', eta)
    set_database_value('BACKUP_PROGRESS_PERCENT', '')
    set_database_value('BACKUP_ETA', '')
    set_database_value('BACKUP_START_TIME', '')

    if rsync_result['success'] is False:
        logger.error("Differential backup failed — skipping snapshot bookkeeping.")
        sys.exit(1)

    mark_snapshot_complete(new_snapshot_path)
    _repoint_full_backup(new_snapshot_path)
    # No-op after the very first-ever backup -- see the function's own
    # docstring in rsync_incremental.py.
    _repoint_original_backup_if_unset(new_snapshot_path)
    ensure_backup_folder_icon()

    changes = parse_logfile(rsync_txt)
    store_changes_in_db(changes)
    last_session_number = get_last_session_number(database)
    record_backup_statistics(changes, last_session_number, incremental_folder)
    cleanup_old_snapshots(load_env_value('MAX_SNAPSHOTS'))

    if last_session_number <= 1:
        notify("FolderW: Full Backup Complete", f"The initial full backup to {full_backup} finished successfully.")
        set_database_value('FULL_BACKUP_COMPLETED', '1')
