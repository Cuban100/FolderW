import os
import time
from dotenv import load_dotenv
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from loguru import logger
import subprocess
import signal

load_dotenv()
rsync_txt = os.path.join(os.getenv('LOG_DIR'), 'rsync.txt')
logfile = os.path.join(os.getenv('LOG_DIR'), 'rsync.log')
src_dir = os.getenv('SRC_DIR')
class BackupHandler(FileSystemEventHandler):
    def __init__(self):
        self.last_event_time = 0
        self.debounce_time = 5  # in seconds

    def on_modified(self, event):
        if not event.is_directory and not self.is_log_file(event.src_path):
            current_time = time.time()
            if current_time - self.last_event_time > self.debounce_time:
                logger.info(f"File modified: {event.src_path}")
                self.run_backup_script()
                self.last_event_time = current_time

    def on_created(self, event):
        if not event.is_directory and not self.is_log_file(event.src_path):
            current_time = time.time()
            if current_time - self.last_event_time > self.debounce_time:
                logger.info(f"File created: {event.src_path}")
                self.run_backup_script()
                self.last_event_time = current_time

    def is_log_file(self, file_path):
        log_files = [rsync_txt, logfile]  # Add more log files if needed
        return file_path in log_files

    def run_backup_script(self):
        try:
            # Using the full path to the Python interpreter in the same environment
            python_path = '/home/caveman/Server/bin/python3'
            script_path = os.path.join(os.path.dirname(__file__), 'incremental_backup.py')
            result = subprocess.run([python_path, script_path], check=True, capture_output=True, text=True)
            logger.info(f"Backup script output: {result.stdout}")
        except subprocess.CalledProcessError as e:
            logger.error(f"Backup script failed with error: {e.stderr}")

def notify_stop(signum, frame):
    logger.info("Observer stopped")
    result = subprocess.run(['/usr/bin/notify-send', 'Observer Stopped', 'The file system observer has been stopped.'], env=dict(os.environ, DISPLAY=":0"))
    logger.info(f"Notification sent with result: {result}")

if __name__ == "__main__":
    # Debug logging to verify the path
    print(f"Watching directory: {src_dir}")
    logger.info(f"Watching directory: {src_dir}")

    signal.signal(signal.SIGTERM, notify_stop)
    signal.signal(signal.SIGINT, notify_stop)  # To handle keyboard interrupts as well
    signal.signal(signal.SIGQUIT, notify_stop)  # Catch additional signals for robust handling

    # Initialize the event handler and observer
    event_handler = BackupHandler()
    observer = Observer()
    observer.schedule(event_handler, path=src_dir, recursive=True)

    # Start the observer
    observer.start()
    logger.info(f"Started watching directory: {src_dir}")

    try:
        while True:
            time.sleep(1)  # Keep the script running
    except KeyboardInterrupt:
        observer.stop()
        notify_stop(signal.SIGINT, None)
    except SystemExit:
        observer.stop()
        notify_stop(signal.SIGTERM, None)

    observer.join()
