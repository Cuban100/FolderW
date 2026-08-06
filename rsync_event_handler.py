import os
import sys
import time
from db_operations import load_env_value, load_other_variables
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from loguru import logger
import subprocess
import schedule
import signal

rsync_txt = load_other_variables('rsync_txt')
logfile = load_other_variables('logfile')
src_dir = load_env_value('SRC_DIR')
backup_interval = load_env_value('BACKUP_INTERVAL')

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
    def __init__(self):
        self.last_event_time = 0
        self.debounce_time = 5  # in seconds

    def on_modified(self, event):
        if not event.is_directory and not self.is_log_file(event.src_path):
            current_time = time.time()
            if current_time - self.last_event_time > self.debounce_time:
                logger.info(f"File modified: {event.src_path}")
                run_backup_script()
                self.last_event_time = current_time

    def on_created(self, event):
        if not event.is_directory and not self.is_log_file(event.src_path):
            current_time = time.time()
            if current_time - self.last_event_time > self.debounce_time:
                logger.info(f"File created: {event.src_path}")
                run_backup_script()
                self.last_event_time = current_time

    def is_log_file(self, file_path):
        log_files = [rsync_txt, logfile]
        return file_path in log_files

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

