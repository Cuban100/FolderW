import os
import sys
import time
import fnmatch
import threading
from db_operations import load_env_value, load_other_variables
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from loguru import logger
import subprocess
import schedule
import signal

# How long to wait after the *last* detected change before actually running
# a backup. Resets on every new event, so a burst of saves/writes collapses
# into a single backup once things go quiet, instead of firing repeatedly.
BACKUP_DELAY_SECONDS = 300

# Ceiling on how long a backup can be postponed by continuous activity.
# The resetting debounce above has no upper bound on its own -- on a live
# desktop, *something* inside the source tree (Docker container logs,
# browser cookie/webstorage journals, app logs) is essentially always
# being written, so a pure "wait for quiet" timer can end up never firing
# at all. Found live: watched a real run go 45+ minutes without a single
# incremental backup despite constant activity, because the 300s quiet
# window was never actually reached. This forces one through regardless
# of ongoing changes once too much time has passed since the first
# unflushed one. Set equal to BACKUP_DELAY_SECONDS on purpose: a backup
# always runs within 5 minutes of the first pending change, whether
# things go quiet by then or not.
MAX_BACKUP_DELAY_SECONDS = 300

rsync_txt = load_other_variables('rsync_txt')
logfile = load_other_variables('logfile')
src_dir = load_env_value('SRC_DIR')
backup_interval = load_env_value('BACKUP_INTERVAL')

def _load_exclude_patterns():
    # Reuses rsync's own exclude file (logs/rsync_exclude.txt) rather than
    # a separate list, so a change/tmp/cache directory only needs to be
    # excluded in one place to be skipped by both the actual backup and
    # the watchdog that triggers it — otherwise every write inside an
    # excluded directory (e.g. a busy browser cache) would still reset the
    # debounce timer for no reason, delaying real backups indefinitely.
    exclude_file = load_other_variables('exclude_file')
    patterns = []
    try:
        with open(exclude_file, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    patterns.append(line.rstrip('/'))
    except FileNotFoundError:
        pass
    return patterns

EXCLUDE_PATTERNS = _load_exclude_patterns()

def run_backup_script():
    try:
        python_path = sys.executable
        script_path = os.path.join(os.path.dirname(__file__), 'rsync_incremental.py')
        result = subprocess.run([python_path, script_path], check=True, capture_output=True, text=True)
        logger.info(f"Backup script output: {result.stdout}")
    except subprocess.CalledProcessError as e:
        logger.error(f"Backup script failed with error: {e.stderr}")

def run_hourly_backup():
    logger.info("Running hourly backup")
    run_backup_script()

def run_half_day_backup():
    logger.info("Running half-day backup")
    run_backup_script()

def run_daily_backup():
    logger.info("Running daily backup")
    run_backup_script()

def run_weekly_backup():
    logger.info("Running weekly backup")
    run_backup_script()

class BackupHandler(FileSystemEventHandler):
    def __init__(self, delay_seconds=BACKUP_DELAY_SECONDS, max_delay_seconds=MAX_BACKUP_DELAY_SECONDS):
        self.delay_seconds = delay_seconds
        self.max_delay_seconds = max_delay_seconds
        self.timer = None
        self.first_pending_change = None
        self.lock = threading.Lock()

    def _schedule_backup(self):
        run_now = False
        with self.lock:
            now = time.time()
            if self.first_pending_change is None:
                self.first_pending_change = now
            if now - self.first_pending_change >= self.max_delay_seconds:
                # Continuous activity has kept resetting the quiet-period
                # timer past the ceiling -- run now instead of waiting for
                # a lull that may never come.
                if self.timer is not None:
                    self.timer.cancel()
                self.first_pending_change = None
                run_now = True
            else:
                if self.timer is not None:
                    self.timer.cancel()
                self.timer = threading.Timer(self.delay_seconds, self._run_backup)
                self.timer.daemon = True
                self.timer.start()
        # Run outside the lock: this blocks for the whole backup (a real
        # rsync run), and holding the lock through that would stall every
        # other file-event thread trying to record a change in the meantime.
        if run_now:
            self._run_backup()

    def _run_backup(self):
        with self.lock:
            self.first_pending_change = None
        logger.info(f"Running backup after {self.delay_seconds}s of inactivity (or {self.max_delay_seconds}s ceiling reached)")
        run_backup_script()

    def on_modified(self, event):
        if not event.is_directory and not self.should_ignore(event.src_path):
            logger.debug(f"File modified: {event.src_path}, backup rescheduled for {self.delay_seconds}s from now")
            self._schedule_backup()

    def on_created(self, event):
        if not event.is_directory and not self.should_ignore(event.src_path):
            logger.debug(f"File created: {event.src_path}, backup rescheduled for {self.delay_seconds}s from now")
            self._schedule_backup()

    def on_deleted(self, event):
        if not event.is_directory and not self.should_ignore(event.src_path):
            logger.debug(f"File deleted: {event.src_path}, backup rescheduled for {self.delay_seconds}s from now")
            self._schedule_backup()

    def should_ignore(self, file_path):
        if file_path in (rsync_txt, logfile):
            return True
        try:
            rel_path = os.path.relpath(file_path, src_dir)
        except ValueError:
            rel_path = file_path
        parts = rel_path.split(os.sep)
        basename = os.path.basename(file_path)
        for pattern in EXCLUDE_PATTERNS:
            if fnmatch.fnmatch(rel_path, pattern) or fnmatch.fnmatch(basename, pattern):
                return True
            if any(fnmatch.fnmatch(part, pattern) for part in parts):
                return True
        return False

def notify_stop():
    logger.info("Observer stopped")
    try:
        result = subprocess.run(['/usr/bin/notify-send', 'Observer Stopped', 'The file system observer has been stopped.'], env=dict(os.environ, DISPLAY=":0"))
        logger.info(f"Notification sent with result: {result}")
    except (FileNotFoundError, OSError) as e:
        logger.debug(f"Desktop notification unavailable, skipping: {e}")

def handle_shutdown_signal(signum, frame):
    raise SystemExit(0)

if __name__ == "__main__":
    logger.info(f"Watching directory: {src_dir}")

    signal.signal(signal.SIGTERM, handle_shutdown_signal)
    signal.signal(signal.SIGINT, handle_shutdown_signal)
    signal.signal(signal.SIGQUIT, handle_shutdown_signal)

    event_handler = BackupHandler()
    observer = Observer()
    observer.schedule(event_handler, path=src_dir, recursive=True)
    observer.start()
    logger.info(f"Started watching directory: {src_dir}")

    if backup_interval == 'hourly':
        schedule.every().hour.do(run_hourly_backup)
    elif backup_interval == 'half-day':
        schedule.every(12).hours.do(run_half_day_backup)
    elif backup_interval == 'daily':
        schedule.every().day.do(run_daily_backup)
    elif backup_interval == 'weekly':
        schedule.every().week.do(run_weekly_backup)

    try:
        while True:
            schedule.run_pending()
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        observer.stop()
        notify_stop()

    observer.join()

