import os
from db_operations import get_last_session_number, list_items_by_session, store_changes_in_db, load_other_variables, load_env_value
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



def rsync():
    rsync_command = ["rsync", "-av", "--delete", f'--exclude-from={exclude_file}', src_dir, full_backup]
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
        # rsync logs paths relative to the parent of src_dir (it includes src_dir's
        # own basename as the first component), so source paths must be resolved
        # from there rather than from src_dir itself.
        src_parent = os.path.dirname(src_dir.rstrip('/'))
        for item_path in session_items:
            source_path = os.path.join(src_parent, item_path)
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


if __name__ == "__main__":
    logger.info(f"Full Backup is: {full_name}")
    for progress in rsync():
        logger.info(f"Progress: {progress}")

    changes = parse_logfile(rsync_txt)
    store_changes_in_db(changes)
    copy_files()
