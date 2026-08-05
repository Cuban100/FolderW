import os
import time
from db_operations import rsync_txt, logfile, src_dir, backup_interval
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from loguru import logger
import subprocess
import schedule
import signal

def run_backup_script():
    try:
        python_path = '/home/caveman/Server/bin/python3'
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

def notify_stop(signum, frame):
    logger.info("Observer stopped")
    result = subprocess.run(['/usr/bin/notify-send', 'Observer Stopped', 'The file system observer has been stopped.'], env=dict(os.environ, DISPLAY=":0"))
    logger.info(f"Notification sent with result: {result}")

if __name__ == "__main__":
    logger.info(f"Watching directory: {src_dir}")

    signal.signal(signal.SIGTERM, notify_stop)
    signal.signal(signal.SIGINT, notify_stop)
    signal.signal(signal.SIGQUIT, notify_stop)

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
    except KeyboardInterrupt:
        observer.stop()
        notify_stop(signal.SIGINT, None)
    except SystemExit:
        observer.stop()
        notify_stop(signal.SIGTERM, None)

    observer.join()

