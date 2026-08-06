import os
from db_operations import get_last_session_number, list_items_by_session, store_changes_in_db, load_other_variables, load_env_value, record_backup_run
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
rsync_txt = load_other_variables('rsync_txt')
database = load_env_value('DATABASE')
full_name = load_env_value("FULL_NAME")
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

def ensure_backup_folder_icon():
    """Drop the FolderW icon into the backup destination folder so it's
    visually identifiable when browsed manually. rsync --delete manages
    the entire full_backup directory (src_dir's contents are mirrored
    directly into it), so FolderW.png is listed in the rsync exclude
    file to keep it from being wiped as an "extra" file on every sync.
    """
    icon_source = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'FolderW.png')
    icon_dest = os.path.join(full_backup, 'FolderW.png')
    os.makedirs(full_backup, exist_ok=True)
    if os.path.exists(icon_source) and not os.path.exists(icon_dest):
        shutil.copy2(icon_source, icon_dest)
        logger.info(f"Added FolderW icon to backup folder: {icon_dest}")

def rsync():
    # Trailing slash on the source makes rsync copy src_dir's *contents*
    # into full_backup, instead of nesting it as full_backup/<src_dir basename>/.
    src_dir_contents = src_dir.rstrip('/') + '/'
    rsync_command = ["rsync", "-av", "--delete", f'--exclude-from={exclude_file}', src_dir_contents, full_backup]
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

def parse_logfile(rsync_txt):
    changes = []
    capturing = False
    try:
        with open(rsync_txt, 'r') as f:
            for line in f:
                #logger.debug(f"Reading line from rsync_txt: {line.strip()}")
                line = line.strip()
                if "sending incremental file list" in line:
                    capturing = True
                    continue
                if capturing:
                    if line == "":
                        continue
                    if "sent" in line or "received" in line or "total size" in line:
                        continue
                    if "Server/" in line:
                        line = line.replace("Server/", "")
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
            destination_path = os.path.join(base_dir, incremental_folder, item_path)
            try:
                if os.path.isdir(source_path):
                    shutil.copytree(source_path, destination_path, dirs_exist_ok=True)  # dirs_exist_ok=True handles existing directories
                    #logger.success(f"Copied directory {source_path} to {destination_path}")
                else:
                    os.makedirs(os.path.dirname(destination_path), exist_ok=True)
                    shutil.copy2(source_path, destination_path)
                    #logger.success(f"Copied file {source_path} to {destination_path}")
            except Exception as e:
                logger.error(f"Error copying {source_path} to {destination_path}: {e}")
    else:
        logger.info("No files to copy.")

    return last_session_number, incremental_folder


if __name__ == "__main__":
    logger.info(f"Full Backup is: {full_name}")
    ensure_backup_folder_icon()
    for progress in rsync():
        logger.info(f"Progress: {progress}")

    changes = parse_logfile(rsync_txt)
    store_changes_in_db(changes)
    last_session_number, incremental_folder = copy_files()
    record_backup_statistics(changes, last_session_number, incremental_folder)
