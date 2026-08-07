import os
import re
import sys
import stat
import threading
from db_operations import get_last_session_number, list_items_by_session, store_changes_in_db, load_other_variables, load_env_value, record_backup_run, set_database_value
from restore_operations import cleanup_old_snapshots
from statistics_operations import get_folder_size_du, get_folder_size_bytes_du, human_readable_size
from notifications import notify
from dotenv import load_dotenv
import sqlite3
import subprocess
from datetime import datetime
from loguru import logger
import shutil

logfile = load_other_variables('logfile')
base_dir = load_env_value('BASE_DIR')
exclude_file = load_other_variables('exclude_file')
src_dir = load_env_value('SRC_DIR')
full_backup = load_other_variables('full_backup')
snapshots_root = load_other_variables('snapshots_root')
rsync_txt = load_other_variables('rsync_txt')
database = load_env_value('DATABASE')
logger.add(logfile, level="INFO", format="{time} - {level} - {message}")



def record_backup_statistics(changes, last_session_number, incremental_folder):
    """Record that this backup run happened — the full backup (session 1)
    or a specific incremental snapshot — plus per-run numeric stats.
    Recording the run itself matters even when 0 files changed, which is
    why this is separate from (and unconditional on) the numeric stats
    below. Deliberately doesn't re-walk the whole source tree (could be
    huge, e.g. a home directory), so this stays cheap on every run: only
    sums the sizes of files that actually changed this session.
    """
    total_files_processed = len(changes)
    total_size_processed = 0
    for _, rel_path in changes:
        fp = os.path.join(src_dir, rel_path)
        if os.path.isfile(fp):
            try:
                total_size_processed += os.path.getsize(fp)
            except OSError:
                pass
    try:
        conn = sqlite3.connect(database)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO statistics (timestamp, total_files_processed, total_size_processed, source_size, destination_size, deleted_empty_folders, average_speed)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            total_files_processed,
            total_size_processed,
            None,
            None,
            0,
            None,
        ))
        conn.commit()
        conn.close()
        logger.success(f"Recorded backup statistics: {total_files_processed} file(s), {total_size_processed} bytes.")
    except sqlite3.Error as e:
        logger.error(f"Error recording backup statistics: {e}")

    if last_session_number <= 1:
        backup_type, label = 'full', 'Full Backup'
    else:
        backup_type, label = 'incremental', incremental_folder
    record_backup_run(last_session_number, backup_type, label, total_files_processed)

    # Cached here (once per backup run) rather than computed on every
    # dashboard page load — `du` walks the entire full_backup tree, which
    # can take minutes on a large backup and would make the dashboard feel
    # hung if run synchronously on every visit.
    current_size = get_folder_size_du(full_backup)
    if current_size:
        set_database_value('CURRENT_BACKUP_SIZE', current_size)

