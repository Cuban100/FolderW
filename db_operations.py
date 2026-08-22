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
    elif variable_name == 'custom_exclude_file':
        # The single exclude-patterns file, fully user-editable from the
        # Settings page -- no separate hardcoded/non-editable exclude list
        # exists anymore (see git history for the old logs/rsync_exclude.txt
        # split). Always ensured to exist, since every caller passes this
        # straight to rsync/du's --exclude-from, which errors on a missing
        # file.
        path = os.path.join(os.path.dirname(__file__), 'logs/custom_exclude.txt')
        if not os.path.exists(path):
            open(path, 'a').close()
        return path
    elif variable_name == 'trigger_exempt_file':
        # Per-install, not shipped with any default content -- a file that
        # gets rewritten continuously by something else on one particular
        # machine (a monitoring agent, another app's own log) is specific
        # to that machine, not something every install should ship with
        # baked in. See rsync_event_handler.py's should_ignore(). Seeded
        # from the tracked .example template (explanatory comments only,
        # no entries) on first creation, same as .env/.env.example.
        path = os.path.join(os.path.dirname(__file__), 'logs/watchdog_trigger_exempt.txt')
        if not os.path.exists(path):
            example_path = path + '.example'
            if os.path.exists(example_path):
                shutil.copy2(example_path, path)
            else:
                open(path, 'a').close()
        return path
    else:
        # For environment variables
        return load_env_value(variable_name)


    
def get_database_value(name, table):
    database = load_env_value('DATABASE')
    try:
        conn = sqlite3.connect(database, timeout=10.0)
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
        conn = sqlite3.connect(database, timeout=10.0)
        cursor = conn.cursor()
        cursor.execute(f"REPLACE INTO {table} (name, value) VALUES (?, ?)", (name, str(value)))
        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        logger.error(f"Error saving {name} to table {table}: {e}")

def check_connection(database):
    try:
        conn = sqlite3.connect(database, timeout=10.0)
        return conn
    except sqlite3.Error as e:
        logger.error(f"Error connecting to database: {e}")
        return None

def create_all_tables(database):
    try:
        connection = sqlite3.connect(database, timeout=10.0)
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

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS restore_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                backup_id TEXT,
                backup_label TEXT,
                destination TEXT,
                file_count INTEGER,
                restore_type TEXT
            );
        ''')
        logger.info("Table 'restore_runs' created successfully.")

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS cloud_sync_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                remote TEXT,
                status TEXT,
                files_transferred INTEGER,
                bytes_transferred INTEGER,
                duration_seconds REAL,
                avg_speed_bps REAL,
                error_message TEXT,
                files_deleted INTEGER DEFAULT 0,
                files_added INTEGER DEFAULT 0
            );
        ''')
        logger.info("Table 'cloud_sync_runs' created successfully.")

        # Migration for installs where this table already existed before
        # files_deleted/files_added were added -- CREATE TABLE IF NOT
        # EXISTS above doesn't retroactively add columns to an existing
        # table. Confirmed live: a deletion-only sync (source file
        # removed, nothing to upload) recorded files_transferred=0,
        # reading as "nothing happened" despite rclone genuinely deleting
        # the file (-> files_deleted); separately, files_transferred
        # conflates genuinely new files with ones that already existed
        # and just got overwritten, overcounting "added" (-> files_added,
        # rclone's "Copied (new)" specifically, not "Copied (replaced
        # existing)" too).
        cursor.execute("PRAGMA table_info(cloud_sync_runs)")
        existing_columns = {row[1] for row in cursor.fetchall()}
        for column in ('files_deleted', 'files_added'):
            if column not in existing_columns:
                cursor.execute(f"ALTER TABLE cloud_sync_runs ADD COLUMN {column} INTEGER DEFAULT 0")
                logger.info(f"Migrated cloud_sync_runs: added {column} column.")

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
        conn = sqlite3.connect(database, timeout=10.0)
        cursor = conn.cursor()
        for table in ('changes', 'statistics', 'performance_metrics', 'backup_runs', 'restore_runs', 'cloud_sync_runs'):
            cursor.execute(f"DELETE FROM {table}")
        conn.commit()
        conn.close()
        # New identity means the initial full backup hasn't happened for
        # it yet -- without this, main_backup.py would see the flag still
        # set from the OLD source/destination and skip straight to
        # monitoring instead of actually syncing the new one.
        set_database_value('FULL_BACKUP_COMPLETED', '0')
        logger.info("Backup history reset (changes/statistics/performance_metrics/backup_runs/restore_runs cleared).")
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
        conn = sqlite3.connect(database, timeout=10.0)
        cursor = conn.cursor()
        for table in ('changes', 'statistics', 'performance_metrics', 'backup_runs', 'restore_runs', 'cloud_sync_runs', 'settings'):
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
        conn = sqlite3.connect(database, timeout=10.0)
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
        conn = sqlite3.connect(database, timeout=10.0)
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
        conn = sqlite3.connect(database, timeout=10.0)
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
        conn = sqlite3.connect(database, timeout=10.0)
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
        conn = sqlite3.connect(database, timeout=10.0)
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
        conn = sqlite3.connect(database, timeout=10.0)
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
        conn = sqlite3.connect(database, timeout=10.0)
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

