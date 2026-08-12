import os
import shutil
import subprocess
import time
from db_operations import load_env_value, load_other_variables, set_database_value
from notifications import notify
from loguru import logger

CLOUD_SYNC_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs', 'cloud_sync.log')

# rclone's own network-facing timeouts, not a Python-side subprocess
# timeout -- a real sync has genuinely unbounded duration (hours, on a
# slow upload link), so a fixed wall-clock cap would have to be
# arbitrarily generous anyway. These instead abort on an actual stall:
# no data moved in 5 minutes, or the initial connection itself hangs.
RCLONE_TIMEOUT = '300s'
RCLONE_CONTIMEOUT = '30s'


def rclone_binary_path():
    return shutil.which('rclone')


def list_rclone_remotes():
    """(installed: bool, remotes: list[str]) -- local and instant (no
    network), safe to call on every /cloud-backup page load. Remote
    names have their trailing ':' stripped (`rclone listremotes` prints
    one 'Name:' per line).
    """
    rclone_path = rclone_binary_path()
    if not rclone_path:
        return False, []
    try:
        result = subprocess.run([rclone_path, 'listremotes'], capture_output=True, text=True, timeout=10)
        remotes = [line.strip().rstrip(':') for line in result.stdout.splitlines() if line.strip()]
        return True, remotes
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning(f"Could not list rclone remotes: {e}")
        return True, []


def test_cloud_connection(remote, remote_path):
    """(success, message) for the Cloud Backup page's Test Connection
    button -- tests the CURRENTLY TYPED, possibly-unsaved remote/path,
    same convention as Backup Hooks' Test Script and Settings' Test
    Notification. `rclone lsd` (list directories, one level deep) rather
    than `rclone about`, since `about` isn't implemented by every
    backend (e.g. a local-filesystem remote) -- `lsd` works uniformly
    and moves no data.
    """
    rclone_path = rclone_binary_path()
    if not rclone_path:
        return False, "rclone not found on this machine."
    remote = (remote or '').strip()
    if not remote:
        return False, "No remote selected."
    remote_path = (remote_path or '').strip()
    target = f"{remote}:{remote_path}" if remote_path else f"{remote}:"
    try:
        result = subprocess.run([rclone_path, 'lsd', target], capture_output=True, text=True, timeout=20)
        if result.returncode != 0:
            return False, f"Could not reach {target}: {result.stderr.strip() or '(no error output)'}"
        return True, f"Connected to {target} successfully."
    except subprocess.TimeoutExpired:
        return False, f"Timed out reaching {target} after 20s."
    except OSError as e:
        return False, f"Could not run rclone: {e}"


def sync_to_cloud():
    """Mirrors the backup destination (Full Backup + every Snapshot --
    BASE_DIR/FULL_NAME, same root restore_operations.py's
    total_destination_size_bytes() measures) to the configured rclone
    remote via `rclone sync`. Called unconditionally right after every
    SUCCESSFUL local backup (main_backup.py's run_regular_backup(),
    rsync_event_handler.py's run_backup_script()) -- no-ops if the
    feature isn't enabled, so callers never need their own enabled
    check, matching how run_backup_script_with_hooks() already handles
    unset PRE_BACKUP_SCRIPT/POST_BACKUP_SCRIPT internally.

    Never raises -- any failure is logged + notify(level='critical') and
    returned as (False, message), same contract as run_hook_script(),
    since a failed cloud push must never be mistaken for the local
    backup (which already succeeded independently of this) having
    failed.

    `rclone sync` makes the remote match the source EXACTLY, including
    deleting remote files/folders no longer present locally -- same
    mirroring philosophy as the local backup's own `rsync --delete`.
    This is why CLOUD_SYNC_REMOTE_PATH should always be a dedicated
    subfolder, never bare remote root (see cloud_backup.html's help
    text) -- nothing here validates that at sync time, only at save
    time (see server.py's /cloud-backup/save).
    """
    if load_env_value('CLOUD_SYNC_ENABLED') != '1':
        return True, "Cloud sync disabled"

    remote = (load_env_value('CLOUD_SYNC_REMOTE') or '').strip()
    remote_path = (load_env_value('CLOUD_SYNC_REMOTE_PATH') or '').strip()
    bwlimit = (load_env_value('CLOUD_SYNC_BWLIMIT') or '').strip()

    def _fail(message):
        logger.error(message)
        notify("FolderW: Cloud sync failed", message, level='critical')
        set_database_value('CLOUD_SYNC_LAST_STATUS', 'failed')
        set_database_value('CLOUD_SYNC_LAST_TIME', str(time.time()))
        return False, message

    rclone_path = rclone_binary_path()
    if not rclone_path:
        return _fail("rclone not found -- cloud sync is enabled but rclone isn't installed.")
    if not remote:
        return _fail("Cloud sync is enabled but no remote is configured.")

    full_backup = load_other_variables('full_backup')
    destination_root = os.path.dirname(full_backup)
    target = f"{remote}:{remote_path}" if remote_path else f"{remote}:"

    os.makedirs(os.path.dirname(CLOUD_SYNC_LOG_FILE), exist_ok=True)
    cmd = [
        rclone_path, 'sync', destination_root, target,
        '--transfers', '4', '--checkers', '8',
        '--timeout', RCLONE_TIMEOUT, '--contimeout', RCLONE_CONTIMEOUT,
        '--log-file', CLOUD_SYNC_LOG_FILE, '--log-level', 'INFO',
    ]
    if bwlimit:
        cmd += ['--bwlimit', bwlimit]

    logger.info(f"Starting cloud sync: {destination_root} -> {target}")
    set_database_value('CLOUD_SYNC_RUNNING', '1')
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            return _fail(f"rclone sync exited {result.returncode} (see {CLOUD_SYNC_LOG_FILE}): {result.stderr.strip() or '(no error output)'}")
        message = f"Synced to {target} successfully."
        logger.info(message)
        set_database_value('CLOUD_SYNC_LAST_STATUS', 'success')
        set_database_value('CLOUD_SYNC_LAST_TIME', str(time.time()))
        return True, message
    except OSError as e:
        return _fail(f"Could not run rclone: {e}")
    finally:
        set_database_value('CLOUD_SYNC_RUNNING', '')