def _set_folder_icon(folder, icon_filename):
    """Make `folder` itself show a branded icon in the file manager, rather
    than just containing a PNG a user has to open to notice.

    Two mechanisms, since no single one covers every file manager:
    - gio set metadata::custom-icon: what GNOME Files/Nemo (GTK/GVFS-based)
      actually use — verified empirically, since the older .directory
      convention below is silently ignored by current Nemo/Nautilus.
    - .directory (freedesktop.org convention): still honored by some other
      file managers (e.g. KDE Dolphin), and travels with the folder itself
      rather than living in a per-user GVFS metadata database, so it's kept
      as a portable fallback even though it's inert on this desktop.
    """
    icon_source = os.path.join(os.path.dirname(os.path.abspath(__file__)), icon_filename)
    icon_dest = os.path.join(folder, icon_filename)
    os.makedirs(folder, exist_ok=True)
    if os.path.exists(icon_source) and not os.path.exists(icon_dest):
        shutil.copy2(icon_source, icon_dest)
        logger.info(f"Added icon to folder: {icon_dest}")

    directory_file = os.path.join(folder, '.directory')
    if not os.path.exists(directory_file):
        with open(directory_file, 'w') as f:
            f.write(f"[Desktop Entry]\nIcon={icon_dest}\n")
        logger.info(f"Set folder icon via {directory_file}")

    try:
        subprocess.run(
            ["gio", "set", folder, "metadata::custom-icon", f"file://{icon_dest}"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        logger.info(f"Set folder icon via gio metadata::custom-icon: {folder}")
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logger.warning(f"Could not set folder icon via gio (non-fatal): {e}")

def ensure_backup_folder_icon():
    # full_backup (rsync's actual destination) gets FolderW.png, listed in
    # the rsync exclude file so it isn't wiped as an "extra" file on every
    # sync. Its FULL_NAME container folder (one level up — e.g. "Caveman",
    # holding both Full Backup/ and Snapshots/) is never an rsync
    # destination itself, so it's safe to brand with logo.png without any
    # exclude-list entry.
    _set_folder_icon(full_backup, 'FolderW.png')
    _set_folder_icon(os.path.dirname(full_backup), 'logo.png')

def rsync(result_holder=None):
    # result_holder (optional): a dict this function sets 'success' on once
    # the rsync command finishes — lets __main__ below tell a real failure
    # apart from a clean run without changing this generator's yield
    # contract (still just progress lines, unchanged for existing callers).
    # Trailing slash on the source makes rsync copy src_dir's *contents*
    # into full_backup, instead of nesting it as full_backup/<src_dir basename>/.
    src_dir_contents = src_dir.rstrip('/') + '/'
    # --info=progress2 reports overall transfer progress (a single running
    # percentage across the whole run) rather than per-file progress, which
    # is what a single dashboard progress bar needs.
    #
    # Deliberately WITHOUT --no-inc-recursive: that flag makes the
    # percentage numerically accurate by forcing rsync to build the
    # complete file list upfront, but on a large/deep source (e.g. an
    # entire home directory with hundreds of thousands of files) that
    # upfront scan can take many minutes with zero output — and unlike a
    # one-time cost, it repeats on every single run, including incremental
    # backups triggered by one changed file. Kept on the default
    # incremental recursion instead: the percentage understates true
    # progress for a while on such trees (denominator keeps growing as
    # rsync discovers more), but transferring starts immediately every time.
    rsync_command = ["rsync", "-av", "--delete", "--info=progress2", f'--exclude-from={exclude_file}', src_dir_contents, full_backup]
    logger.warning(f"Executing rsync command {rsync_command}")
        
    try:
        with open(rsync_txt, 'w') as log_f:
            # Run the rsync command and capture output in real-time
            process = subprocess.Popen(rsync_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for line in process.stdout:
                log_f.write(line)
                if "%" in line:
                    yield line.strip()  # Stream progress to the frontend
            process.wait()
            if process.returncode != 0:
                logger.error(f"Rsync command failed with return code {process.returncode}")
                if result_holder is not None:
                    result_holder['success'] = False
            else:
                logger.success(f"Rsync command executed successfully.")
                if result_holder is not None:
                    result_holder['success'] = True
    except Exception as e:
        logger.error(f"Error executing rsync command: {e}")
        if result_holder is not None:
            result_holder['success'] = False

PROGRESS_LINE_RE = re.compile(r'^[\d,]+\s+\d+%')

def parse_logfile(rsync_txt):
    changes = []
    capturing = False
    try:
        with open(rsync_txt, 'r') as f:
            for line in f:
                #logger.debug(f"Reading line from rsync_txt: {line.strip()}")
                line = line.strip()
                # --no-inc-recursive changes rsync's header from "sending
                # incremental file list" to "building file list ... done" —
                # both mark the start of the actual file listing.
                if "sending incremental file list" in line or "building file list" in line:
                    capturing = True
                    continue
                if capturing:
                    if line == "":
                        continue
                    if "sent" in line or "received" in line or "total size" in line:
                        continue
                    if PROGRESS_LINE_RE.match(line):
                        # --info=progress2 interleaves per-update transfer
                        # stat lines (bytes, percent, speed, ETA) among the
                        # real filenames on stdout — without this filter
                        # these get mistaken for changed file paths.
                        continue
                    if "Server/" in line:
                        line = line.replace("Server/", "")
                    if line in (".", "./"):
                        # rsync emits this for the destination root itself
                        # (e.g. on a deletion-only run) — it's not a real
                        # file/subdirectory. Treating it as one would resolve
                        # to src_dir itself and copytree the entire source
                        # tree into what's supposed to be a small incremental
                        # snapshot.
                        continue
                    if line and not line.startswith("deleting"):  # Exclude deletion lines
                        changes.append(("update", line))
    except FileNotFoundError:
        logger.error(f"Rsync log file not found: {rsync_txt}")
    except Exception as e:
        logger.error(f"Error parsing rsync log file: {e}")
    logger.debug(f"Parsed changes: {changes}")
    return changes

def generate_incremental_folder():
    now = datetime.now() 
    month = now.strftime('%B') 
    day = now.strftime('%d') 
    hour = now.strftime('%I').lstrip('0')  
    minute = now.strftime('%M')
    am_pm = now.strftime('%p')
    folder = f"{month}/{day}/{hour}:{minute}-{am_pm}"
    logger.debug(f"Generated incremental folder name: {folder}")
    return folder

def _safe_copy2(source_path, destination_path):
    """Like shutil.copy2, but tolerates a destination that already exists
    and is read-only — e.g. immutable/content-addressed files like git
    objects can legitimately be referenced unchanged across sessions, and
    plain shutil.copy2 raises PermissionError trying to overwrite them.
    Made writable first instead of failing; used directly for single files
    and as copytree's copy_function so it also applies to every file
    inside a copied directory tree.
    """
    if os.path.exists(destination_path) and not os.access(destination_path, os.W_OK):
        os.chmod(destination_path, stat.S_IWUSR | stat.S_IRUSR)
    shutil.copy2(source_path, destination_path)


def copy_files():
    incremental_folder = generate_incremental_folder()
    session_items = list_items_by_session(database)
    last_session_number = get_last_session_number(database)

    if last_session_number > 1:
        logger.info(f"Folder Created: {incremental_folder}")
        #logger.info(f"Paths to be copied: {session_items}")
        # With the trailing slash on the rsync source, item_path is already
        # relative to src_dir itself (no basename prefix to strip).
        for item_path in session_items:
            source_path = os.path.join(src_dir, item_path)
            destination_path = os.path.join(snapshots_root, incremental_folder, item_path)
            try:
                if os.path.isdir(source_path):
                    shutil.copytree(source_path, destination_path, dirs_exist_ok=True, copy_function=_safe_copy2)
                    #logger.success(f"Copied directory {source_path} to {destination_path}")
                else:
                    os.makedirs(os.path.dirname(destination_path), exist_ok=True)
                    _safe_copy2(source_path, destination_path)
                    #logger.success(f"Copied file {source_path} to {destination_path}")
            except Exception as e:
                logger.error(f"Error copying {source_path} to {destination_path}: {e}")
    else:
        logger.info("No files to copy.")

    return last_session_number, incremental_folder


if __name__ == "__main__":
    logger.info(f"Full Backup is: {full_backup}")
    ensure_backup_folder_icon()
    # Cleared up front so a stale percentage/size from a previous run can't
    # be shown on the dashboard during the icon-setup gap before rsync's
    # first progress line actually arrives.
    set_database_value('BACKUP_PROGRESS_PERCENT', '')

    # rsync's own --info=progress2 percentage only counts bytes it actually
    # transfers this run — a file that already matches at the destination
    # is skipped and never counted, so on a resumed/interrupted backup its
    # percentage badly understates true progress (verified empirically: a
    # run that only needed to send one small new file into an otherwise
    # complete 50MB destination reported "0%" throughout). Computing our
    # own baseline — what's already at the destination — lets us report
    # (baseline + transferred-this-run) / true-source-total instead. Both
    # numbers come from `du`, run in background threads so neither blocks
    # rsync from starting and both run concurrently with each other.
    baselines = {'dest_baseline': None, 'source_total': None}

    def _compute_dest_baseline():
        baselines['dest_baseline'] = get_folder_size_bytes_du(full_backup)
        logger.info(f"Destination baseline size: {baselines['dest_baseline']}")

    def _compute_source_total():
        baselines['source_total'] = get_folder_size_bytes_du(src_dir)
        logger.info(f"Source total size: {baselines['source_total']}")

    threading.Thread(target=_compute_dest_baseline, daemon=True).start()
    threading.Thread(target=_compute_source_total, daemon=True).start()

    rsync_result = {'success': None}
    last_percent = None
    for progress in rsync(rsync_result):
        logger.info(f"Progress: {progress}")
        match = re.search(r'^([\d,]+)\s+(\d+)%', progress)
        if not match:
            continue
        transferred_bytes = int(match.group(1).replace(',', ''))
        dest_baseline = baselines['dest_baseline']
        source_total = baselines['source_total']
        if dest_baseline is not None and source_total:
            current_total = dest_baseline + transferred_bytes
            percent = min(100, round(current_total / source_total * 100))
            set_database_value('CURRENT_BACKUP_SIZE', human_readable_size(current_total))
        else:
            # Baselines not ready yet — fall back to rsync's own
            # percentage rather than blocking the progress bar on the
            # (potentially slow) du scans above.
            percent = int(match.group(2))
        percent_str = str(percent)
        if percent_str != last_percent:
            last_percent = percent_str
            set_database_value('BACKUP_PROGRESS_PERCENT', percent_str)
    set_database_value('BACKUP_PROGRESS_PERCENT', '')

    if rsync_result['success'] is False:
        # rsync itself failed — recording stats/copying an incremental
        # snapshot against a failed/partial sync would misrepresent it as
        # a completed backup. Exit non-zero so main_backup.py's
        # subprocess.run(check=True) sees it as a failure and notifies.
        logger.error("Backup failed — skipping snapshot bookkeeping.")
        sys.exit(1)

    changes = parse_logfile(rsync_txt)
    store_changes_in_db(changes)
    last_session_number, incremental_folder = copy_files()
    record_backup_statistics(changes, last_session_number, incremental_folder)
    cleanup_old_snapshots(load_env_value('MAX_SNAPSHOTS'))

    if last_session_number <= 1:
        notify("FolderW: Full Backup Complete", f"The initial full backup to {full_backup} finished successfully.")