def record_restore_run(backup_id, backup_label, destination, file_count, restore_type):
    """Log that a restore happened -- the Recovery History page's source
    of truth. Captures backup_label at restore time (not looked up
    later) since the source backup/snapshot can be pruned by retention
    or deleted afterward, at which point its id alone wouldn't resolve
    to anything meaningful anymore.
    """
    database = load_env_value('DATABASE')
    try:
        conn = sqlite3.connect(database, timeout=10.0)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO restore_runs (timestamp, backup_id, backup_label, destination, file_count, restore_type)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            backup_id,
            backup_label,
            destination,
            file_count,
            restore_type,
        ))
        conn.commit()
        conn.close()
        logger.success(f"Recorded restore run: '{backup_label}' ({restore_type}) -> {destination}, {file_count} file(s).")
    except sqlite3.Error as e:
        logger.error(f"Error recording restore run: {e}")

def count_restore_runs():
    database = load_env_value('DATABASE')
    try:
        conn = sqlite3.connect(database, timeout=10.0)
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM restore_runs')
        count = cursor.fetchone()[0]
        conn.close()
        return count
    except sqlite3.Error as e:
        logger.error(f"Error counting restore runs: {e}")
        return 0

def list_restore_runs(limit, offset):
    """Paginated, newest-first log of every restore recorded (see
    record_restore_run) -- grows forever, nothing prunes it, so this is
    paginated at the SQL level rather than fetching everything and
    slicing in Python (same reasoning as list_backup_runs).
    """
    database = load_env_value('DATABASE')
    try:
        conn = sqlite3.connect(database, timeout=10.0)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT timestamp, backup_id, backup_label, destination, file_count, restore_type
            FROM restore_runs ORDER BY id DESC LIMIT ? OFFSET ?
        ''', (limit, offset))
        rows = cursor.fetchall()
        conn.close()
        return [
            {
                "timestamp": r[0],
                "backup_id": r[1],
                "backup_label": r[2],
                "destination": r[3],
                "file_count": r[4],
                "restore_type": r[5],
            }
            for r in rows
        ]
    except sqlite3.Error as e:
        logger.error(f"Error listing restore runs: {e}")
        return []

def record_cloud_sync_run(remote, status, files_transferred=0, bytes_transferred=0, duration_seconds=0.0, avg_speed_bps=0.0, error_message=None, files_deleted=0, files_added=0):
    """Log one sync_to_cloud() attempt -- success or failure -- so the
    dashboard's Cloud Sync section has real history to show instead of
    just the single CLOUD_SYNC_LAST_STATUS/TIME settings values (which
    only ever reflect the most recent attempt, with no record of what
    happened before it or how much data actually moved).

    files_transferred/files_deleted/files_added all stay separate here
    (rclone itself reports them as distinct counters -- a deletion moves
    no bytes and isn't a "transfer"; files_added is the subset of
    files_transferred rclone logged as "Copied (new)" specifically, not
    "Copied (replaced existing)" too) even though the dashboard combines
    or reformats them for display -- confirmed live twice: a deletion-
    only sync recorded files_transferred=0, reading as "nothing
    happened" despite rclone genuinely deleting a file; separately, the
    Added/Deleted stat card showed files_transferred (new + replaced
    combined) as "added," overcounting whenever a run modified existing
    files rather than adding brand new ones.
    """
    database = load_env_value('DATABASE')
    try:
        conn = sqlite3.connect(database, timeout=10.0)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO cloud_sync_runs (timestamp, remote, status, files_transferred, bytes_transferred, duration_seconds, avg_speed_bps, error_message, files_deleted, files_added)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            remote,
            status,
            files_transferred,
            bytes_transferred,
            duration_seconds,
            avg_speed_bps,
            error_message,
            files_deleted,
            files_added,
        ))
        conn.commit()
        conn.close()
        logger.success(f"Recorded cloud sync run: {status} to {remote} ({files_transferred} file(s) transferred [{files_added} new], {files_deleted} deleted, {bytes_transferred} byte(s)).")
    except sqlite3.Error as e:
        logger.error(f"Error recording cloud sync run: {e}")

def list_cloud_sync_runs(limit=10):
    """Newest-first list of the last `limit` cloud sync attempts -- not
    paginated like list_backup_runs/list_restore_runs, since this only
    ever backs a short "recent activity" list on the dashboard, not a
    dedicated history page.
    """
    database = load_env_value('DATABASE')
    try:
        conn = sqlite3.connect(database, timeout=10.0)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT timestamp, remote, status, files_transferred, bytes_transferred, duration_seconds, avg_speed_bps, error_message, files_deleted, files_added
            FROM cloud_sync_runs ORDER BY id DESC LIMIT ?
        ''', (limit,))
        rows = cursor.fetchall()
        conn.close()
        return [
            {
                "timestamp": r[0],
                "remote": r[1],
                "status": r[2],
                "files_transferred": r[3],
                "bytes_transferred": r[4],
                "duration_seconds": r[5],
                "avg_speed_bps": r[6],
                "error_message": r[7],
                "files_deleted": r[8] or 0,
                "files_added": r[9] or 0,
            }
            for r in rows
        ]
    except sqlite3.Error as e:
        logger.error(f"Error listing cloud sync runs: {e}")
        return []

