"""Differential-mode backup runner. Every run compares the live source
directly against full_backup (the one-time initial backup, frozen
forever) -- unlike --link-dest, unchanged files are simply skipped, not
hardlinked, so each dated snapshot under Snapshots/ ends up containing
ONLY what's new or changed since that original. A true delta, matching
the industry-standard definition of "differential backup" (full backup
+ all cumulative changes since it, confirmed against Wikipedia/Acronis/
Redstor) -- not the complete, hardlink-based point-in-time tree
rsync_incremental.py's --link-dest chain produces. Snapshots grow larger
over time as more changes accumulate since the fixed original; that's
expected, not a bug.

Under the hood (see rsync()'s compare_dest branch in rsync_incremental.py):
a dry-run --compare-dest pass first finds exactly which files differ,
then the real transfer is restricted to just those via --files-from --
plain --compare-dest only skips individual unchanged files while still
creating every directory that exists in the source, so a naive
implementation leaves a full mirror of empty directories behind for
everything that didn't change.

Kept as a separate script from rsync_incremental.py, selected by the
user's BACKUP_METHOD setting (see main_backup.py/rsync_event_handler.py),
rather than a runtime flag inside it, so the two strategies stay
independently readable. The initial full backup itself (creating
full_backup, before either strategy applies) is fully shared via
_run_initial_full_backup_if_needed(), imported below -- whichever mode
happens to run first produces the exact same one-time full backup.
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
    _run_initial_full_backup_if_needed, _unique_new_snapshot_path,
    ensure_backup_folder_icon, rsync, parse_logfile,
    generate_incremental_folder, record_backup_statistics,
)

src_dir = load_env_value('SRC_DIR')
full_backup = load_other_variables('full_backup')
snapshots_root = load_other_variables('snapshots_root')
rsync_txt = load_other_variables('rsync_txt')
exclude_file = load_other_variables('exclude_file')
custom_exclude_file = load_other_variables('custom_exclude_file')
database = load_env_value('DATABASE')


if __name__ == "__main__":
    _check_sudo_rsync_available()
    _migrate_legacy_full_backup()

    # The initial full backup, regardless of which BACKUP_METHOD is
    # configured -- every install's very first run goes straight into
    # full_backup itself, not a dated snapshot. Every run after this one
    # is differential, since this script only runs when BACKUP_METHOD !=
    # incremental.
    if _run_initial_full_backup_if_needed():
        sys.exit(0)

    new_snapshot_path = _unique_new_snapshot_path(generate_incremental_folder())
    incremental_folder = os.path.relpath(new_snapshot_path, snapshots_root)
    os.makedirs(new_snapshot_path, exist_ok=True)
    logger.info(f"New differential snapshot destination: {new_snapshot_path} (compare-dest: {full_backup})")

    set_database_value('BACKUP_PROGRESS_PERCENT', '')
    set_database_value('BACKUP_ETA', '')
    set_database_value('CURRENT_BACKUP_SIZE', 'Calculating…')
    set_database_value('BACKUP_PREPARING', '')
    run_start_time = time.time()
    set_database_value('BACKUP_START_TIME', str(run_start_time))

    baselines = {'source_total': None, 'last_transferred': 0}

    def _compute_source_total():
        baselines['source_total'] = get_folder_size_bytes_du(src_dir, exclude_from=[exclude_file, custom_exclude_file])
        logger.info(f"Source total size: {baselines['source_total']}")
        if baselines['source_total'] is not None:
            set_database_value('LAST_SRC_SIZE', human_readable_size(baselines['source_total']))

    threading.Thread(target=_compute_source_total, daemon=True).start()

    rsync_result = {'success': None}
    last_eta = None
    DB_WRITE_MIN_INTERVAL = 2
    last_db_write_time = 0.0
    # --compare-dest doesn't hardlink unchanged files in the way --link-
    # dest does -- nothing is "already there for free" to anchor a
    # dest_baseline against, and the actual size of a delta-only transfer
    # isn't knowable upfront without a separate comparison pass. Showing
    # transferred/source_total as "percent" would understate completion
    # (a differential run can finish having transferred only a tiny
    # fraction of source_total, by design) and could look stuck well
    # below 100% even after a real success -- worse than just not
    # showing a number. Left unset throughout; ETA (rsync's own remaining-
    # time estimate) still updates independently.
    for progress in rsync(new_snapshot_path, compare_dest=full_backup, result_holder=rsync_result):
        match = re.search(r'^([\d,]+)\s+(\d+)%\s+\S+\s+(\d+:\d+:\d+)', progress)
        if not match:
            continue
        transferred_bytes = int(match.group(1).replace(',', ''))
        baselines['last_transferred'] = transferred_bytes
        eta = match.group(3)
        now_ts = time.time()
        if eta != last_eta and now_ts - last_db_write_time >= DB_WRITE_MIN_INTERVAL:
            last_db_write_time = now_ts
            last_eta = eta
            set_database_value('BACKUP_ETA', eta)
    set_database_value('BACKUP_PROGRESS_PERCENT', '')
    set_database_value('BACKUP_ETA', '')
    set_database_value('BACKUP_START_TIME', '')

    if rsync_result['success'] is False:
        logger.error("Differential backup failed — skipping snapshot bookkeeping.")
        sys.exit(1)

    mark_snapshot_complete(new_snapshot_path)
    ensure_backup_folder_icon()

    changes = parse_logfile(rsync_txt)
    store_changes_in_db(changes)
    last_session_number = get_last_session_number(database)
    record_backup_statistics(changes, last_session_number, incremental_folder, backup_type='differential', duration_seconds=time.time() - run_start_time)
    cleanup_old_snapshots(load_env_value('MAX_SNAPSHOTS'))
