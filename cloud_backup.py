import json
import os
import re
import select
import shutil
import subprocess
import threading
import time
import uuid
import psutil
from db_operations import load_env_value, load_other_variables, set_database_value, get_database_value, record_cloud_sync_run, list_cloud_sync_runs
from notifications import notify, _notify_desktop
from statistics_operations import human_readable_size
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

# How often rclone writes a progress snapshot (bytes/eta/speed/current
# files) to --log-file -- rclone's own default is 60s, too coarse for a
# dashboard progress bar that only gets checked this often anyway (see
# get_cloud_sync_progress(), polled from /dashboard-stats every 30s).
PROGRESS_STATS_INTERVAL = '15s'

# --transfers/--checkers: how many files rclone uploads/checks in
# parallel. Confirmed live this matters far more than bandwidth for this
# workload -- incremental snapshots mean thousands of small individual
# files (this session's real first sync averaged ~45 KB/file), and each
# one costs close to a full Google Drive API round-trip (~1.5s) more
# than actual transfer time. At the old --transfers 4, that caps
# throughput at ~2.7 files/sec REGARDLESS of upload bandwidth, which is
# exactly what made a fast connection look stuck at the same few percent
# for hours. Higher concurrency directly attacks that ceiling; rclone
# handles any resulting rate-limit backoff itself.
RCLONE_TRANSFERS = '16'
RCLONE_CHECKERS = '32'

# How long to wait for the user to finish logging in in the browser tab
# before giving up on a "Log In via Browser" job and killing the rclone
# process holding the local OAuth callback port open.
AUTHORIZE_JOB_TIMEOUT = 300

# Deliberately a short, curated subset of the "Add a New Remote" form's
# dropdown, not all 40+ backends rclone supports -- these are the OAuth
# ones that "Log In via Browser" can fully finish end to end right after
# creation. Backends needing other credentials (S3-style keys, SFTP/FTP
# passwords, WebDAV) need fields this minimal name+type form doesn't
# collect, so they're left to `rclone config` in a terminal as before.
KNOWN_REMOTE_TYPES = [
    ('drive', 'Google Drive'),
    ('dropbox', 'Dropbox'),
    ('onedrive', 'Microsoft OneDrive'),
    ('box', 'Box'),
    ('pcloud', 'pCloud'),
]

_authorize_jobs = {}
_authorize_jobs_lock = threading.Lock()


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


