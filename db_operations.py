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
        base_dir = load_env_value('BASE_DIR')
        full_name = load_env_value('FULL_NAME')
        return os.path.join(base_dir, full_name, 'Full Backup')
    elif variable_name == 'snapshots_root':
        base_dir = load_env_value('BASE_DIR')
        full_name = load_env_value('FULL_NAME')
        return os.path.join(base_dir, full_name, 'Snapshots')
    elif variable_name == 'env_file':
        return os.path.join(os.path.dirname(__file__), '.env')
    elif variable_name == 'logfile':
        return os.path.join(os.path.dirname(__file__), 'logs/rsync.log')
    elif variable_name == 'rsync_txt':
        return os.path.join(os.path.dirname(__file__), 'logs/rsync.txt')
    elif variable_name == 'exclude_file':
        return os.path.join(os.path.dirname(__file__), 'logs/rsync_exclude.txt')
    elif variable_name == 'custom_exclude_file':
        # User-editable exclusions (Settings page), kept separate from the
        # developer-curated logs/rsync_exclude.txt above so a user's own
        # patterns never get mixed into (or wipe out) that file. Always
        # ensured to exist, since every caller passes this straight to
        # rsync/du's --exclude-from, which errors on a missing file.
        path = os.path.join(os.path.dirname(__file__), 'logs/custom_exclude.txt')
        if not os.path.exists(path):
            open(path, 'a').close()
        return path
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

def reset_backup_history(database):
    """Wipe recorded change/session/statistics history. Called when SRC_DIR,
    BASE_DIR, or FULL_NAME changes — i.e. the user pointed FolderW at a
    different backup — since old sessions reference a source/destination
    that no longer applies and mixing them in would corrupt session
    numbering (incremental folder naming) and historical stats/backup runs.
    """
    try:
        conn = sqlite3.connect(database)
        cursor = conn.cursor()
        for table in ('changes', 'statistics', 'performance_metrics', 'backup_runs'):
            cursor.execute(f"DELETE FROM {table}")
        conn.commit()
        conn.close()
        # New identity means the initial full backup hasn't happened for
        # it yet -- without this, main_backup.py would see the flag still
        # set from the OLD source/destination and skip straight to
        # monitoring instead of actually syncing the new one.
        set_database_value('FULL_BACKUP_COMPLETED', '0')
        logger.info("Backup history reset (changes/statistics/performance_metrics/backup_runs cleared).")
    except sqlite3.Error as e:
        logger.error(f"Error resetting backup history: {e}")

def wipe_database_for_fresh_start(database):
    """Full reset for "start from scratch, same settings" (the Manage
    Databases page's database-reset action) -- unlike reset_backup_
    history() (identity changes only, four tables), this also clears
    'settings' -- every piece of runtime/derived state stored in the
    database (FULL_BACKUP_COMPLETED, cached sizes, persisted Settings/
    Validation/Evaluation check results, CURRENT_BACKUP_SIZE, etc.), not
    just change/session/stats history. .env itself -- the actual
    configured settings: paths, password, notification URLs -- is a
    separate file, untouched by this.
    """
    try:
        conn = sqlite3.connect(database)
        cursor = conn.cursor()
        for table in ('changes', 'statistics', 'performance_metrics', 'backup_runs', 'settings'):
            cursor.execute(f"DELETE FROM {table}")
        conn.commit()
        conn.close()
        logger.info(f"Database {database} fully reset for a fresh start (all tables cleared).")
    except sqlite3.Error as e:
        logger.error(f"Error resetting database {database} for a fresh start: {e}")

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


def has_completed_backup(database):
    """Whether at least one backup has actually finished for the current
    backup identity. Backed by backup_runs rather than a separate flag so
    it automatically goes back to False when reset_backup_history() clears
    it after SRC_DIR/BASE_DIR/FULL_NAME changes — a different backup means
    starting over, verification widgets and all.
    """
    try:
        conn = sqlite3.connect(database)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM backup_runs WHERE status = 'completed'")
        count = cursor.fetchone()[0]
        conn.close()
        return count > 0
    except sqlite3.Error as e:
        logger.error(f"Error checking backup history: {e}")
        return False

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

