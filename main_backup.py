import os
import sys
from db_operations import load_env_value, load_other_variables, set_database_value
from notifications import notify
from statistics_operations import check_env_variables, validate_all_conditions, evaluation_of_resources
import subprocess
import schedule
import time
from loguru import logger

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def run_regular_backup():
    logger.info("Running regular backup with rsync_incremental.py")
    try:
        result = subprocess.run([sys.executable, os.path.join(BASE_DIR, "rsync_incremental.py")], check=True)
        logger.info(f"Backup result: {result}")
    except subprocess.CalledProcessError as e:
        # Centralized here rather than in rsync_incremental.py itself so
        # every failure mode is covered with a single notification — not
        # just rsync failing cleanly, but any unhandled crash in that
        # script too, since both surface the same way: a non-zero exit.
        logger.error(f"Error running rsync_incremental.py: {e}")
        notify("FolderW: Backup Failed", f"A backup run failed (exit code {e.returncode}). Check the FolderW logs for details.")

def start_event_backup():
    # rsync_event_handler.py blocks here for as long as it runs successfully
    # (its own watchdog Observer loop runs forever) — this call only returns
    # at all if it exits, whether cleanly (a deliberate shutdown signal) or
    # by crashing (e.g. a transient OS resource limit like inotify instances
    # being exhausted by other running apps). A crash used to mean giving up
    # on monitoring permanently and silently — retry with backoff instead,
    # since the underlying cause is often transient and clears up on its own.
    retry_delay = 30
    max_retry_delay = 300
    while True:
        logger.info("Starting event-driven backup with rsync_event_handler.py")
        try:
            result = subprocess.run([sys.executable, os.path.join(BASE_DIR, "rsync_event_handler.py")], check=True)
            logger.info(f"Event-driven backup result: {result}")
            break
        except subprocess.CalledProcessError as e:
            logger.error(f"Error running rsync_event_handler.py: {e}. Retrying in {retry_delay}s.")
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, max_retry_delay)

def backup_prerequisites_met():
    """The same three-step gate the web UI's /run-all-steps route already
    enforces before letting a user start a backup — settings present,
    source/destination paths valid, enough space. Without this, this
    worker ran a real backup the instant it started for ANY reason (a
    fresh install before setup is finished, the service enabled at boot,
    any manual restart) using whatever's in .env, with none of the checks
    a user clicking "Start Full Backup" in the browser gets first.
    """
    settings_sent, _settings, missing_vars = check_env_variables()
    if not settings_sent:
        logger.error(f"Backup worker started, but required settings are missing: {missing_vars}. Not running a backup.")
        return False

    validation_status, validation_message = validate_all_conditions(load_env_value('SRC_DIR'), load_env_value('BASE_DIR'))
    if not validation_status:
        logger.error(f"Backup worker started, but validation failed: {validation_message}. Not running a backup.")
        return False

    _src_size, _dest_space, can_backup, evaluation_message = evaluation_of_resources()
    if not can_backup:
        logger.error(f"Backup worker started, but evaluation failed: {evaluation_message}. Not running a backup.")
        return False

    return True

if __name__ == "__main__":
    monitor = load_env_value('MONITOR')
    backup_interval = load_env_value('BACKUP_INTERVAL')

    # Set before the checks below (which include a real du scan and can
    # take a while) so the dashboard's /backup-status has something to
    # report "running" during this window — without it, is_backup_running()
    # only recognizes rsync_incremental.py, which doesn't exist yet at this
    # point. The gap between this process starting and that one existing
    # used to read as "not running" to the frontend poll, which took it to
    # mean the backup had already finished and stopped polling, right as
    # the real backup was about to start. rsync_incremental.py clears this
    # once it takes over; the early-exit path below clears it too.
    set_database_value('BACKUP_PREPARING', '1')

    if not backup_prerequisites_met():
        logger.warning("Exiting without starting a backup or monitoring — fix settings/validation/evaluation via the dashboard, then start the backup service again (e.g. Start Full Backup once those checks pass).")
        set_database_value('BACKUP_PREPARING', '')
        sys.exit(0)

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