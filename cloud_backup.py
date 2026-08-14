import json
import os
import shutil
import subprocess
import time
from db_operations import load_env_value, load_other_variables, set_database_value
from notifications import notify, _notify_desktop
from translations import t
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
        return False, t('cloud_test_not_found')
    remote = (remote or '').strip()
    if not remote:
        return False, t('cloud_test_no_remote')
    remote_path = (remote_path or '').strip()
    target = f"{remote}:{remote_path}" if remote_path else f"{remote}:"
    try:
        result = subprocess.run([rclone_path, 'lsd', target], capture_output=True, text=True, timeout=20)
        if result.returncode != 0:
            return False, t('cloud_test_could_not_reach', target=target, error=(result.stderr.strip() or t('hooks_no_error_output')))
        return True, t('cloud_test_connected', target=target)
    except subprocess.TimeoutExpired:
        return False, t('cloud_test_timeout', target=target)
    except OSError as e:
        return False, t('cloud_test_could_not_run', error=str(e))


def get_remote_type(remote):
    """The backend type (e.g. 'drive', 'dropbox') of an already-configured
    rclone remote, parsed from `rclone config show` -- used by the Cloud
    Backup page's Renew Token section to tell the user exactly which
    `rclone authorize "<type>"` command to run for the remote they have
    selected.
    """
    rclone_path = rclone_binary_path()
    if not rclone_path or not remote:
        return None
    try:
        result = subprocess.run([rclone_path, 'config', 'show', remote], capture_output=True, text=True, timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        key, _, value = line.partition('=')
        if key.strip() == 'type':
            return value.strip()
    return None


def renew_remote_token(remote, token_json):
    """(success, message) for the Cloud Backup page's Renew Token button.

    FolderW never runs the OAuth browser flow itself (see sync_to_cloud()'s
    docstring -- it never handles cloud credentials directly): the user
    runs `rclone authorize "<type>"` themselves on any device with a
    browser, then pastes the resulting JSON token blob here. This writes
    that blob into the named remote's config via `rclone config update`
    and immediately verifies it with a real connection test -- rclone
    itself writes ANY string into the token field with no validation
    (confirmed directly: `rclone config update <remote> token 'not-json'`
    exits 0 and silently corrupts the remote), so both the JSON shape
    check before writing and the connectivity check after are
    load-bearing, not just nice-to-haves.
    """
    rclone_path = rclone_binary_path()
    if not rclone_path:
        return False, t('cloud_test_not_found')

    remote = (remote or '').strip()
    if not remote:
        return False, t('cloud_renew_error_no_remote')

    _, remotes = list_rclone_remotes()
    if remote not in remotes:
        return False, t('cloud_renew_error_unknown_remote', remote=remote)

    token_json = (token_json or '').strip()
    try:
        parsed = json.loads(token_json)
    except (ValueError, TypeError):
        return False, t('cloud_renew_error_invalid_json')
    if not isinstance(parsed, dict) or 'access_token' not in parsed or 'token_type' not in parsed:
        return False, t('cloud_renew_error_missing_fields')

    try:
        result = subprocess.run(
            [rclone_path, 'config', 'update', remote, 'token', token_json, '--non-interactive'],
            capture_output=True, text=True, timeout=20,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, t('cloud_renew_error_could_not_run', error=str(e))
    if result.returncode != 0:
        return False, t('cloud_renew_error_rclone_failed', error=(result.stderr.strip() or result.stdout.strip() or t('hooks_no_error_output')))

    verified, verify_message = test_cloud_connection(remote, '')
    if verified:
        return True, t('cloud_renew_success', remote=remote)
    return False, t('cloud_renew_success_unverified', error=verify_message)


def _notify_synced_files(log_file, start_offset):
    """Fires one desktop notify-send per file the last sync_to_cloud()
    call actually copied -- explicitly requested (per-file, not a single
    summary), and desktop-only by design: routing this through notify()
    would also fan it out to every configured Apprise URL, which would
    turn a big sync into a phone-buzzing storm on Pushover/ntfy. Gated on
    NOTIFY_SEND_ALWAYS like every other desktop notification, so enabling
    cloud sync alone can't surprise someone who's opted out of desktop
    notifications.

    Reads only the bytes appended to `log_file` since `start_offset` --
    rclone's --log-file APPENDS across runs rather than truncating
    (confirmed directly), so reading the whole file every time would
    re-notify about every previous sync's files too.
    """
    if load_env_value('NOTIFY_SEND_ALWAYS') != '1':
        return
    try:
        with open(log_file, 'r') as f:
            f.seek(start_offset)
            new_content = f.read()
    except OSError:
        return
    for line in new_content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get('msg') in ('Copied (new)', 'Copied (replaced existing)'):
            _notify_desktop("FolderW: Cloud sync", f"Sent: {entry.get('object', '?')}", 'low')


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
    try:
        log_start_offset = os.path.getsize(CLOUD_SYNC_LOG_FILE)
    except OSError:
        log_start_offset = 0
    cmd = [
        rclone_path, 'sync', destination_root, target,
        '--transfers', '4', '--checkers', '8',
        '--timeout', RCLONE_TIMEOUT, '--contimeout', RCLONE_CONTIMEOUT,
        '--log-file', CLOUD_SYNC_LOG_FILE, '--log-level', 'INFO', '--use-json-log',
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
        _notify_synced_files(CLOUD_SYNC_LOG_FILE, log_start_offset)
        return True, message
    except OSError as e:
        return _fail(f"Could not run rclone: {e}")
    finally:
        set_database_value('CLOUD_SYNC_RUNNING', '')
