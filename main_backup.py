import os
from dotenv import load_dotenv
import subprocess
import schedule
import time
from loguru import logger

# Load configuration from .env file
load_dotenv()

# Debug log to check if .env file is loaded
logger.debug(".env file loaded")

# Get the MONITOR_SOURCE_FOLDER variable
MONITOR_SOURCE_FOLDER = os.getenv('MONITOR')
LOG_DIR = os.getenv("LOG_DIR")
logger.debug(f"MONITOR_SOURCE_FOLDER = {os.getenv('MONITOR')}")
logger.debug(f"LOG DIR: {LOG_DIR}")

# Check if MONITOR_SOURCE_FOLDER is correctly loaded
if MONITOR_SOURCE_FOLDER == '1':
    logger.debug("MONITOR_SOURCE_FOLDER is correctly set to '1'")
else:
    logger.debug("MONITOR_SOURCE_FOLDER is not '1', current value: " + MONITOR_SOURCE_FOLDER)

# Function to run the regular backup
def run_regular_backup():
    logger.info("Running regular backup with rsync_incremental.py")
    subprocess.run(["python", "rsync_incremental.py"])

# Function to start the event-driven backup
def start_event_backup():
    logger.info("Starting event-driven backup with rsync_event_handler.py")
    subprocess.run(["python", "rsync_event_handler.py"])

# Function to start the appropriate backup method
def start_backup():
    logger.debug(f"start_backup called with MONITOR_SOURCE_FOLDER = {MONITOR_SOURCE_FOLDER}")
    if MONITOR_SOURCE_FOLDER == '1':
        logger.info("Detected MONITOR_SOURCE_FOLDER as '1'. Starting event-driven backup.")
        start_event_backup()
    else:
        logger.info("Detected MONITOR_SOURCE_FOLDER as not '1'. Running regular backup.")
        run_regular_backup()

if __name__ == "__main__":
    logger.info("Starting backup process.")
    # Perform an immediate backup upon startup
    start_backup()

    # Schedule regular backup every hour if not monitoring the source folder
    if MONITOR_SOURCE_FOLDER != '1':
        schedule.every().hour.do(run_regular_backup)
        logger.info("Scheduled hourly regular backup")
    else:
        logger.info("Event-driven backup is active; hourly regular backup not scheduled.")

    while True:
        schedule.run_pending()
        time.sleep(1)
