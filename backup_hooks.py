import os
import subprocess
from db_operations import load_env_value
from notifications import notify
from loguru import logger

# Generous but bounded -- a hung "stop service"/"dump database" command
# would otherwise wedge every future backup forever (main_backup.py's
# scheduler and rsync_event_handler.py's debounced trigger both just
# call this and wait).
HOOK_TIMEOUT_SECONDS = 300


def _run_hook_script(path, label):
    """Run one pre/post-backup hook script. Returns True on success,
    False on any failure (missing file, not executable, non-zero exit,
    timeout) -- every failure mode is logged and notified at 'critical'
    so a broken hook is never silently invisible.
    """
    if not os.path.isfile(path):
        logger.error(f"{label} script not found: {path}")
        notify(f"FolderW: {label} script missing", f"Configured {label.lower()} script not found: {path}", level='critical')
        return False
    if not os.access(path, os.X_OK):
        logger.error(f"{label} script is not executable: {path}")
        notify(f"FolderW: {label} script not executable", f"Configured {label.lower()} script isn't executable (chmod +x): {path}", level='critical')
        return False
    try:
        result = subprocess.run([path], timeout=HOOK_TIMEOUT_SECONDS, capture_output=True, text=True)
        if result.returncode != 0:
            logger.error(f"{label} script ({path}) exited {result.returncode}: {result.stderr.strip()}")
            notify(f"FolderW: {label} script failed", f"{path} exited {result.returncode}. Check FolderW logs for details.", level='critical')
            return False
        logger.info(f"{label} script completed successfully: {path}")
        return True
    except subprocess.TimeoutExpired:
        logger.error(f"{label} script timed out after {HOOK_TIMEOUT_SECONDS}s: {path}")
        notify(f"FolderW: {label} script timed out", f"{path} did not finish within {HOOK_TIMEOUT_SECONDS}s.", level='critical')
        return False
    except OSError as e:
        logger.error(f"Error running {label.lower()} script {path}: {e}")
        notify(f"FolderW: {label} script error", f"Could not run {path}: {e}", level='critical')
        return False


def run_backup_script_with_hooks(*subprocess_args, **subprocess_kwargs):
    """Wraps one backup-script invocation (rsync_incremental.py or
    rsync_differential.py) with the user's optional PRE_BACKUP_SCRIPT /
    POST_BACKUP_SCRIPT settings. Shared by main_backup.py (scheduled and
    the initial backup) and rsync_event_handler.py (watchdog-triggered
    runs) -- the two independent places that actually launch a backup
    script -- so hooks fire the same way regardless of what triggered
    this particular run.

    A failing pre-script aborts the backup entirely (rsync never runs):
    if the hook's job is producing a consistent snapshot (a database
    dump, say), a failed pre-step means the backup would capture
    inconsistent state -- worse than not running at all. A post-script
    always runs, even if the backup itself failed, since its job is
    often undoing the pre-script's (restarting a service that was
    stopped for the backup) -- a failed backup shouldn't leave that
    service down indefinitely just because the backup didn't succeed.

    *subprocess_args/**subprocess_kwargs are passed straight through to
    the actual subprocess.run() of the backup script -- each caller
    already builds a slightly different call (rsync_event_handler.py
    captures output, main_backup.py doesn't), so this preserves each
    one's existing behavior/return value exactly, hooks aside.
    """
    pre_script = load_env_value('PRE_BACKUP_SCRIPT')
    post_script = load_env_value('POST_BACKUP_SCRIPT')

    # The pre-script check/raise is INSIDE the try, not before it --
    # found live, the hard way: post-script is supposed to run
    # unconditionally (see docstring), but an earlier version raised
    # for a failed pre-script before entering the try/finally, so the
    # post-script's cleanup (e.g. restarting a service) never ran in
    # exactly the case it matters most -- something upstream already
    # went wrong.
    try:
        if pre_script and not _run_hook_script(pre_script, 'Pre-backup'):
            logger.error("Pre-backup script failed -- aborting this backup run.")
            raise subprocess.CalledProcessError(1, pre_script)
        return subprocess.run(*subprocess_args, **subprocess_kwargs)
    finally:
        if post_script:
            _run_hook_script(post_script, 'Post-backup')