def get_files_changed_by_label(label):
    """files_changed for the backup_runs row matching this snapshot's
    folder id (see record_backup_run's label -- always the real
    Month/Day/Time id now, not a special-cased "Full Backup" string), so
    the Restore page can show how many files were actually new/modified
    in a given snapshot, not just its total file count. None if no
    matching row exists (a snapshot that predates this being tracked, or
    the DB was reset separately from the filesystem).
    """
    database = load_env_value('DATABASE')
    try:
        conn = sqlite3.connect(database)
        cursor = conn.cursor()
        cursor.execute('SELECT files_changed FROM backup_runs WHERE label = ? ORDER BY id DESC LIMIT 1', (label,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None
    except sqlite3.Error as e:
        logger.error(f"Error looking up files_changed for {label}: {e}")
        return None

def count_backup_runs():
    database = load_env_value('DATABASE')
    try:
        conn = sqlite3.connect(database)
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM backup_runs')
        count = cursor.fetchone()[0]
        conn.close()
        return count
    except sqlite3.Error as e:
        logger.error(f"Error counting backup runs: {e}")
        return 0

def list_backup_runs(limit, offset):
    """Paginated, newest-first log of every backup run recorded (see
    record_backup_run) -- unlike the Restore page's snapshot list, this
    grows forever (nothing prunes backup_runs the way cleanup_old_
    snapshots prunes the filesystem), so it's paginated at the SQL level
    (LIMIT/OFFSET) rather than fetching everything and slicing in Python.
    """
    database = load_env_value('DATABASE')
    try:
        conn = sqlite3.connect(database)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT timestamp, session, backup_type, label, files_changed, status
            FROM backup_runs ORDER BY id DESC LIMIT ? OFFSET ?
        ''', (limit, offset))
        rows = cursor.fetchall()
        conn.close()
        return [
            {
                "timestamp": r[0],
                "session": r[1],
                "backup_type": r[2],
                "label": r[3],
                "files_changed": r[4],
                "status": r[5],
            }
            for r in rows
        ]
    except sqlite3.Error as e:
        logger.error(f"Error listing backup runs: {e}")
        return []

def get_backup_stats_series(limit=200):
    """Chronological (oldest-first) series for the Statistics page's charts
    -- one entry per backup run, combining backup_runs (type, files_changed,
    status), statistics (total_size_processed, destination_size), and
    performance_metrics (backup_duration).

    statistics/backup_runs joined on id, not a real foreign key: they're
    only ever inserted into together, in the same order, by a single call
    site (record_backup_statistics() in rsync_incremental.py inserts into
    statistics, then calls record_backup_run() for backup_runs), and only
    ever cleared together (reset_backup_history(), wipe_database_for_
    fresh_start()) -- confirmed no other code path writes to either table,
    so their id sequences stay aligned by construction.

    performance_metrics joined on timestamp instead: it sat completely
    unused (zero rows, ever) until duration tracking was added alongside
    this function, so its id sequence starts fresh at 1 rather than
    lining up with statistics/backup_runs' already-existing rows --
    record_backup_statistics() captures one timestamp and reuses it for
    both the statistics and performance_metrics inserts specifically so
    this join has something exact to match on. Runs recorded before
    duration tracking existed simply have no performance_metrics row and
    show a None duration.

    Capped at `limit` most recent runs (still returned oldest-first within
    that window) since backup_runs grows forever and a chart doesn't need
    every run ever made to be useful.
    """
    database = load_env_value('DATABASE')
    try:
        conn = sqlite3.connect(database)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT br.id, br.timestamp, br.session, br.backup_type, br.files_changed, br.status,
                   s.total_size_processed, s.destination_size, pm.backup_duration
            FROM backup_runs br
            LEFT JOIN statistics s ON s.id = br.id
            LEFT JOIN performance_metrics pm ON pm.timestamp = br.timestamp
            ORDER BY br.id DESC LIMIT ?
        ''', (limit,))
        rows = cursor.fetchall()
        conn.close()
        rows.reverse()
        return [
            {
                "id": r[0],
                "timestamp": r[1],
                "session": r[2],
                "backup_type": r[3],
                "files_changed": r[4],
                "status": r[5],
                "size_processed": r[6] or 0,
                "destination_size": r[7],
                "duration_seconds": r[8],
            }
            for r in rows
        ]
    except sqlite3.Error as e:
        logger.error(f"Error fetching backup stats series: {e}")
        return []

def get_backup_stats_summary():
    """All-time KPI row for the Statistics page -- total runs, full-backup
    count, success rate, and total data processed across every run ever
    recorded (not scoped to the `limit`-capped series above).
    """
    database = load_env_value('DATABASE')
    try:
        conn = sqlite3.connect(database)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*), SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) FROM backup_runs")
        total, completed = cursor.fetchone()
        cursor.execute("SELECT COUNT(*) FROM backup_runs WHERE backup_type = 'full'")
        full_count = cursor.fetchone()[0]
        cursor.execute("SELECT SUM(total_size_processed) FROM statistics")
        total_size = cursor.fetchone()[0]
        conn.close()
        return {
            "total_runs": total or 0,
            "full_runs": full_count or 0,
            "success_rate": round(100 * completed / total, 1) if total else None,
            "total_size_processed": total_size or 0,
        }
    except sqlite3.Error as e:
        logger.error(f"Error fetching backup stats summary: {e}")
        return {"total_runs": 0, "full_runs": 0, "success_rate": None, "total_size_processed": 0}

def get_changes_by_session(session):
    """Files changed in one specific backup run -- like list_items_by_
    session(), but for an arbitrary session number rather than always the
    most recent one, so the Statistics page's drill-down can show the
    file list for whichever run was clicked, not just the latest.
    """
    database = load_env_value('DATABASE')
    if not check_connection(database):
        return []
    try:
        conn = sqlite3.connect(database)
        cursor = conn.cursor()
        cursor.execute('SELECT path, action, timestamp FROM changes WHERE session = ? ORDER BY id', (session,))
        rows = cursor.fetchall()
        conn.close()
        return [{"path": r[0], "action": r[1], "timestamp": r[2]} for r in rows]
    except sqlite3.Error as e:
        logger.error(f"Error fetching changes for session {session}: {e}")
        return []

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