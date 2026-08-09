import os
import sys
import time
import fnmatch
import threading
from db_operations import load_env_value, load_other_variables, set_database_value
from backup_hooks import run_backup_script_with_hooks
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from loguru import logger
import subprocess
import schedule
import signal

# User-configurable via Settings' "Watchdog Delay" slider
# (MONITORING_DELAY_SECONDS in .env; server.py clamps it to
# MONITORING_DELAY_MIN/MAX on save). Falls back to the original
# hardcoded default (120s) if unset or somehow invalid -- this module
# is a standalone script (re-executed fresh per watchdog run, not just
# imported), so it can't assume .env has always been through that
# validation.
MONITORING_DELAY_MIN = 30
MONITORING_DELAY_MAX = 1800

def _load_monitoring_delay_seconds():
    try:
        value = int(load_env_value('MONITORING_DELAY_SECONDS'))
        return max(MONITORING_DELAY_MIN, min(MONITORING_DELAY_MAX, value))
    except (TypeError, ValueError):
        return 120

# How long to wait after the *last* detected change before actually running
# a backup, and also the ceiling on how long a backup can be postponed by
# continuous activity (the resetting debounce above has no upper bound on
# its own -- on a live desktop, *something* inside the source tree (Docker
# container logs, browser cookie/webstorage journals, app logs) is
# essentially always being written, so a pure "wait for quiet" timer can
# end up never firing at all -- found live: watched a real run go 45+
# minutes without a single incremental backup despite constant activity).
# Deliberately the same configured value for both, not two separate
# settings: a backup always runs within this many seconds of the first
# pending change, whether things go quiet by then or not.
#
# Read live via BackupHandler.delay_seconds/max_delay_seconds (properties,
# below) rather than cached once at import time -- this process runs for
# as long as the watchdog is active (hours/days), and a value frozen at
# startup meant the Settings page's Watchdog Delay slider silently had no
# effect on an already-running watchdog until the service was restarted.

rsync_txt = load_other_variables('rsync_txt')
logfile = load_other_variables('logfile')
src_dir = load_env_value('SRC_DIR')
backup_interval = load_env_value('BACKUP_INTERVAL')

# FolderW's own working directory (wherever this file actually lives),
# not a hardcoded name -- an install can be cloned into any folder name
# at all, so matching on a literal string here would only ever work for
# whichever name one particular install happened to use.
APP_DIR_REAL = os.path.realpath(os.path.dirname(os.path.abspath(__file__)))