def get_cloud_sync_stats_series(limit=200):
    """Chronological (oldest-first) series for the Statistics page's Cloud
    Sync chart -- one entry per cloud_sync_runs row. Same id/DESC-then-
    reverse pattern as get_backup_stats_series(), for the same reason:
    capped at `limit` most recent runs since cloud_sync_runs grows
    forever, but still returned oldest-first within that window so the
    chart reads left-to-right chronologically.
    """
    database = load_env_value('DATABASE')
    try:
        conn = sqlite3.connect(database, timeout=10.0)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, timestamp, remote, status, files_transferred, bytes_transferred, duration_seconds, avg_speed_bps, error_message, files_deleted, files_added
            FROM cloud_sync_runs ORDER BY id DESC LIMIT ?
        ''', (limit,))
        rows = cursor.fetchall()
        conn.close()
        rows.reverse()
        return [
            {
                "id": r[0],
                "timestamp": r[1],
                "remote": r[2],
                "status": r[3],
                "files_transferred": r[4],
                "bytes_transferred": r[5],
                "duration_seconds": r[6],
                "avg_speed_bps": r[7],
                "error_message": r[8],
                "files_deleted": r[9] or 0,
                "files_added": r[10] or 0,
            }
            for r in rows
        ]
    except sqlite3.Error as e:
        logger.error(f"Error fetching cloud sync stats series: {e}")
        return []

def get_cloud_sync_stats_summary():
    """All-time KPI row for the Statistics page's Cloud Sync column --
    total syncs, success rate, and total data transferred across every
    sync ever recorded (not scoped to the `limit`-capped series above).
    Same shape/reasoning as get_backup_stats_summary().
    """
    database = load_env_value('DATABASE')
    try:
        conn = sqlite3.connect(database, timeout=10.0)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*), SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END), SUM(bytes_transferred) FROM cloud_sync_runs")
        total, succeeded, total_bytes = cursor.fetchone()
        conn.close()
        return {
            "total_runs": total or 0,
            "success_rate": round(100 * succeeded / total, 1) if total else None,
            "total_bytes_transferred": total_bytes or 0,
        }
    except sqlite3.Error as e:
        logger.error(f"Error fetching cloud sync stats summary: {e}")
        return {"total_runs": 0, "success_rate": None, "total_bytes_transferred": 0}

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
        conn = sqlite3.connect(database, timeout=10.0)
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
        conn = sqlite3.connect(database, timeout=10.0)
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
        conn = sqlite3.connect(database, timeout=10.0)
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
        connection = sqlite3.connect(database_file, timeout=10.0)
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