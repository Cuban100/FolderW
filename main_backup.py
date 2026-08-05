import os
from db_operations import load_env_value, load_other_variables
import subprocess
import schedule
import time
from loguru import logger

def run_regular_backup():
    logger.info("Running regular backup with rsync_incremental.py")
    try:
        result = subprocess.run(["python", "rsync_incremental.py"], check=True)
        logger.info(f"Backup result: {result}")
    except subprocess.CalledProcessError as e:
        logger.error(f"Error running rsync_incremental.py: {e}")

def start_event_backup():
    logger.info("Starting event-driven backup with rsync_event_handler.py")
    try:
        result = subprocess.run(["python", "rsync_event_handler.py"], check=True)
        logger.info(f"Event-driven backup result: {result}")
    except subprocess.CalledProcessError as e:
        logger.error(f"Error running rsync_event_handler.py: {e}")

if __name__ == "__main__":
    monitor = load_env_value('MONITOR')
    backup_interval = load_env_value('BACKUP_INTERVAL')

    run_regular_backup()

    if monitor == '1':
        logger.info("Detected MONITOR_SOURCE_FOLDER as '1'. Starting event-driven backup.")
        start_event_backup()
    else:
        logger.info(f"Detected MONITOR_SOURCE_FOLDER as '{monitor}'. Scheduling regular backups.")
        if backup_interval == 'hourly':
            schedule.every().hour.do(run_regular_backup)
            logger.info("Scheduled hourly regular backup")
        elif backup_interval == 'half-day':
            schedule.every(12).hours.do(run_regular_backup)
            logger.info("Scheduled half-day regular backup")
        elif backup_interval == 'daily':
            schedule.every().day.do(run_regular_backup)
            logger.info("Scheduled daily regular backup")
        elif backup_interval == 'weekly':
            schedule.every().week.do(run_regular_backup)
            logger.info("Scheduled weekly regular backup")
        else:
            logger.error(f"Unsupported backup interval: {backup_interval}")

    while True:
        schedule.run_pending()
        time.sleep(1)

logger.info("main_backup.py executed")