def _load_patterns(path):
    patterns = []
    try:
        with open(path, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    patterns.append(line.rstrip('/'))
    except FileNotFoundError:
        pass
    return patterns

def _load_exclude_patterns():
    # Reuses rsync's own exclude file (logs/custom_exclude.txt) rather than
    # a separate list, so a change/tmp/cache directory only needs to be
    # excluded in one place to be skipped by both the actual backup and
    # the watchdog that triggers it — otherwise every write inside an
    # excluded directory (e.g. a busy browser cache) would still reset the
    # debounce timer for no reason, delaying real backups indefinitely.
    return _load_patterns(load_other_variables('custom_exclude_file'))

EXCLUDE_PATTERNS = _load_exclude_patterns()

# Separate from EXCLUDE_PATTERNS on purpose: these files ARE still backed
# up normally (rsync never sees this list), they just don't get to
# trigger/reschedule a backup on their own. For files that are constantly
# rewritten by a long-running host/container service regardless of real
# user activity (confirmed, not guessed -- see the file's own header) --
# without this, that alone is enough to keep the watchdog's ceiling
# (BackupHandler.max_delay_seconds, above) firing around the clock.
TRIGGER_EXEMPT_PATTERNS = _load_patterns(load_other_variables('trigger_exempt_file'))

def run_backup_script():
    try:
        python_path = sys.executable
        # See main_backup.py's _backup_script_name() for the same choice --
        # kept in lockstep so event-driven and scheduled/manual runs always
        # honor the same BACKUP_METHOD setting.
        script_name = "rsync_incremental.py" if load_env_value('BACKUP_METHOD') == 'incremental' else "rsync_differential.py"
        script_path = os.path.join(os.path.dirname(__file__), script_name)
        result = run_backup_script_with_hooks([python_path, script_path], check=True, capture_output=True, text=True)
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
    def __init__(self):
        self.timer = None
        self.first_pending_change = None
        self.lock = threading.Lock()

    @property
    def delay_seconds(self):
        # Re-read live (see module comment above) so a Watchdog Delay
        # change on the Settings page takes effect on the very next
        # event, without needing to restart the watchdog process.
        return _load_monitoring_delay_seconds()

    @property
    def max_delay_seconds(self):
        return self.delay_seconds

    def _schedule_backup(self, event_path=None):
        run_now = False
        # Read once per call, not once per use below -- delay_seconds is a
        # live property (re-parses .env), and this can fire dozens of
        # times a second during a burst of events.
        delay_seconds = self.delay_seconds
        with self.lock:
            now = time.time()
            if self.first_pending_change is None:
                self.first_pending_change = now
            # Persisted so the dashboard (a separate process) can show a
            # live "last change detected" + "backup in Xs" countdown --
            # this handler's own state otherwise only ever existed in
            # this process's memory, invisible from outside it.
            set_database_value('WATCHDOG_LAST_EVENT_TIME', str(now))
            if event_path is not None:
                set_database_value('WATCHDOG_LAST_EVENT_PATH', event_path)
            if now - self.first_pending_change >= delay_seconds:
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
                self.timer = threading.Timer(delay_seconds, self._run_backup)
                self.timer.daemon = True
                self.timer.start()
                set_database_value('WATCHDOG_NEXT_BACKUP_TIME', str(now + delay_seconds))
        # Run outside the lock: this blocks for the whole backup (a real
        # rsync run), and holding the lock through that would stall every
        # other file-event thread trying to record a change in the meantime.
        if run_now:
            self._run_backup()

    def _run_backup(self):
        with self.lock:
            self.first_pending_change = None
        set_database_value('WATCHDOG_NEXT_BACKUP_TIME', '')
        logger.info(f"Running backup after {self.delay_seconds}s of inactivity (or {self.max_delay_seconds}s ceiling reached)")
        run_backup_script()

    def on_modified(self, event):
        if not event.is_directory and not self.should_ignore(event.src_path):
            logger.debug(f"File modified: {event.src_path}, backup rescheduled for {self.delay_seconds}s from now")
            self._schedule_backup(event.src_path)

    def on_created(self, event):
        # Directories ARE let through here (unlike on_modified below): a
        # newly created but still-empty folder has no file inside it yet
        # to fire its own on_created event, so this is the only signal
        # that folder exists at all. Found live: creating a new folder
        # in SRC_DIR triggered nothing, because this used to skip
        # directory events unconditionally.
        if not self.should_ignore(event.src_path):
            logger.debug(f"{'Directory' if event.is_directory else 'File'} created: {event.src_path}, backup rescheduled for {self.delay_seconds}s from now")
            self._schedule_backup(event.src_path)

    def on_deleted(self, event):
        # Same reasoning as on_created -- a whole folder being removed
        # is a real change (Incremental mode's --delete needs a run to
        # actually propagate it), and there's no separate per-file event
        # for the files that were inside it if the folder itself is gone.
        if not self.should_ignore(event.src_path):
            logger.debug(f"{'Directory' if event.is_directory else 'File'} deleted: {event.src_path}, backup rescheduled for {self.delay_seconds}s from now")
            self._schedule_backup(event.src_path)

    @staticmethod
    def _matches_any(patterns, rel_path, parts, basename):
        for pattern in patterns:
            if fnmatch.fnmatch(rel_path, pattern) or fnmatch.fnmatch(basename, pattern):
                return True
            if any(fnmatch.fnmatch(part, pattern) for part in parts):
                return True
            # Multi-segment patterns (e.g. "**/.local/share/docker") only
            # ever matched here if rel_path ended exactly there -- a file
            # nested any deeper (.local/share/docker/containers/<id>/...)
            # slipped through every check above and kept resetting the
            # debounce timer. rsync itself excludes the whole subtree once
            # a directory matches; mirror that by checking whether the
            # pattern's components appear as a contiguous run anywhere in
            # rel_path's components, not just as a full-path suffix.
            anchor = pattern[3:] if pattern.startswith('**/') else pattern
            anchor_parts = anchor.split('/')
            if len(anchor_parts) > 1:
                for i in range(len(parts) - len(anchor_parts) + 1):
                    if all(fnmatch.fnmatch(parts[i + j], anchor_parts[j]) for j in range(len(anchor_parts))):
                        return True
        return False

    def should_ignore(self, file_path):
        if file_path in (rsync_txt, logfile):
            return True
        # FolderW's own working directory, if it happens to sit inside
        # SRC_DIR -- a real path check, not a name match, so it's correct
        # regardless of what the install folder is actually called. See
        # APP_DIR_REAL above. Confirmed live: FolderW's own progress-
        # reporting writes to its database file during a run were, on
        # their own, enough to keep re-arming the debounce ceiling before
        # the run even finished, producing a new backup right after the
        # previous one, indefinitely, with zero real user activity
        # involved. Still fully included in every backup like any other
        # file -- this only stops a write to it from scheduling one.
        real_path = os.path.realpath(file_path)
        if real_path == APP_DIR_REAL or real_path.startswith(APP_DIR_REAL + os.sep):
            return True
        try:
            rel_path = os.path.relpath(file_path, src_dir)
        except ValueError:
            rel_path = file_path
        parts = rel_path.split(os.sep)
        basename = os.path.basename(file_path)
        if self._matches_any(EXCLUDE_PATTERNS, rel_path, parts, basename):
            return True
        # Trigger-exempt files are NOT excluded from rsync -- only checked
        # here, so a write to one still can't reschedule a backup, but the
        # backup itself (whenever one runs for another reason) still picks
        # up its current contents like any other included file.
        if self._matches_any(TRIGGER_EXEMPT_PATTERNS, rel_path, parts, basename):
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
    # No pending timer yet in this fresh process -- clear any stale
    # value a previous run might have left (e.g. killed mid-countdown),
    # so the dashboard doesn't show a countdown to a time that will
    # never actually fire.
    set_database_value('WATCHDOG_NEXT_BACKUP_TIME', '')

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

