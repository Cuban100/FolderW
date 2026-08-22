import os
import subprocess
import threading
from db_operations import load_env_value
from notifications import notify
from translations import t
from loguru import logger

# Generous but bounded -- a hung "stop service"/"dump database" command
# would otherwise wedge every future backup forever (main_backup.py's
# scheduler and rsync_event_handler.py's debounced trigger both just
# call this and wait).
HOOK_TIMEOUT_SECONDS = 300


def backup_failure_message(returncode, stderr):
    """Human-readable reason for a failed backup subprocess -- shared by
    main_backup.py and rsync_event_handler.py's exception handlers so
    both give the same specific answer instead of each falling back to
    a bare "check the logs" (confirmed as a real gap: the previous
    generic message never said whether a failure was actually "ran out
    of disk space" versus anything else, and a user had no way to tell
    without going and reading the log file themselves).

    Only detects the one cause distinctive enough to catch reliably
    from stderr text (rsync's actual `No space left on device` message,
    the OS's own ENOSPC error) -- everything else still falls back to
    the exit-code message rather than guessing at rsync's dozens of
    other possible exit codes.
    """
    if stderr and 'No space left on device' in stderr:
        return "Backup failed: destination ran out of disk space during the transfer."
    return f"A backup run failed (exit code {returncode}). Check the FolderW logs for details."


def run_hook_script(path, label):
    """Run one pre/post-backup hook script. Returns (success, message)
    -- message is a short, human-readable outcome description, used for
    the notify()/logger calls here and reused verbatim by the Backup
    Hooks settings page's "Test Script" button, so a failure's exact
    reason is visible right there inline, not just in the logs or a
    desktop notification.
    """
    if not os.path.isfile(path):
        message = t('hooks_error_not_found', path=path)
        logger.error(f"{label} script not found: {path}")
        notify(f"FolderW: {label} script missing", f"Configured {label.lower()} script not found: {path}", level='critical')
        return False, message
    if not os.access(path, os.X_OK):
        message = t('hooks_error_not_executable', path=path)
        logger.error(f"{label} script is not executable: {path}")
        notify(f"FolderW: {label} script not executable", f"Configured {label.lower()} script isn't executable (chmod +x): {path}", level='critical')
        return False, message
    try:
        result = subprocess.run([path], timeout=HOOK_TIMEOUT_SECONDS, capture_output=True, text=True)
        if result.returncode != 0:
            message = t('hooks_error_exited', code=result.returncode, stderr=(result.stderr.strip() or t('hooks_no_error_output')))
            logger.error(f"{label} script ({path}) exited {result.returncode}: {result.stderr.strip()}")
            notify(f"FolderW: {label} script failed", f"{path} exited {result.returncode}. Check FolderW logs for details.", level='critical')
            return False, message
        message = t('hooks_success_with_output', output=result.stdout.strip()) if result.stdout.strip() else t('hooks_success')
        logger.info(f"{label} script completed successfully: {path}")
        return True, message
    except subprocess.TimeoutExpired:
        message = t('hooks_error_timeout', seconds=HOOK_TIMEOUT_SECONDS)
        logger.error(f"{label} script timed out after {HOOK_TIMEOUT_SECONDS}s: {path}")
        notify(f"FolderW: {label} script timed out", f"{path} did not finish within {HOOK_TIMEOUT_SECONDS}s.", level='critical')
        return False, message
    except OSError as e:
        message = t('hooks_error_could_not_run', error=str(e))
        logger.error(f"Error running {label.lower()} script {path}: {e}")
        notify(f"FolderW: {label} script error", f"Could not run {path}: {e}", level='critical')
        return False, message


def run_backup_script_with_hooks(*subprocess_args, concurrent_fn=None, **subprocess_kwargs):
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

    concurrent_fn: optional zero-arg callable run in a background thread
    alongside the backup subprocess (rclone's cloud sync, in practice --
    it reads SRC_DIR independently of rsync, so it doesn't need to wait
    for rsync to finish). Deliberately started AFTER the pre-script and
    joined BEFORE the post-script, not around them: both hooks can
    change SRC_DIR's contents (a pre-script dumping a database, a post-
    script cleaning that dump up or restarting a stopped service), so
    concurrent_fn only gets the window where SRC_DIR is actually in its
    finished, stable state -- the same window rsync itself gets. The
    thread is always joined in `finally`, even if the backup subprocess
    raises, so a slow cloud sync can never outlive this function the way
    a detached background process could (confirmed live earlier this
    session: an orphaned rclone process kept running for hours after its
    Python wrapper was gone, silently blocking every later sync attempt).
    When concurrent_fn is given, returns (subprocess_result,
    concurrent_fn_result); otherwise just subprocess_result, unchanged
    from before.

    *subprocess_args/**subprocess_kwargs are passed straight through to
    the actual subprocess.run() of the backup script -- each caller
    already builds a slightly different call (rsync_event_handler.py
    captures output, main_backup.py doesn't), so this preserves each
    one's existing behavior/return value exactly, hooks aside.
    """
    pre_script = load_env_value('PRE_BACKUP_SCRIPT')
    post_script = load_env_value('POST_BACKUP_SCRIPT')
    concurrent_result = [None]
    thread = None

    # The pre-script check/raise is INSIDE the try, not before it --
    # found live, the hard way: post-script is supposed to run
    # unconditionally (see docstring), but an earlier version raised
    # for a failed pre-script before entering the try/finally, so the
    # post-script's cleanup (e.g. restarting a service) never ran in
    # exactly the case it matters most -- something upstream already
    # went wrong.
    try:
        if pre_script:
            success, _ = run_hook_script(pre_script, 'Pre-backup')
            if not success:
                logger.error("Pre-backup script failed -- aborting this backup run.")
                raise subprocess.CalledProcessError(1, pre_script)
        if concurrent_fn is not None:
            def _run_concurrent():
                concurrent_result[0] = concurrent_fn()
            thread = threading.Thread(target=_run_concurrent, daemon=True)
            thread.start()
        result = subprocess.run(*subprocess_args, **subprocess_kwargs)
        return (result, concurrent_result[0]) if concurrent_fn is not None else result
    finally:
        if thread is not None:
            thread.join()
        if post_script:
            run_hook_script(post_script, 'Post-backup')
