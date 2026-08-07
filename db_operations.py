import sqlite3
from dotenv import load_dotenv, find_dotenv, set_key
import os
import shutil
from loguru import logger
from datetime import datetime


def ensure_trailing_slash(path):
    """Ensure the given path ends with a trailing slash."""
    if path and not path.endswith('/'):
        path += '/'
    return path

def load_env_value(variable_name):
    """
    example usage:
    database = load_env_value('DATABASE')
    """
    # Ensure to reload the .env file
    dotenv_path = find_dotenv()
    load_dotenv(dotenv_path, override=True)
    
    value = os.getenv(variable_name)
    if value is None:
        logger.error(f"Environment variable '{variable_name}' not found.")
    return value





def save_env_values(values):
    """
    example usage:
    save_env_values({'SRC_DIR': '/home/user/data', 'MONITOR': '1'})
    """
    env_path = find_dotenv()
    if not env_path:
        env_path = os.path.join(os.path.dirname(__file__), '.env')
        open(env_path, 'a').close()
    for key, value in values.items():
        set_key(env_path, key, str(value))


def load_other_variables(variable_name):
    '''
    example usage:
    full_backup = load_other_variables('full_backup')
    '''
    load_dotenv()

    if variable_name == 'full_backup':
        base_dir = ensure_trailing_slash(load_env_value('BASE_DIR'))
        return os.path.join(base_dir, 'Full Backup')
    elif variable_name == 'snapshots_root':
        base_dir = load_env_value('BASE_DIR')
        return os.path.join(base_dir, 'Snapshots', 'Months')
    elif variable_name == 'env_file':
        return os.path.join(os.path.dirname(__file__), '.env')
    elif variable_name == 'logfile':
        return os.path.join(os.path.dirname(__file__), 'logs/rsync.log')
    elif variable_name == 'rsync_txt':
        return os.path.join(os.path.dirname(__file__), 'logs/rsync.txt')
    elif variable_name == 'exclude_file':
        return os.path.join(os.path.dirname(__file__), 'logs/rsync_exclude.txt')
    else:
        # For environment variables
        return load_env_value(variable_name)


    
def get_database_value(name, table):
    database = load_env_value('DATABASE')
    try:
        conn = sqlite3.connect(database)
        cursor = conn.cursor()
        query = f'SELECT value FROM {table} WHERE name = ?'
        cursor.execute(query, (name,))
        result = cursor.fetchone()
        conn.close()
        if result:
            return result[0]
        else:
            logger.error(f"{name} not found in table {table}.")
            return None
    except sqlite3.Error as e:
        logger.error(f"Error retrieving {name} from table {table}: {e}")
        return None

def set_database_value(name, value, table='settings'):
    database = load_env_value('DATABASE')
    try:
        conn = sqlite3.connect(database)
        cursor = conn.cursor()
        cursor.execute(f"REPLACE INTO {table} (name, value) VALUES (?, ?)", (name, str(value)))
        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        logger.error(f"Error saving {name} to table {table}: {e}")

def check_connection(database):
    try:
        conn = sqlite3.connect(database)
        return conn
    except sqlite3.Error as e:
        logger.error(f"Error connecting to database: {e}")
        return None

def create_all_tables(database):
    try:
        connection = sqlite3.connect(database)
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

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS statistics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                total_files_processed INTEGER,
                total_size_processed INTEGER,
                source_size INTEGER, 
                destination_size INTEGER,
                deleted_empty_folders INTEGER,
                average_speed REAL
            );
        ''')
        logger.info("Table 'statistics' created successfully.")

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS performance_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                backup_duration INTEGER,
                success_rate REAL,
                average_speed REAL,
                sent_bytes INTEGER,
                received_bytes INTEGER,
                total_size INTEGER,
                speedup REAL
            );
        ''')
        logger.info("Table 'performance_metrics' created successfully.")

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                value TEXT NOT NULL
            );
        ''')
        logger.info("Table 'settings' created successfully.")

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS backup_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                session INTEGER,
                backup_type TEXT,
                label TEXT,
                files_changed INTEGER,
                status TEXT
            );
        ''')
        logger.info("Table 'backup_runs' created successfully.")

        connection.commit()
        connection.close()
        logger.info("All tables created successfully.")
    except sqlite3.Error as e:
        logger.error(f"SQLite error: {e}")
    except Exception as e:
        logger.error(f"Failed to create tables: {e}")

def get_next_session_number():
    database = load_env_value('DATABASE')
    last_session_number = get_last_session_number(database)
    next_session_number = last_session_number + 1
    return next_session_number

def get_last_session_number(database):
    try:
        conn = sqlite3.connect(database, timeout=10.0)
        cursor = conn.cursor()
        cursor.execute('SELECT session FROM changes ORDER BY id DESC LIMIT 1')
        row = cursor.fetchone()
        conn.close()
        if row:
            return row[0]
        else:
            return 0
    except sqlite3.Error as e:
        logger.error(f"Error getting last session number: {e}")
        return 0

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
        paths = [item[0] for item in items]
        conn.close()
        return paths
    except sqlite3.Error as e:
        logger.error(f"An error occurred: {e}")
        return []

def store_changes_in_db(changes):
    database = load_env_value('DATABASE')
    next_session_number = get_next_session_number()
    if changes is None or len(changes) == 0:
        logger.info("No changes to store.")
        return
    try:
        conn = sqlite3.connect(database)
        cursor = conn.cursor()
        cursor.execute("BEGIN TRANSACTION;")
        for action, path in changes:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute('INSERT INTO changes (timestamp, action, path, session) VALUES (?, ?, ?, ?)',
                        (timestamp, action, path, next_session_number))
        conn.commit()
        conn.close()
        logger.success("Changes stored in database successfully.")
    except sqlite3.Error as e:
        logger.error(f"Error storing changes in database: {e}")


def record_backup_run(session, backup_type, label, files_changed, status='completed'):
    """Log that a backup run happened — the full backup or a specific
    incremental snapshot — independent of how many files changed. This is
    the source of truth for "was this backup executed", separate from the
    numeric per-run stats in the statistics table.
    """
    database = load_env_value('DATABASE')
    try:
        conn = sqlite3.connect(database)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO backup_runs (timestamp, session, backup_type, label, files_changed, status)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            session,
            backup_type,
            label,
            files_changed,
            status,
        ))
        conn.commit()
        conn.close()
        logger.success(f"Recorded backup run: {backup_type} '{label}' (session {session}, {files_changed} file(s) changed).")
    except sqlite3.Error as e:
        logger.error(f"Error recording backup run: {e}")

def save_settings_to_db(log_directory, source_directory, base_backup_directory, database_file, monitor_source_folder, backup_interval):
    try:
        connection = sqlite3.connect(database_file)
        cursor = connection.cursor()
        settings = [
            ("LOG_DIRECTORY", log_directory),
            ("SOURCE_DIRECTORY", source_directory),
            ("BASE_BACKUP_DIRECTORY", base_backup_directory),
            ("DATABASE_FILE", database_file),
            ("MONITOR_SOURCE_FOLDER", str(monitor_source_folder)),
            ("BACKUP_INTERVAL", backup_interval)
        ]
        for name, value in settings:
            cursor.execute("REPLACE INTO settings (name, value) VALUES (?, ?)", (name, value))
        connection.commit()
        connection.close()
    except sqlite3.Error as e:
        logger.error(f"Error saving settings to database: {e}")

logger.info("db_operations.py executed")