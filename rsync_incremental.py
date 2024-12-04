import os
from dotenv import load_dotenv
import sqlite3
import subprocess
import time
from datetime import datetime
from loguru import logger
import shutil

# Load configuration from .env file
load_dotenv()

# Debug log for environment variables
logfile = os.path.join(os.getenv('LOG_DIR'), 'rsync.log')
rsync_txt = os.path.join(os.getenv('LOG_DIR'), 'rsync.txt')
src_dir = os.getenv('SRC_DIR')
base_dir = os.getenv('BASE_DIR')
database = os.getenv('DATABASE') + '.db'
full_backup = os.path.join(os.getenv('BASE_DIR'), os.getenv('FULL_NAME'))
exclude_file = os.path.join(os.getenv('LOG_DIR'), 'rsync_exclude.txt')

logger.add(logfile, level="INFO", format="{time} - {level} - {message}")
logger.debug(f"Log file: {logfile}, RSYNC Text File: {rsync_txt}, Backup Base Dir: {base_dir}, Database: {database}, Full Backup: {full_backup}, Exclude File: {exclude_file}")
def rsync():
    rsync_command = ["rsync", "-av", "--delete", f'--exclude-from={exclude_file}', src_dir, full_backup]
    logger.warning(f"Executing rsync command {rsync_command}")
    try:
        with open(rsync_txt, 'w') as log_f:
            process = subprocess.run(rsync_command, stdout=log_f, stderr=log_f, text=True)
        logger.success(f"Rsync output captured successfully.")
        logger.debug(f"Rsync command return code: {process.returncode}")
        if process.returncode != 0:
            logger.error(f"Rsync command failed with return code {process.returncode}")
        elif process.returncode == 0:
            logger.success(f"Rsync command executed successfully.")
        return process.stdout
    except Exception as e:
        logger.error(f"Error executing rsync command: {e}")
        return ""

def parse_logfile(rsync_txt):
    changes = []
    capturing = False
    try:
        with open(rsync_txt, 'r') as f:
            for line in f:
                logger.debug(f"Reading line from rsync_txt: {line.strip()}")
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


def get_last_session_number(database):
    try:
        conn = sqlite3.connect(database, timeout=10.0)
        cursor = conn.cursor()
        cursor.execute('SELECT session FROM changes ORDER BY id DESC LIMIT 1')
        row = cursor.fetchone()
        conn.close()
        if row:
            return row[0]  # Return the session number if it exists
        else:
            return 0  # Return 0 if no sessions are found
    except sqlite3.Error as e:
        logger.error(f"Error getting last session number: {e}")
        return 0

def get_next_session_number(database):
    last_session_number = get_last_session_number(database)
    logger.debug(f"Last session number: {last_session_number}")
    next_session_number = last_session_number + 1
    logger.debug(f"Next session number: {next_session_number}")
    return next_session_number  # Increment by 1, ensuring the first session starts at 1

def db_create(database):
    try:
        logger.info("Starting to create the database if it doesn't exist.")
        if os.path.exists(database):
            logger.info("Database already exists. Skipping creation.")
            return
        connection = sqlite3.connect(database)
        logger.info(f"Database file '{database}' does not exist. Creating a new one.")
        cursor = connection.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                action TEXT,
                path TEXT,
                session INTEGER
            );
        ''')
        logger.info("Table 'changes' created successfully.")
        connection.commit()
        connection.close()
    except Exception as e:
        logger.error(f"Error during database creation: {e}")
        raise

def check_connection(database):
    try:
        conn = sqlite3.connect(database)
        conn.close()
        return True
    except sqlite3.Error as e:
        logger.error(f"Error connecting to database: {e}")
        return False

def list_items_by_session(database):
    last_session_number = get_last_session_number(database)
    if not check_connection(database):
        logger.info("Failed to connect to the database.")
        return []
    try:
        conn = sqlite3.connect(database)
        cursor = conn.cursor()
        query = "SELECT path FROM changes WHERE session = ?"
        cursor.execute(query, (last_session_number,))
        items = cursor.fetchall()
        paths = [item[0] for item in items]  # Converting fetched items to a list of paths
        conn.close()
        logger.debug(f"Item List for session {last_session_number}: {paths}")
        return paths
    except sqlite3.Error as e:
        logger.error(f"An error occurred: {e}")
        return []

def store_changes_in_db(changes):
    next_session_number = get_next_session_number(database)
    if changes is None or len(changes) == 0:
        logger.info("No changes to store.")
        return
    try:
        conn = sqlite3.connect(database, timeout=10.0)  # Set timeout to 10 seconds
        cursor = conn.cursor()
        cursor.execute("BEGIN TRANSACTION;")
        for action, path in changes:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute('INSERT INTO changes (timestamp, action, path, session) VALUES (?, ?, ?, ?)',
                        (timestamp, action, path, next_session_number))
        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        logger.error(f"Error storing changes in database: {e}")

def generate_incremental_folder():
    now = datetime.now()
    month_day_year = now.strftime('%b-%d-%y')
    hour = now.strftime('%I').lstrip('0')  # Remove leading zeros
    minute = now.strftime('%M')
    am_pm = now.strftime('%p')
    folder = f"{month_day_year} {hour}:{minute} {am_pm}"
    logger.debug(f"Generated incremental folder name: {folder}")
    return folder

def copy_files():
    folder = generate_incremental_folder()
    paths = list_items_by_session(database)
    last_session = get_last_session_number(database)
    if last_session > 1:
        logger.info(f"Folder Created: {folder}")
        logger.info(f"Paths to be copied: {paths}")
        for path in paths:
            src_path = os.path.join(src_dir, path)
            dst_path = os.path.join(base_dir, folder, path)
            try:
                if os.path.isdir(src_path):
                    shutil.copytree(src_path, dst_path, dirs_exist_ok=True)  # dirs_exist_ok=True handles existing directories
                    logger.success(f"Copied directory {src_path} to {dst_path}")
                else:
                    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
                    shutil.copy2(src_path, dst_path)
                    logger.success(f"Copied file {src_path} to {dst_path}")
            except Exception as e:
                logger.error(f"Error copying {src_path} to {dst_path}: {e}")
    else:
        logger.info("No files to copy.")

# Running the backup process with enhanced logging
try:
    db_create(database)
    logger.info("Database created successfully or already exists.")
    rsync()
    logger.info("Rsync completed successfully.")
    changes = parse_logfile(rsync_txt)
    logger.info(f"Log file parsed successfully. Changes: {changes}")
    store_changes_in_db(changes)
    logger.info("Changes stored in database successfully.")
    copy_files()
    logger.info("Files copied successfully.")
except Exception as e:
    logger.error(f"An unexpected error occurred: {e}")

logger.info(f"Full Backup is: {full_backup}")
logger.info(f"Last Session Number: {get_last_session_number(database)}")
logger.info(f"Next Session Number: {get_next_session_number(database)}")
logger.info(f"Items found on the last session: {list_items_by_session(database)}")
