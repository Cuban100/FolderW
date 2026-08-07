import os
import re
import stat
from db_operations import get_last_session_number, list_items_by_session, store_changes_in_db, load_other_variables, load_env_value, record_backup_run, set_database_value
from restore_operations import cleanup_old_snapshots
from statistics_operations import get_folder_size_du
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

def ensure_backup_folder_icon():
    """Make the backup destination folder itself show the FolderW logo as
    its icon in the file manager, rather than just containing a PNG a user
    has to open to notice. rsync --delete manages the entire full_backup
    directory (src_dir's contents are mirrored directly into it), so both
    files below are listed in the rsync exclude file to keep them from
    being wiped as "extra" files on every sync.

    Two mechanisms, since no single one covers every file manager:
    - gio set metadata::custom-icon: what GNOME Files/Nemo (GTK/GVFS-based)
      actually use — verified empirically, since the older .directory
      convention below is silently ignored by current Nemo/Nautilus.
    - .directory (freedesktop.org convention): still honored by some other
      file managers (e.g. KDE Dolphin), and travels with the folder itself
      rather than living in a per-user GVFS metadata database, so it's kept
      as a portable fallback even though it's inert on this desktop.
    """
    icon_source = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'FolderW.png')
    icon_dest = os.path.join(full_backup, 'FolderW.png')
    os.makedirs(full_backup, exist_ok=True)
    if os.path.exists(icon_source) and not os.path.exists(icon_dest):
        shutil.copy2(icon_source, icon_dest)
        logger.info(f"Added FolderW icon to backup folder: {icon_dest}")

    directory_file = os.path.join(full_backup, '.directory')
    if not os.path.exists(directory_file):
        with open(directory_file, 'w') as f:
            f.write(f"[Desktop Entry]\nIcon={icon_dest}\n")
        logger.info(f"Set folder icon via {directory_file}")

    try:
        subprocess.run(
            ["gio", "set", full_backup, "metadata::custom-icon", f"file://{icon_dest}"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        logger.info(f"Set folder icon via gio metadata::custom-icon: {full_backup}")
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logger.warning(f"Could not set folder icon via gio (non-fatal): {e}")

def rsync():
    # Trailing slash on the source makes rsync copy src_dir's *contents*
    # into full_backup, instead of nesting it as full_backup/<src_dir basename>/.
    src_dir_contents = src_dir.rstrip('/') + '/'
    # --info=progress2 reports overall transfer progress (a single running
    # percentage across the whole run) rather than per-file progress, which
    # is what a single dashboard progress bar needs. --no-inc-recursive
    # disables rsync's default incremental recursion so it builds the
    # complete file list upfront instead of discovering files as it goes —
    # without it, --info=progress2's percentage is "bytes sent so far /
    # bytes discovered so far", which understates true progress for a long
    # time on large, deep trees (the denominator keeps growing). The
    # trade-off is a pause upfront, proportional to file count, before any
    # progress shows at all.
    rsync_command = ["rsync", "-av", "--delete", "--info=progress2", "--no-inc-recursive", f'--exclude-from={exclude_file}', src_dir_contents, full_backup]
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
            else:
                logger.success(f"Rsync command executed successfully.")
    except Exception as e:
        logger.error(f"Error executing rsync command: {e}")

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
    # Cleared up front so a stale percentage from a previous run can't be
    # shown on the dashboard during the icon-setup gap before rsync's first
    # progress line actually arrives.
    set_database_value('BACKUP_PROGRESS_PERCENT', '')
    last_percent = None
    for progress in rsync():
        logger.info(f"Progress: {progress}")
        match = re.search(r'(\d+)%', progress)
        if match and match.group(1) != last_percent:
            last_percent = match.group(1)
            set_database_value('BACKUP_PROGRESS_PERCENT', last_percent)
    set_database_value('BACKUP_PROGRESS_PERCENT', '')

    changes = parse_logfile(rsync_txt)
    store_changes_in_db(changes)
    last_session_number, incremental_folder = copy_files()
    record_backup_statistics(changes, last_session_number, incremental_folder)
    cleanup_old_snapshots(load_env_value('MAX_SNAPSHOTS'))