def _get_remote_config_field(remote, field):
    """One field (e.g. 'type', 'client_id') from `rclone config show
    <remote>` -- shared by get_remote_type() and the custom OAuth client
    credential lookup start_remote_authorize() uses to avoid rclone's
    shared default client. rclone does NOT obscure client_secret in this
    output (confirmed directly) -- it's an OAuth "installed app" secret,
    not a traditional one, so this is expected rclone behavior, not a
    leak on our part.
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
        if key.strip() == field:
            return value.strip()
    return None


def get_remote_type(remote):
    """The backend type (e.g. 'drive', 'dropbox') of an already-configured
    rclone remote -- used by the Cloud Backup page's Renew Token section
    to tell the user exactly which `rclone authorize "<type>"` command
    applies to the remote they have selected.
    """
    return _get_remote_config_field(remote, 'type')


def get_remote_client_id(remote):
    """The custom OAuth client_id configured on a remote, if any -- used
    by the Cloud Backup page to show whether a remote is still on
    rclone's shared default client (see start_remote_authorize()'s
    docstring for why that matters) or has its own.
    """
    return _get_remote_config_field(remote, 'client_id')


_REMOTE_NAME_RE = re.compile(r'[A-Za-z0-9_-]+')


def create_remote(name, backend_type, client_id=None, client_secret=None):
    """(success, message) for the Cloud Backup page's "Add a New Remote"
    form -- the other half of the zero-terminal setup story alongside
    start_remote_authorize(): this registers a bare remote entry (name +
    backend type, no token yet) via `rclone config create`, so "Log In
    via Browser" has something to authenticate right after. Never
    contacts the provider itself -- confirmed directly that `rclone
    config create <name> <type> --non-interactive` writes a minimal
    `[name]\\ntype = <type>` entry and returns even for backends that
    would normally ask more setup questions (e.g. onedrive), since
    --non-interactive just leaves those unanswered rather than blocking.

    client_id/client_secret are optional -- when given, they're stored
    on the remote too, so start_remote_authorize() picks them up
    automatically and runs the OAuth flow against the user's OWN
    registered app instead of rclone's shared default one (see that
    function's docstring for why that matters: the shared client's
    quota is pooled across every rclone install worldwide).
    """
    rclone_path = rclone_binary_path()
    if not rclone_path:
        return False, t('cloud_test_not_found')

    name = (name or '').strip()
    if not name:
        return False, t('cloud_create_error_no_name')
    if not _REMOTE_NAME_RE.fullmatch(name):
        return False, t('cloud_create_error_invalid_name')

    valid_types = {key for key, _ in KNOWN_REMOTE_TYPES}
    if backend_type not in valid_types:
        return False, t('cloud_create_error_invalid_type')

    _, remotes = list_rclone_remotes()
    if name in remotes:
        return False, t('cloud_create_error_exists', name=name)

    client_id = (client_id or '').strip()
    client_secret = (client_secret or '').strip()
    if bool(client_id) != bool(client_secret):
        return False, t('cloud_create_error_partial_credentials')

    cmd = [rclone_path, 'config', 'create', name, backend_type]
    if client_id:
        cmd += [f'client_id={client_id}', f'client_secret={client_secret}']
    cmd += ['--non-interactive']

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, t('cloud_create_error_could_not_run', error=str(e))
    if result.returncode != 0:
        return False, t('cloud_create_error_rclone_failed', error=(result.stderr.strip() or result.stdout.strip() or t('hooks_no_error_output')))

    return True, t('cloud_create_success', name=name)


def update_remote_credentials(remote, client_id, client_secret):
    """(success, message) for the Cloud Backup page's Renew Token
    section -- attaches a custom OAuth client_id/secret to an ALREADY-
    EXISTING remote (e.g. one connected before this feature existed,
    still on rclone's shared default client). Doesn't touch the token --
    the existing connection keeps working until the next "Log In via
    Browser", which will then pick up these credentials automatically
    (see start_remote_authorize()).
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

    client_id = (client_id or '').strip()
    client_secret = (client_secret or '').strip()
    if not client_id or not client_secret:
        return False, t('cloud_create_error_partial_credentials')

    try:
        result = subprocess.run(
            [rclone_path, 'config', 'update', remote, 'client_id', client_id, 'client_secret', client_secret, '--non-interactive'],
            capture_output=True, text=True, timeout=20,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, t('cloud_create_error_could_not_run', error=str(e))
    if result.returncode != 0:
        return False, t('cloud_create_error_rclone_failed', error=(result.stderr.strip() or result.stdout.strip() or t('hooks_no_error_output')))

    return True, t('cloud_credentials_updated', remote=remote)


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

    # A single verification attempt run immediately after writing a
    # freshly-exchanged OAuth token can hit a transient timeout that a
    # retry moments later doesn't -- confirmed directly: a "timed out"
    # renewal result, followed right away by clicking the page's own
    # Test Cloud Connection button, succeeded against the very same
    # token that had just supposedly failed. Two attempts with a short
    # pause avoids reporting a scary false-negative for what's actually
    # a working token, without masking a genuinely broken one (which
    # will still fail both attempts).
    verify_message = ''
    for attempt in range(2):
        verified, verify_message = test_cloud_connection(remote, '')
        if verified:
            return True, t('cloud_renew_success', remote=remote)
        if attempt == 0:
            time.sleep(3)
    return False, t('cloud_renew_success_unverified', error=verify_message)


_AUTHORIZE_LINK_RE = re.compile(r'Please go to the following link:\s*(\S+)')


def start_remote_authorize(remote):
    """(job_id, auth_url, error_message) for the Cloud Backup page's "Log
    In via Browser" button -- the fully web-UI version of renew_remote_
    token(), no terminal needed. Only job_id/auth_url or error_message is
    ever non-None.

    Runs `rclone authorize "<type>"` as a background subprocess. That
    command starts a local webserver (127.0.0.1:53682 by default) and
    prints a link that itself points at that local server -- rclone's
    own redirector, which then bounces the browser to Google/Dropbox/etc.
    and catches the callback. This means the link this function returns
    ONLY works from a browser running on THIS machine (confirmed
    directly: even the very first link rclone prints is a 127.0.0.1 one,
    not just the final OAuth callback) -- on a different device on the
    LAN, opening it hits that device's own loopback with nothing
    listening. cloud_backup.html surfaces this; the manual paste-a-token
    flow in renew_remote_token() remains the only option for remote
    access, this is a same-machine convenience on top of it.

    Only one job runs at a time -- rclone's local OAuth webserver binds a
    fixed port, so a second concurrent job would just fail to bind it.
    """
    rclone_path = rclone_binary_path()
    if not rclone_path:
        return None, None, t('cloud_test_not_found')

    remote = (remote or '').strip()
    if not remote:
        return None, None, t('cloud_renew_error_no_remote')

    _, remotes = list_rclone_remotes()
    if remote not in remotes:
        return None, None, t('cloud_renew_error_unknown_remote', remote=remote)

    backend_type = get_remote_type(remote)
    if not backend_type:
        return None, None, t('cloud_authorize_error_unknown_type')

    with _authorize_jobs_lock:
        if any(j['status'] == 'waiting' for j in _authorize_jobs.values()):
            return None, None, t('cloud_authorize_error_already_running')

    authorize_cmd = [rclone_path, 'authorize', backend_type, '--auth-no-open-browser']
    # `rclone authorize <type>` alone always runs against rclone's own
    # shared default OAuth client for that backend -- it has no idea
    # which named remote this is for, so it can't pick up a custom
    # client_id/secret on its own even if one is stored on the remote.
    # Confirmed live this matters: that shared client's request quota is
    # pooled across every rclone install worldwide, and a sync with
    # enough files (this session's real one) can exhaust it on its own
    # well before bandwidth becomes the limit. Passing these backend-
    # specific flags (confirmed they exist: --drive-client-id, --
    # dropbox-client-id, etc., one per KNOWN_REMOTE_TYPES entry) is what
    # actually routes the OAuth flow through the user's own app instead.
    client_id = get_remote_client_id(remote)
    client_secret = _get_remote_config_field(remote, 'client_secret')
    if client_id and client_secret:
        authorize_cmd += [f'--{backend_type}-client-id={client_id}', f'--{backend_type}-client-secret={client_secret}']

    try:
        process = subprocess.Popen(
            authorize_cmd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
    except OSError as e:
        return None, None, t('cloud_authorize_error_process', error=str(e))

    # rclone prints the link within the first couple lines, before it
    # blocks waiting for the OAuth callback -- read line-by-line (not
    # process.communicate(), which would block until the whole process
    # exits, i.e. until the login is done) until that line shows up or
    # the process dies without ever printing one. select() (not a bare
    # readline() loop) is what makes the 10s deadline real: readline()
    # blocks with no timeout of its own, so a deadline only re-checked
    # between readline() calls would never fire against a process that's
    # alive but silent -- select() bounds each wait explicitly instead.
    lines = []
    auth_url = None
    deadline = time.time() + 10
    while time.time() < deadline:
        ready, _, _ = select.select([process.stdout], [], [], max(0.0, deadline - time.time()))
        if not ready:
            break
        line = process.stdout.readline()
        if not line:
            if process.poll() is not None:
                break
            continue
        lines.append(line)
        match = _AUTHORIZE_LINK_RE.search(line)
        if match:
            auth_url = match.group(1)
            break

    if not auth_url:
        process.kill()
        process.wait()
        return None, None, t('cloud_authorize_error_no_url')

    job_id = uuid.uuid4().hex
    with _authorize_jobs_lock:
        # `process` is kept here (not just its pid) specifically so
        # cancel_authorize_job() can kill it directly -- rclone's local
        # OAuth listener has no way to know the browser tab was closed,
        # so without an explicit cancel it just sits there blocking
        # until AUTHORIZE_JOB_TIMEOUT, holding the "already running"
        # lock the whole time. Confirmed live this is a real gap, not a
        # theoretical one.
        _authorize_jobs[job_id] = {'status': 'waiting', 'message': '', 'remote': remote, 'process': process}

    thread = threading.Thread(target=_finish_authorize_job, args=(job_id, process, remote, lines), daemon=True)
    thread.start()
    return job_id, auth_url, None


def cancel_authorize_job(job_id):
    """(success, message) for the Cloud Backup page's Cancel button --
    kills the rclone process behind a "waiting" Log In via Browser job
    immediately, instead of leaving it to block the OAuth port (and the
    already-running lock) until AUTHORIZE_JOB_TIMEOUT. _finish_
    authorize_job()'s own communicate() call unblocks as soon as the
    process dies and takes care of moving the job to a terminal status
    from there -- this only needs to kill the process.
    """
    with _authorize_jobs_lock:
        job = _authorize_jobs.get(job_id)
        if not job or job['status'] != 'waiting':
            return False, t('cloud_authorize_error_no_token')
        process = job.get('process')
    if process:
        try:
            process.kill()
        except OSError:
            pass
    return True, t('cloud_authorize_cancelled')


def _finish_authorize_job(job_id, process, remote, lines):
    try:
        # communicate() (not another readline() loop) is what actually
        # makes the timeout real: readline() blocks forever on a process
        # that's still alive but has nothing more to print (exactly the
        # "user opened the tab, then walked away" case), so a deadline
        # only checked between readline() calls never fires. communicate()
        # enforces the timeout itself and raises instead of hanging.
        try:
            remaining_output, _ = process.communicate(timeout=AUTHORIZE_JOB_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            with _authorize_jobs_lock:
                _authorize_jobs[job_id] = {'status': 'error', 'message': t('cloud_authorize_timeout'), 'remote': remote}
            return
        if remaining_output:
            lines.extend(remaining_output.splitlines())

        token_json = None
        for line in lines:
            line = line.strip()
            if not line.startswith('{'):
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict) and 'access_token' in parsed:
                token_json = line
                break

        if not token_json:
            with _authorize_jobs_lock:
                _authorize_jobs[job_id] = {'status': 'error', 'message': t('cloud_authorize_error_no_token'), 'remote': remote}
            return

        success, message = renew_remote_token(remote, token_json)
        with _authorize_jobs_lock:
            _authorize_jobs[job_id] = {'status': 'success' if success else 'error', 'message': message, 'remote': remote}
    except Exception as e:
        logger.error(f"Browser authorize job {job_id} for {remote} crashed: {e}")
        with _authorize_jobs_lock:
            _authorize_jobs[job_id] = {'status': 'error', 'message': t('cloud_authorize_error_process', error=str(e)), 'remote': remote}


def get_authorize_job_status(job_id):
    with _authorize_jobs_lock:
        job = _authorize_jobs.get(job_id)
    if not job:
        return {'status': 'error', 'message': t('cloud_authorize_error_no_token')}
    return {'status': job['status'], 'message': job['message']}


def _parse_sync_log(log_file, start_offset):
    """(copied_files, stats) parsed from the slice of `log_file` appended
    since `start_offset` -- this run's own output only. rclone's
    --log-file APPENDS across runs rather than truncating (confirmed
    directly), so reading the whole file every time would pick up every
    previous run's files and stats too, not just this one's.

    `stats` is rclone's own periodic transfer-summary object (bytes,
    transfers, elapsedTime, speed, ...) -- keeps the LAST one seen in
    this slice, which is the final tally for the run, since rclone logs
    it more than once as the sync progresses. None if the run produced
    no stats line at all (e.g. it failed before rclone got that far).

    Shared by _notify_synced_files() and sync_to_cloud()'s history
    recording so the log is only read and parsed once per run.
    """
    try:
        with open(log_file, 'r') as f:
            f.seek(start_offset)
            new_content = f.read()
    except OSError:
        return [], None
    copied = []
    stats = None
    for line in new_content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get('msg') in ('Copied (new)', 'Copied (replaced existing)'):
            copied.append(entry.get('object', '?'))
        if isinstance(entry.get('stats'), dict):
            stats = entry['stats']
    return copied, stats


def _notify_synced_files(copied_files):
    """Fires one desktop notify-send per file sync_to_cloud() actually
    copied -- explicitly requested (per-file, not a single summary), and
    desktop-only by design: routing this through notify() would also fan
    it out to every configured Apprise URL, which would turn a big sync
    into a phone-buzzing storm on Pushover/ntfy. Gated on
    NOTIFY_SEND_ALWAYS like every other desktop notification, so enabling
    cloud sync alone can't surprise someone who's opted out of desktop
    notifications.
    """
    if load_env_value('NOTIFY_SEND_ALWAYS') != '1':
        return
    for name in copied_files:
        _notify_desktop("FolderW: Cloud sync", f"Sent: {name}", 'low')


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

    if is_cloud_sync_running():
        # Confirmed live: nothing previously stopped two calls from
        # racing -- a manually-triggered sync was still running when a
        # second, watchdog-triggered backup completed and called this
        # again, launching a SECOND rclone process against the exact
        # same source/remote at the same time. Both then spent 10+ hours
        # fighting each other (duplicate checks, contended file state,
        # roughly double the API request volume triggering harsher
        # Google Drive rate-limiting) instead of one properly finishing.
        message = "Cloud sync already in progress -- skipping this trigger rather than racing a second rclone process against the same destination."
        logger.warning(message)
        return True, message

    remote = (load_env_value('CLOUD_SYNC_REMOTE') or '').strip()
    remote_path = (load_env_value('CLOUD_SYNC_REMOTE_PATH') or '').strip()
    bwlimit = (load_env_value('CLOUD_SYNC_BWLIMIT') or '').strip()

    try:
        log_start_offset = os.path.getsize(CLOUD_SYNC_LOG_FILE)
    except OSError:
        log_start_offset = 0

    def _fail(message):
        logger.error(message)
        notify("FolderW: Cloud sync failed", message, level='critical')
        set_database_value('CLOUD_SYNC_LAST_STATUS', 'failed')
        set_database_value('CLOUD_SYNC_LAST_TIME', str(time.time()))
        _, stats = _parse_sync_log(CLOUD_SYNC_LOG_FILE, log_start_offset)
        stats = stats or {}
        record_cloud_sync_run(
            remote or '(none)', 'failed',
            files_transferred=stats.get('transfers', 0),
            bytes_transferred=stats.get('bytes', 0),
            duration_seconds=stats.get('elapsedTime', 0.0),
            avg_speed_bps=stats.get('speed', 0.0),
            error_message=message,
        )
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
        '--transfers', RCLONE_TRANSFERS, '--checkers', RCLONE_CHECKERS,
        '--timeout', RCLONE_TIMEOUT, '--contimeout', RCLONE_CONTIMEOUT,
        '--log-file', CLOUD_SYNC_LOG_FILE, '--log-level', 'INFO', '--use-json-log',
        '--stats', PROGRESS_STATS_INTERVAL,
        # Confirmed live: the real bottleneck wasn't upload bandwidth or
        # even --transfers concurrency -- it was rclone walking the
        # destination's deep Snapshot/Day/Time tree with one API call
        # per directory just to compare against source, which alone was
        # enough to hit Google Drive's "Queries per minute" quota
        # (actual 403 errors seen in the log) on a tree this size.
        # --fast-list replaces that with a handful of bulk paginated
        # listings instead of one request per directory -- far fewer
        # transactions, at the cost of holding the full listing in
        # memory (trivial for a tree this size).
        '--fast-list',
    ]
    if bwlimit:
        cmd += ['--bwlimit', bwlimit]

    logger.info(f"Starting cloud sync: {destination_root} -> {target}")
    # So get_cloud_sync_progress() (running in the OTHER process -- the
    # web dashboard, not this backup worker) knows where THIS run's own
    # log output starts, the same reason log_start_offset exists at all:
    # --log-file appends across runs rather than truncating.
    set_database_value('CLOUD_SYNC_CURRENT_LOG_OFFSET', str(log_start_offset))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            return _fail(f"rclone sync exited {result.returncode} (see {CLOUD_SYNC_LOG_FILE}): {result.stderr.strip() or '(no error output)'}")
        message = f"Synced to {target} successfully."
        logger.info(message)
        set_database_value('CLOUD_SYNC_LAST_STATUS', 'success')
        set_database_value('CLOUD_SYNC_LAST_TIME', str(time.time()))
        copied_files, stats = _parse_sync_log(CLOUD_SYNC_LOG_FILE, log_start_offset)
        stats = stats or {}
        record_cloud_sync_run(
            remote, 'success',
            files_transferred=stats.get('transfers', len(copied_files)),
            bytes_transferred=stats.get('bytes', 0),
            duration_seconds=stats.get('elapsedTime', 0.0),
            avg_speed_bps=stats.get('speed', 0.0),
        )
        _notify_synced_files(copied_files)
        return True, message
    except OSError as e:
        return _fail(f"Could not run rclone: {e}")


def is_cloud_sync_running():
    """Whether an `rclone sync` launched by sync_to_cloud() is actually
    alive right now, for the dashboard's live indicator -- checked
    against the real process list (psutil), the same way server.py's
    is_backup_running() cross-checks rsync_incremental.py, rather than a
    DB flag set at start and cleared at the end.

    That flag approach was tried first here (CLOUD_SYNC_RUNNING) and
    removed after confirming live exactly the failure mode is_backup_
    running()'s docstring already warned about: this session's first
    real cloud sync got killed mid-transfer by an unrelated `systemctl
    restart` of the backup service, and the flag was left stuck at '1'
    with nothing actually running -- it would have shown "syncing"
    forever. Checking the real process instead is self-healing: a dead
    process is just absent from the list, no cleanup needed.
    """
    for proc in psutil.process_iter(['cmdline']):
        try:
            cmdline = proc.info['cmdline'] or []
            if any('rclone' in part for part in cmdline) and 'sync' in cmdline:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return False


def get_cloud_sync_progress():
    """Live progress for the dashboard's Cloud Sync indicator while
    is_cloud_sync_running() is True -- percent complete, ETA, current
    speed, and which file(s) are transferring right now. Parsed from
    rclone's own periodic stats snapshot (--stats, see PROGRESS_STATS_
    INTERVAL) in this run's slice of the log -- the same "confirmed the
    sync wasn't actually stuck, just silent" data already used to debug
    this live, now surfaced instead of needing a manual log check.

    Only reads the tail of the file, not the whole slice from
    CLOUD_SYNC_CURRENT_LOG_OFFSET (which could be many MB into a long
    first sync) -- the periodic stats line recurs often enough that the
    last 64KB comfortably contains one.
    """
    if not is_cloud_sync_running():
        return {"cloud_sync_progress_running": False}

    try:
        start_offset = int(get_database_value('CLOUD_SYNC_CURRENT_LOG_OFFSET', 'settings') or 0)
    except (TypeError, ValueError):
        start_offset = 0

    try:
        file_size = os.path.getsize(CLOUD_SYNC_LOG_FILE)
        read_from = max(start_offset, file_size - 65536)
        with open(CLOUD_SYNC_LOG_FILE, 'r') as f:
            f.seek(read_from)
            tail = f.read()
    except OSError:
        return {"cloud_sync_progress_running": True, "cloud_sync_progress_percent": None}

    stats = None
    for line in tail.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry.get('stats'), dict):
            stats = entry['stats']

    if not stats:
        return {"cloud_sync_progress_running": True, "cloud_sync_progress_percent": None}

    bytes_done = stats.get('bytes', 0)
    total_bytes = stats.get('totalBytes', 0)
    eta_seconds = stats.get('eta')
    current_files = [os.path.basename(f['name']) for f in (stats.get('transferring') or []) if f.get('name')]

    return {
        "cloud_sync_progress_running": True,
        "cloud_sync_progress_percent": round(100 * bytes_done / total_bytes, 1) if total_bytes else None,
        "cloud_sync_progress_files_display": f"{stats.get('transfers', 0)} / {stats.get('totalTransfers', 0)}",
        "cloud_sync_progress_bytes_display": f"{human_readable_size(bytes_done)} / {human_readable_size(total_bytes)}" if total_bytes else human_readable_size(bytes_done),
        "cloud_sync_progress_eta_display": _format_duration_seconds(eta_seconds) if eta_seconds else None,
        "cloud_sync_progress_speed_display": f"{human_readable_size(stats.get('speed', 0))}/s",
        "cloud_sync_progress_current_files": current_files[:3],
    }


def _format_duration_seconds(seconds):
    """'2h 44m 34s' style string for a completed sync's duration -- a
    dedicated formatter rather than reusing server.py's
    _format_delay_seconds() (that one's scoped to the Watchdog Delay
    config value, which is never more than a few minutes; a real sync
    against a slow upload link routinely runs for hours, confirmed
    directly in this install's own cloud_sync.log).
    """
    try:
        seconds = int(seconds or 0)
    except (TypeError, ValueError):
        seconds = 0
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return t('cloud_duration_hms', h=hours, m=minutes, s=secs)
    if minutes:
        return t('cloud_duration_ms', m=minutes, s=secs)
    return t('cloud_duration_s', s=secs)


def get_cloud_sync_dashboard_stats():
    """Everything the dashboard's dedicated Cloud Sync section needs --
    last-run summary (files/data/duration/speed) plus a short recent-
    history list -- pulled from cloud_sync_runs (see db_operations.
    record_cloud_sync_run()), which actually distinguishes "the last
    attempt succeeded with real numbers" from "nothing has run since the
    last failure" the way the older CLOUD_SYNC_LAST_STATUS/_TIME
    settings values alone can't.
    """
    runs = list_cloud_sync_runs(limit=10)
    bwlimit_display = (load_env_value('CLOUD_SYNC_BWLIMIT') or '').strip() or None
    if not runs:
        return {
            "cloud_sync_last_files": None,
            "cloud_sync_last_bytes_display": None,
            "cloud_sync_last_duration_display": None,
            "cloud_sync_last_speed_display": None,
            "cloud_sync_bwlimit_display": bwlimit_display,
            "cloud_sync_history": [],
        }
    last = runs[0]
    history = [
        {
            "timestamp": run["timestamp"],
            "status": run["status"],
            "files_transferred": run["files_transferred"],
            "bytes_display": human_readable_size(run["bytes_transferred"] or 0),
            "duration_display": _format_duration_seconds(run["duration_seconds"]),
            "error_message": run["error_message"],
        }
        for run in runs
    ]
    return {
        "cloud_sync_last_files": last["files_transferred"],
        "cloud_sync_last_bytes_display": human_readable_size(last["bytes_transferred"] or 0),
        "cloud_sync_last_duration_display": _format_duration_seconds(last["duration_seconds"]),
        "cloud_sync_last_speed_display": f"{human_readable_size(last['avg_speed_bps'] or 0)}/s",
        "cloud_sync_bwlimit_display": bwlimit_display,
        "cloud_sync_history": history,
    }
