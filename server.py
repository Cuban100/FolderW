from fastapi import FastAPI, Request, WebSocket, Form
from pydantic import BaseModel
import subprocess
import asyncio
import json
import os
import sys
import time
import threading
import webbrowser
import psutil
import apprise
from datetime import datetime
from notifications import _notify_desktop
from backup_hooks import run_hook_script
from statistics_operations import check_env_variables, validate_all_conditions, evaluation_of_resources, destination_space
from db_operations import load_env_value, load_other_variables, save_env_values, create_all_tables, get_last_session_number, list_items_by_session, get_database_value, set_database_value, reset_backup_history, has_completed_backup, count_backup_runs, list_backup_runs, wipe_database_for_fresh_start, get_backup_stats_series, get_backup_stats_summary, get_changes_by_session, record_restore_run, count_restore_runs, list_restore_runs
from restore_operations import list_backups, get_backup_path, list_files_in_backup, restore_backup, set_snapshot_note, COMPLETION_MARKER, compile_latest_snapshot, search_all_backups, restore_single_path, summarize_folder
from auth import hash_password, verify_password, get_or_create_secret_key
from loguru import logger
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.concurrency import run_in_threadpool
import uvicorn

# Initialize FastAPI app
app = FastAPI()

def _json_for_script(data):
    """json.dumps(), safe to embed inside <script type="application/json">.
    Plain json.dumps() doesn't escape '</' -- fine for data built only
    from fixed enums/numbers, but the file tree/restore listing embeds
    real file names and paths from SRC_DIR, which are genuinely
    arbitrary strings. A file literally named to contain "</script>"
    would otherwise close the tag early and let its own "content" be
    parsed as HTML. '\\/' is valid JSON and decodes back to '/' via
    JSON.parse(), so this changes nothing about the decoded value.
    """
    return json.dumps(data).replace('</', '<\\/')

# Resolve relative to this file, not the process's working directory, so
# templates/static still load correctly no matter where server.py is launched from
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Ensures every table (including ones added by an update after this
# install's database was first created) actually exists on every
# startup -- CREATE TABLE IF NOT EXISTS is always safe/idempotent, so
# this can't disturb existing data. Without this, a newly added table
# only ever reached an existing install the next time Settings happened
# to be re-saved (the only other call site) -- not guaranteed to ever
# happen again after initial setup. Found live: restore_runs existed in
# code but not in this install's actual database until this was added.
_database = load_env_value('DATABASE')
if _database:
    create_all_tables(_database)

# Cache-busting query param for /static/index.css and /static/sidebar.js
# -- both are shared across every page and have been edited repeatedly
# this session, and the <link>/<script> tags referencing them have no
# versioning at all, so a browser that already cached the old file
# keeps serving it indefinitely (no revalidation trigger) even after a
# deploy. Confirmed live: the same sidebar controls rendered fully
# styled on one page and completely unstyled on another, because
# different tabs/pages had cached different versions of index.css.
# max() of both files' mtimes so it bumps whenever either one changes,
# computed once at process start (an actual code change always
# restarts this process anyway -- see deploy workflow/update.sh).
STATIC_VERSION = int(max(
    os.path.getmtime(os.path.join(BASE_DIR, "static", "index.css")),
    os.path.getmtime(os.path.join(BASE_DIR, "static", "sidebar.js")),
))


def _sidebar_context(request):
    """Starlette context processor -- runs for EVERY TemplateResponse
    across the whole app (see context_processors= below), not just
    index.html's own routes. templates/_sidebar.html is included on
    every page, but its Stop Monitoring / Backup Service controls only
    ever rendered where a route happened to pass watchdog_active/
    backup_service_unit_exists/backup_service_active explicitly --
    which was only ever index.html's own render points. Confirmed live:
    the controls simply vanished on every other page (Settings, Restore,
    etc.), not because _sidebar.html itself changed, but because those
    routes never provided the variables its {% if %} guards check for.
    A context processor is the one place this can be fixed globally
    instead of touching every individual route's context dict.

    Deliberately minimal: only the 3 values _sidebar.html actually
    needs, not the rest of _dashboard_settings_context()'s heavier
    stats (disk scans, etc.) -- those stay index.html-specific, no
    reason to pay for them on, say, the Settings page.
    """
    return {
        "watchdog_active": is_watchdog_active(),
        "backup_service_unit_exists": _backup_service_unit_exists(),
        "backup_service_active": _backup_service_is_active(),
        "static_version": STATIC_VERSION,
    }


# Initialize Jinja2 templates
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
logo = '/static/logo.png'
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"), context_processors=[_sidebar_context])

SESSION_MAX_AGE = 15 * 24 * 60 * 60  # 15 days
PUBLIC_PATHS = {"/login"}

@app.middleware("http")
async def require_login(request: Request, call_next):
    # Login is opt-in: with no password configured (fresh/existing installs
    # that haven't set one via Settings), the dashboard stays open exactly
    # like before this feature existed.
    admin_hash = load_env_value('ADMIN_PASSWORD_HASH')
    path = request.url.path
    if admin_hash and path not in PUBLIC_PATHS and not path.startswith("/static/"):
        if not request.session.get('authenticated'):
            return RedirectResponse(url=f"/login?next={path}")
    return await call_next(request)

# Deliberately added *after* the middleware above: Starlette treats the
# most-recently-added middleware as outermost, so SessionMiddleware ends up
# wrapping require_login and populates request.session before it's read.
app.add_middleware(
    SessionMiddleware,
    secret_key=get_or_create_secret_key(),
    session_cookie="folderw_session",
    max_age=SESSION_MAX_AGE,
)

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    return templates.TemplateResponse("login.html", {"request": request, "next": next, "logo": logo})

@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, password: str = Form(...), next: str = Form("/")):
    admin_hash = load_env_value('ADMIN_PASSWORD_HASH')
    if admin_hash and verify_password(password, admin_hash):
        request.session['authenticated'] = True
        return RedirectResponse(url=next or "/", status_code=303)
    logger.warning("Failed login attempt.")
    return templates.TemplateResponse("login.html", {
        "request": request,
        "next": next,
        "logo": logo,
        "error": "Incorrect password.",
    })

@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login")

# Define the form data model
class FormData(BaseModel):
    server_port: int
    src_dir: str
    base_dir: str
    database: str
    monitor: bool
    backup_interval: str

status = {
    "backup_running": False,
    "message": "",
    "src_size": 0,
    "dest_space": 0,
    "can_backup": False
}

# Persisted check results, so the dashboard remembers Settings/Validation/
# Evaluation already passed instead of showing everything unchecked again
# on every page load. Cleared whenever settings actually change.
CHECK_KEYS = {
    "settings_sent": "SETTINGS_CHECK_PASSED",
    "validation_status": "VALIDATION_CHECK_PASSED",
    "evaluation_status": "EVALUATION_CHECK_PASSED",
}

def persist_check_results(**results):
    for key, value in results.items():
        if key in CHECK_KEYS:
            set_database_value(CHECK_KEYS[key], '1' if value else '0')
        else:
            set_database_value(key, value)

def load_persisted_checks():
    checked = {key: get_database_value(db_key, 'settings') == '1' for key, db_key in CHECK_KEYS.items()}
    src_size = get_database_value('LAST_SRC_SIZE', 'settings')
    dest_space = get_database_value('LAST_DEST_SPACE', 'settings')
    return {
        **checked,
        "src_size": src_size,
        "dest_space": dest_space,
        "can_backup": 'Yes' if checked["evaluation_status"] else ('No' if src_size else None),
    }

def clear_persisted_checks():
    for db_key in CHECK_KEYS.values():
        set_database_value(db_key, '0')
    # Also clear the last computed src/dest sizes — otherwise these stick
    # around from whatever the previous check computed (potentially a much
    # earlier setup reusing the same database file) even though the pass/
    # fail flags above correctly reset, showing stale numbers on the
    # dashboard until a fresh check happens to be run.
    set_database_value('LAST_SRC_SIZE', '')
    set_database_value('LAST_DEST_SPACE', '')

# Mirrors rsync_event_handler.py's own MONITORING_DELAY_MIN/MAX (duplicated
# rather than imported -- that module is a standalone runner script with
# its own module-level side effects on import, same reasoning as
# restore_operations.py's separately-duplicated COMPLETION_MARKER).
MONITORING_DELAY_MIN = 30
MONITORING_DELAY_MAX = 1800
MONITORING_DELAY_DEFAULT = 120


def _current_monitoring_delay_seconds():
    try:
        value = int(load_env_value('MONITORING_DELAY_SECONDS'))
    except (TypeError, ValueError):
        return MONITORING_DELAY_DEFAULT
    return max(MONITORING_DELAY_MIN, min(MONITORING_DELAY_MAX, value))


def _format_delay_seconds(seconds):
    """Human-readable form of a watchdog delay value, for the Settings
    slider's live label and the dashboard's Monitoring Delay stat.
    """
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        seconds = MONITORING_DELAY_DEFAULT
    if seconds < 60:
        return f"{seconds} sec"
    minutes, remainder = divmod(seconds, 60)
    if remainder:
        return f"{minutes} min {remainder} sec"
    return f"{minutes} min"


BACKUP_SERVICE_NAME = "folderw-backup.service"


def _backup_service_unit_exists():
    # Only true when setup.py's autostart configuration has written
    # folderw-backup.service (see setup.py:configure_systemd_autostart).
    # Installs that never enabled autostart have no such unit, so the
    # start/stop routes below fall back to the old Popen/psutil approach.
    try:
        result = subprocess.run(
            ["systemctl", "--user", "list-unit-files", BACKUP_SERVICE_NAME],
            capture_output=True, text=True, timeout=5,
        )
        return BACKUP_SERVICE_NAME in result.stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


def _backup_service_is_active():
    # Independent of is_watchdog_active() (which only ever detects
    # rsync_event_handler.py, i.e. MONITOR=1's watch loop): with a
    # scheduled interval (MONITOR=0) the service sits idle in
    # schedule.run_pending() between runs, no watchdog process at all,
    # so that check alone can't tell a dashboard control whether there's
    # anything to stop. This asks systemd directly instead.
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", BACKUP_SERVICE_NAME],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() == "active"
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


def _check_for_updates():
    """git fetch + compare local HEAD against origin/main. Returns a dict
    with commits_behind (int) and commit_summaries (newest first, capped
    at 10), or None if the check couldn't be completed at all (offline,
    not a git checkout, no 'origin' remote, git not installed) --
    callers should treat None as "unknown, try again later", not as "no
    update available", so a temporary network blip can't make the
    dashboard falsely claim everything is up to date.
    """
    if not os.path.isdir(os.path.join(BASE_DIR, '.git')):
        return None
    try:
        fetch = subprocess.run(
            ["git", "fetch", "origin", "main"],
            cwd=BASE_DIR, capture_output=True, text=True, timeout=20,
        )
        if fetch.returncode != 0:
            logger.warning(f"Update check: git fetch failed: {fetch.stderr.strip()}")
            return None
        count = subprocess.run(
            ["git", "rev-list", "--count", "HEAD..origin/main"],
            cwd=BASE_DIR, capture_output=True, text=True, timeout=10,
        )
        if count.returncode != 0:
            logger.warning(f"Update check: git rev-list failed: {count.stderr.strip()}")
            return None
        commits_behind = int(count.stdout.strip())
        commit_summaries = []
        if commits_behind:
            log_result = subprocess.run(
                ["git", "log", "--oneline", "HEAD..origin/main", "-n", "10"],
                cwd=BASE_DIR, capture_output=True, text=True, timeout=10,
            )
            if log_result.returncode == 0:
                commit_summaries = [line for line in log_result.stdout.splitlines() if line.strip()]
        return {"commits_behind": commits_behind, "commit_summaries": commit_summaries}
    except (subprocess.SubprocessError, FileNotFoundError, ValueError) as e:
        logger.warning(f"Update check failed: {e}")
        return None


def _cached_update_status():
    commits_behind = get_database_value('UPDATE_COMMITS_BEHIND', 'settings')
    summaries = get_database_value('UPDATE_COMMIT_SUMMARIES', 'settings')
    return {
        "update_commits_behind": int(commits_behind) if commits_behind else 0,
        "update_commit_summaries": summaries.split('\n') if summaries else [],
        "update_last_checked": get_database_value('UPDATE_LAST_CHECKED', 'settings') or None,
    }


def is_watchdog_active():
    # Deliberately matches rsync_event_handler.py, not main_backup.py: the
    # supervisor runs the first full backup to completion before ever
    # launching the event handler (see main_backup.py's run_regular_backup()
    # followed by start_event_backup()), so matching main_backup.py itself
    # would report the watchdog as "active" during that initial full
    # backup, before it's actually watching anything.
    for proc in psutil.process_iter(['cmdline']):
        try:
            cmdline = proc.info['cmdline'] or []
            if any('rsync_event_handler.py' in part for part in cmdline):
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return False


def get_backup_stats_context(database):
    # Shared by every route that renders index.html, so the Backup
    # Statistics panel is populated consistently everywhere — not just on
    # a plain dashboard load, but also right after clicking a Check button
    # or Start Full Backup.
    total, used, _free = destination_space()
    # interval=None (non-blocking): compares against the last call within
    # this same process rather than sampling over a fixed window, so it
    # doesn't add latency to the page load. The very first call after
    # startup returns 0.0 (or [0.0, ...] per-core) — accurate readings
    # kick in from the second request on.
    #
    # Per-core rather than just the overall average: rsync and du (the
    # actual work FolderW does) are both single-threaded per run, so on a
    # multi-core machine one core can be fully saturated while the average
    # still reads low and misleadingly implies plenty of headroom.
    per_core = psutil.cpu_percent(interval=None, percpu=True)
    cpu_percent = sum(per_core) / len(per_core) if per_core else 0
    mem = psutil.virtual_memory()
    return {
        "watchdog_active": is_watchdog_active(),
        "current_backup_size": get_database_value('CURRENT_BACKUP_SIZE', 'settings'),
        "dest_total": f"{total:.2f} GB" if total is not None else None,
        "dest_used": f"{used:.2f} GB" if used is not None else None,
        "last_session": get_last_session_number(database),
        "last_session_files": len(list_items_by_session(database)),
        # Snapshots physically on disk right now (Restore page's own count,
        # "full" excluded) -- deliberately NOT last_session/count_backup_
        # runs(), both of which only ever grow and don't reflect retention
        # (MAX_SNAPSHOTS) deleting old ones. Confirmed live this session:
        # showing an all-time run count next to a disk-truth Restore
        # listing reads as a bug ("6 backups shown, only 3 exist") even
        # though both numbers are individually correct for what they mean.
        "snapshot_count": len([b for b in list_backups() if b["id"] != "full"]),
        "cpu_percent": f"{cpu_percent:.1f}%",
        "cpu_cores": len(per_core),
        "cpu_busiest_core": f"{max(per_core):.1f}%" if per_core else None,
        "ram_used": f"{mem.used / (1024**3):.2f} GB",
        "ram_total": f"{mem.total / (1024**3):.2f} GB",
    }


def is_backup_running():
    # Deliberately excludes main_backup.py itself: in event-driven
    # (MONITOR=1) mode it's a supervisor process that runs for as long as
    # watchdog monitoring is active, not just while a backup is actually
    # executing — matching it here would make the dashboard show "Backup in
    # progress" essentially permanently. rsync_incremental.py is the actual
    # work being done: it starts and exits with each individual backup run.
    #
    # BACKUP_PREPARING is the one exception: main_backup.py sets it while
    # running its own prerequisite checks (settings/validation/evaluation,
    # including a real du scan) *before* rsync_incremental.py exists yet.
    # Without it, that startup gap read as "not running" — which the
    # frontend poll took to mean a just-started backup had already
    # finished, stopping the poll and showing "Backup complete" right as
    # the real backup was about to start.
    #
    # But the flag alone isn't trustworthy on its own: it's only cleared by
    # two "normal" exit paths (prerequisites failing, or rsync_incremental.py
    # starting), so an abrupt kill in between (a crash, uninstall.sh, `kill
    # -9`) orphans it at '1' forever — found live: with nothing at all
    # running, the dashboard still showed a phantom "backup in progress"
    # because a stale flag from an earlier killed run was never cleared.
    # Cross-checking that main_backup.py is actually still alive catches
    # that: a stale flag with no matching process is ignored instead of
    # trusted blindly.
    preparing = get_database_value('BACKUP_PREPARING', 'settings') == '1'
    for proc in psutil.process_iter(['cmdline']):
        try:
            cmdline = proc.info['cmdline'] or []
            if any('rsync_incremental.py' in part for part in cmdline):
                return True
            if preparing and any('main_backup.py' in part for part in cmdline):
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return False

def _backup_status_payload():
    percent = get_database_value('BACKUP_PROGRESS_PERCENT', 'settings')
    eta = get_database_value('BACKUP_ETA', 'settings')
    start_time_raw = get_database_value('BACKUP_START_TIME', 'settings')
    elapsed_seconds = None
    if start_time_raw:
        try:
            elapsed_seconds = int(time.time() - float(start_time_raw))
        except ValueError:
            pass
    # rsync's raw stdout (filenames + periodic progress2 stat lines,
    # interleaved) — lets the dashboard show real activity happening right
    # now, so a long silent stretch (e.g. deep in file-list scanning with
    # nothing new to transfer) is visibly "still working through files"
    # rather than indistinguishable from actually being stuck.
    rsync_tail = []
    try:
        # 20 lines: matches how many actually fit in the panel's fixed
        # height at its font/line-height before needing to scroll (16 still
        # left a bit of empty space below the last line).
        tail_result = subprocess.run(
            ['tail', '-n', '20', load_other_variables('rsync_txt')],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=2,
        )
        rsync_tail = [line for line in tail_result.stdout.decode('utf-8', errors='replace').splitlines() if line.strip()]
    except Exception:
        pass
    return {
        "running": is_backup_running(),
        "percent": percent or None,
        "eta": eta or None,
        "elapsed_seconds": elapsed_seconds,
        "current_backup_size": get_database_value('CURRENT_BACKUP_SIZE', 'settings') or None,
        "src_size": get_database_value('LAST_SRC_SIZE', 'settings') or None,
        "rsync_tail": rsync_tail,
        "watchdog_active": is_watchdog_active(),
        "check_step": get_database_value('CHECK_STEP', 'settings') or None,
    }


@app.get("/backup-status")
async def backup_status_endpoint():
    return JSONResponse(_backup_status_payload())


@app.get("/backup-status-stream")
async def backup_status_stream():
    # Server-Sent Events -- replaces the dashboard's old 2-second fetch()
    # poll for the same data (progress %, ETA, current size, watchdog
    # dot, live rsync log tail) with one held connection instead of a
    # fresh HTTP request every 2 seconds: less request/response overhead
    # per update, and updates can push at whatever cadence the server
    # wants rather than being capped at the client's poll interval.
    #
    # Ends the stream (rather than holding it open indefinitely) once
    # running is False -- the client explicitly closes its EventSource
    # on that same signal, since a naturally-ended SSE stream otherwise
    # triggers the browser's automatic reconnect, which would silently
    # re-poll forever after a backup finishes.
    async def event_generator():
        while True:
            payload = _backup_status_payload()
            yield f"data: {json.dumps(payload)}\n\n"
            if not payload["running"]:
                break
            await asyncio.sleep(1)
    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.post("/stop-backup")
async def stop_backup():
    # A stopped run isn't a completed one — the Settings/Validation/
    # Evaluation dots are driven by these persisted flags (see load_
    # persisted_checks in the / route), and previously nothing cleared them
    # on stop, so they stayed green from whatever the last successful run
    # left behind even though nothing is currently confirmed or running.
    clear_persisted_checks()
    set_database_value('CHECK_STEP', '')
    if _backup_service_unit_exists():
        try:
            subprocess.run(["systemctl", "--user", "stop", BACKUP_SERVICE_NAME], check=True)
            logger.info("Stopped folderw-backup.service on request.")
            return JSONResponse({"message": "Backup and monitoring stopped."})
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to stop {BACKUP_SERVICE_NAME}: {e}")
            return JSONResponse({"message": f"Failed to stop: {e}"}, status_code=500)

    # No systemd unit (autostart never configured) — fall back to killing
    # the known process names directly. main_backup.py's own retry loop
    # would otherwise just relaunch rsync_event_handler.py after a bare
    # kill of that child, so the supervisor itself has to go too.
    killed = 0
    for proc in psutil.process_iter(['cmdline']):
        try:
            cmdline = proc.info['cmdline'] or []
            if any('main_backup.py' in part or 'rsync_event_handler.py' in part for part in cmdline):
                proc.terminate()
                killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    logger.info(f"Stopped {killed} backup/watchdog process(es) directly.")
    return JSONResponse({"message": f"Stopped {killed} process(es)." if killed else "Nothing was running."})


@app.post("/start-backup-service")
async def start_backup_service():
    # Deliberately just "start", never "restart" -- this is the general,
    # always-available counterpart to the Stop button above, meant to
    # resume a deliberately-stopped service, not to interrupt/replace a
    # currently-running one the way /run-all-steps' restart does. A plain
    # start is also a safe no-op if it's already running.
    #
    # No settings/validation/evaluation gate here (unlike /run-all-steps)
    # -- main_backup.py re-checks all three itself the moment it starts
    # (see backup_prerequisites_met()) and exits cleanly without running
    # anything if they fail, so this can't bypass that safety net.
    #
    # Only meaningful with a real systemd unit -- without autostart
    # configured, there's no persistent service to "start" independent of
    # actually launching a backup (see /run-all-steps' Popen fallback).
    if not _backup_service_unit_exists():
        return JSONResponse({"message": "No backup service is configured (autostart was never enabled)."}, status_code=400)
    try:
        subprocess.run(["systemctl", "--user", "start", BACKUP_SERVICE_NAME], check=True)
        logger.info("Started folderw-backup.service on request.")
        return JSONResponse({"message": "Backup service started."})
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to start {BACKUP_SERVICE_NAME}: {e}")
        return JSONResponse({"message": f"Failed to start: {e}"}, status_code=500)


@app.get("/check-for-updates")
async def check_for_updates():
    # Blocking (network + subprocess) -- run off the event loop so this
    # can't stall every other request while it waits on git.
    result = await run_in_threadpool(_check_for_updates)
    if result is None:
        # Keep showing the last known-good state rather than flipping to
        # "no updates" on a transient failure (offline, DNS hiccup) --
        # that would be actively misleading, not just uninformative.
        cached = _cached_update_status()
        return JSONResponse({
            "checked": False,
            "commits_behind": cached["update_commits_behind"],
            "commit_summaries": cached["update_commit_summaries"],
        })
    set_database_value('UPDATE_COMMITS_BEHIND', result['commits_behind'])
    set_database_value('UPDATE_COMMIT_SUMMARIES', '\n'.join(result['commit_summaries']))
    set_database_value('UPDATE_LAST_CHECKED', datetime.now().isoformat())
    return JSONResponse({
        "checked": True,
        "commits_behind": result['commits_behind'],
        "commit_summaries": result['commit_summaries'],
    })


@app.post("/run-update")
async def run_update():
    # update.sh restarts folderw.service itself once it's done (see
    # update.sh) -- if that ran synchronously inside this request
    # handler, the restart would kill the very process handling this
    # request before a response could ever be sent, and the browser
    # would just see a dropped connection with no explanation. Launching
    # it detached (own session, output to a log file — same pattern as
    # /run-all-steps' no-systemd fallback below) lets this handler return
    # immediately; the frontend polls until the dashboard comes back up
    # on the other side of the restart.
    update_script = os.path.join(BASE_DIR, "update.sh")
    if not os.path.isfile(update_script):
        return JSONResponse({"message": "update.sh not found -- is this a git checkout?"}, status_code=400)
    log_dir = os.path.join(BASE_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    update_log = open(os.path.join(log_dir, "update.log"), "a")
    try:
        subprocess.Popen(
            ["bash", update_script],
            cwd=BASE_DIR,
            stdout=update_log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as e:
        logger.error(f"Failed to launch update.sh: {e}")
        return JSONResponse({"message": f"Failed to start the update: {e}"}, status_code=500)
    logger.info("Update started via dashboard.")
    return JSONResponse({"message": "Update started -- the dashboard will reload automatically once it's back."})


@app.get("/run-all-steps", response_class=HTMLResponse)
async def run_all_steps(request: Request, force_full: bool = False):
    logger.info(f"Response received from Front End for /run-all-steps (force_full={force_full})")
    src_dir = load_env_value('SRC_DIR')
    base_dir = load_env_value('BASE_DIR')
    # CHECK_STEP: lets the frontend show which of settings/validation/
    # evaluation is currently being checked while this request is still in
    # flight (polled via /backup-status). Each check below is run via
    # run_in_threadpool rather than called directly: they're plain blocking
    # functions, and calling them inline on an async def route would block
    # Uvicorn's single-threaded event loop for as long as they take —
    # including evaluation's real du scan — during which the server can't
    # respond to *any* other request, so the polling itself would never get
    # a response until everything was already done. Found live: every dot
    # was flipping green in one batch right at the end regardless of how
    # far apart the steps actually ran, because the poll requests were
    # queued behind this handler the whole time, not actually blocked by
    # each other.
    set_database_value('CHECK_STEP', 'settings')
    settings_sent, settings, missing_vars = await run_in_threadpool(check_env_variables)
    if not settings_sent:
        return templates.TemplateResponse("index.html", {
            "request": request,
            "logo": logo,
            "setting": settings,
            "missing_settings": missing_vars,
            "settings_sent": settings_sent,
            "settings_check_passed": settings_sent,
            **_dashboard_settings_context(),
        })

    set_database_value('CHECK_STEP', 'validation')
    validation_status, validation_message = await run_in_threadpool(validate_all_conditions, src_dir, base_dir)
    if not validation_status:
        return templates.TemplateResponse("index.html", {
            "request": request,
            "logo": logo,
            "settings": settings,
            "missing_settings": missing_vars,
            "settings_sent": settings_sent,
            "settings_check_passed": settings_sent,
            "validation_status": validation_status,
            "validation_message": validation_message,
            **_dashboard_settings_context(),
        })

    set_database_value('CHECK_STEP', 'evaluation')
    src_size, dest_space, can_backup, evaluation_message = await run_in_threadpool(evaluation_of_resources)
    persist_check_results(
        settings_sent=settings_sent,
        validation_status=validation_status,
        evaluation_status=can_backup,
        LAST_SRC_SIZE=src_size,
        LAST_DEST_SPACE=dest_space,
    )
    if not can_backup:
        return templates.TemplateResponse("index.html", {
            "request": request,
            "logo": logo,
            "missing_settings": missing_vars,
            "settings_sent": settings_sent,
            "settings_check_passed": settings_sent,
            "validation_status": validation_status,
            "validation_message": validation_message,
            "evaluation_status": can_backup,
            "evaluation_message": evaluation_message,
            "src_size": src_size,
            "dest_space": dest_space,
            **_dashboard_settings_context(),
        })

    success_message = "All settings, validations, and evaluations are correct. READY"
    set_database_value('CHECK_STEP', 'starting')
    # force_full=True (the "Start Full Backup" button, shown before any
    # full backup exists yet): a deliberate request for a real full
    # backup right now -- clear this unconditionally so main_backup.py's
    # restart-skip shortcut (see main_backup.py) never applies to this
    # specific path, only to routine restarts (deploys, crash-restarts,
    # reboots) where skipping a redundant re-sync is actually wanted.
    #
    # force_full=False (the "Create a Manual Snapshot" button, shown
    # once a full backup already exists): leaves FULL_BACKUP_COMPLETED
    # alone -- the full backup itself shouldn't redo -- but the restart-
    # skip shortcut in main_backup.py otherwise skips running ANY
    # backup at all when that flag is already '1' (it goes straight to
    # scheduling/monitoring instead), which silently made this button
    # do nothing. MANUAL_SNAPSHOT_REQUESTED is the explicit signal that
    # tells main_backup.py "run one regular snapshot immediately even
    # though the shortcut would normally apply" -- same idea as
    # FULL_BACKUP_COMPLETED=0 above, just for the non-full case. Found
    # live: with nothing distinguishing this restart from a routine
    # deploy/crash restart, main_backup.py couldn't tell "someone
    # deliberately clicked Manual Snapshot" from "this process just
    # happened to restart" -- both look identical from its side.
    if force_full:
        set_database_value('FULL_BACKUP_COMPLETED', '0')
    else:
        set_database_value('MANUAL_SNAPSHOT_REQUESTED', '1')

    try:
        if _backup_service_unit_exists():
            # restart (not start) so re-clicking while a backup/watchdog is
            # already running replaces it with a fresh run instead of being
            # a no-op — and, since it's its own systemd unit with default
            # KillMode, the old one is guaranteed to be fully stopped first
            # (no duplicate watchdog processes left behind).
            subprocess.run(["systemctl", "--user", "restart", BACKUP_SERVICE_NAME], check=True)
        else:
            # No systemd unit for it (autostart was never configured) —
            # start_new_session detaches main_backup.py from this process's
            # session, so it survives server.py restarting or its terminal
            # closing. stdout=PIPE without ever being read risks the child
            # blocking once the OS pipe buffer fills, so redirect to a log
            # file instead.
            log_dir = os.path.join(BASE_DIR, "logs")
            os.makedirs(log_dir, exist_ok=True)
            backup_log = open(os.path.join(log_dir, "main_backup.log"), "a")
            subprocess.Popen(
                [sys.executable, os.path.join(BASE_DIR, "main_backup.py")],
                stdout=backup_log,
                stderr=backup_log,
                start_new_session=True,
            )
        backup_status = "Backup started."
    except Exception as e:
        backup_status = f"Failed to start backup process: {str(e)}"
    
    return templates.TemplateResponse("index.html", {
        "request": request,
        "logo": logo,
        "success_message": success_message,
        "settings_sent": settings_sent,
        "settings_check_passed": settings_sent,
        "validation_status": validation_status,
        "validation_message": validation_message,
        "evaluation_status": can_backup,
        "evaluation_message": "",
        "backup_status": backup_status,
        "src_size": src_size,
        "dest_space": dest_space,
        "can_backup": can_backup,
        **_dashboard_settings_context(),
    })

def _dashboard_settings_context():
    """The "current settings, as configured" portion of index.html's
    context -- values that only depend on the current .env, not on
    anything specific to whichever route is rendering the page (a plain
    dashboard load vs. one of /run-all-steps' several stages).

    Shared so every render of index.html shows the same complete
    picture. Found live, the hard way: /run-all-steps had its own
    separate, never-updated copy of this dict (four render points, one
    per stage) that silently fell behind every time a new field was
    added to the dashboard's Settings panel -- Backup Method, Snapshots
    to Keep, Login, notifications, the pre/post-backup hook scripts all
    rendered blank specifically right after a Manual Snapshot/Start
    Full Backup click replaced the page via document.write(), even
    though a plain page reload showed them correctly.
    """
    database = load_env_value('DATABASE')
    return {
        "src_dir": load_env_value('SRC_DIR'),
        "base_dir": load_env_value('BASE_DIR'),
        "full_name": load_env_value('FULL_NAME'),
        "database": database,
        "monitor": load_env_value('MONITOR'),
        "interval": load_env_value('BACKUP_INTERVAL'),
        "has_completed_backup": has_completed_backup(database),
        "backup_method": load_env_value('BACKUP_METHOD') or 'differential',
        "max_snapshots": load_env_value('MAX_SNAPSHOTS'),
        "login_enabled": bool(load_env_value('ADMIN_PASSWORD_HASH')),
        "notify_send_always": load_env_value('NOTIFY_SEND_ALWAYS') == '1',
        # Whether an external notification service is configured, not
        # the URL(s) themselves -- an Apprise URL routinely embeds an
        # auth token (e.g. pover://user@token), which has no business
        # being shown on a dashboard anyone glancing at the screen (or a
        # screenshot) could read.
        "has_notify_urls": bool(load_env_value('NOTIFY_URLS')),
        "pre_backup_script": load_env_value('PRE_BACKUP_SCRIPT'),
        "post_backup_script": load_env_value('POST_BACKUP_SCRIPT'),
        "backup_service_unit_exists": _backup_service_unit_exists(),
        "backup_service_active": _backup_service_is_active(),
        "monitoring_delay_display": _format_delay_seconds(load_env_value('MONITORING_DELAY_SECONDS') or MONITORING_DELAY_DEFAULT),
        **_cached_update_status(),
        **get_backup_stats_context(database),
    }


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    # Restore the last known Validation/Evaluation check results, so
    # returning to the dashboard doesn't require re-running them — those
    # involve real filesystem/disk-usage checks, worth caching. Settings is
    # different: it's just reading already-loaded env vars (cheap, no I/O),
    # and a persisted flag defaults to false on a brand-new database with
    # nothing ever checked yet — showing "Missing settings" on a fresh
    # install's first load even with a fully filled-in .env. Always fresh.
    #
    # settings_check_passed (below) is a separate thing: the dot next to
    # "Settings" tracks whether that check has actually run as part of a
    # Start/Stop cycle (same persisted flag as validation/evaluation, reset
    # together on Stop) — not whether the .env happens to be valid right
    # now, which is what settings_sent/missing_settings answer instead.
    settings_sent, _settings, missing_vars = check_env_variables()
    persisted = load_persisted_checks()
    return templates.TemplateResponse("index.html",
        { "request": request,
        "logo": logo,
        "settings_sent": settings_sent,
        "settings_check_passed": persisted["settings_sent"],
        "missing_settings": missing_vars,
        "validation_status": persisted["validation_status"],
        "evaluation_status": persisted["evaluation_status"],
        "src_size": persisted["src_size"],
        "dest_space": persisted["dest_space"],
        "can_backup": persisted["can_backup"],
        "backup_in_progress": is_backup_running(),
        **_dashboard_settings_context(),
        })

def _build_file_tree(files):
    """Nest a flat list of {'path': rel, 'size': ...} (see list_files_in_
    backup()) into a tree of directories, for the Restore/Browse page's
    collapsible file-tree view -- a deep snapshot's flat path list (e.g.
    hundreds of files under a handful of top-level folders) is much
    harder to scan than the same thing grouped by directory.

    Children are a dict (not a list) keyed by name for O(1) insertion
    while walking the flat list; the template/JS that consumes this
    sorts and iterates them for display, same as a list would render.
    """
    root = {"name": "", "type": "dir", "children": {}}
    for f in files:
        parts = f["path"].split("/")
        node = root
        for i, part in enumerate(parts):
            if i == len(parts) - 1:
                node["children"][part] = {"name": part, "type": "file", "path": f["path"], "size": f["size"]}
            else:
                existing = node["children"].get(part)
                if existing is None or existing["type"] != "dir":
                    existing = {"name": part, "type": "dir", "children": {}}
                    node["children"][part] = existing
                node = existing
    return root


RESTORE_PAGE_SIZE = 20

def _restore_page_context(page=1):
    """Shared pagination slicing for every route that renders restore.html
    -- list_backups() itself is fast (cached stats, see restore_operations
    .py), but a snapshot list in the dozens (MAX_SNAPSHOTS) is still a lot
    of rows to dump in one table.
    """
    all_backups = list_backups()
    total_pages = max(1, (len(all_backups) + RESTORE_PAGE_SIZE - 1) // RESTORE_PAGE_SIZE)
    page = min(max(1, page), total_pages)
    start = (page - 1) * RESTORE_PAGE_SIZE
    return {
        "backups": all_backups[start:start + RESTORE_PAGE_SIZE],
        "page": page,
        "total_pages": total_pages,
    }

@app.get("/restore", response_class=HTMLResponse)
async def restore_page(request: Request, page: int = 1):
    return templates.TemplateResponse("restore.html", {
        "request": request,
        "logo": logo,
        **_restore_page_context(page),
    })

@app.get("/restore/browse/{backup_id:path}", response_class=HTMLResponse)
async def restore_browse(request: Request, backup_id: str, search: str = ""):
    backup_path = get_backup_path(backup_id)
    if backup_path is None:
        return templates.TemplateResponse("restore.html", {
            "request": request,
            "logo": logo,
            "error": f"Backup not found: {backup_id}",
            **_restore_page_context(),
        })
    files, truncated = list_files_in_backup(backup_path, search=search or None)
    return templates.TemplateResponse("restore_browse.html", {
        "request": request,
        "logo": logo,
        "backup_id": backup_id,
        "files": files,
        "tree_json": _json_for_script(_build_file_tree(files)),
        "truncated": truncated,
        "search": search,
    })

@app.post("/restore/note")
async def restore_set_note(backup_id: str = Form(...), note: str = Form(""), page: int = Form(1)):
    backup_path = get_backup_path(backup_id)
    if backup_path is not None:
        set_snapshot_note(backup_path, note)
    # Redirect (not a template response) so refreshing the page afterward
    # doesn't resubmit the note -- same reasoning as any POST-then-refresh
    # form. Preserves whichever page of the (now paginated) list the note
    # was edited from.
    return RedirectResponse(url=f"/restore?page={page}", status_code=303)

@app.post("/restore/execute", response_class=HTMLResponse)
async def restore_execute(request: Request, backup_id: str = Form(...), selected_paths: list[str] = Form(default=[]), combine_with_full: str = Form("1")):
    try:
        dest_root, file_count = restore_backup(backup_id, selected_paths or None, combine_with_full=(combine_with_full == "1"))
        # Captured for Recovery History at restore time, not looked up
        # later -- the source backup/snapshot can be pruned by retention
        # or deleted afterward, at which point backup_id alone wouldn't
        # resolve to a label anymore. Falls back to the raw id itself if
        # the backup has already vanished by the time this runs (rare,
        # but not impossible with a fast retention cleanup).
        matching_backup = next((b for b in list_backups() if b["id"] == backup_id), None)
        backup_label = matching_backup["label"] if matching_backup else backup_id
        if selected_paths:
            restore_type = "Selected Files"
        elif matching_backup and matching_backup["is_delta_only"]:
            restore_type = "Full Restore" if combine_with_full == "1" else "Only Snapshot"
        else:
            restore_type = "Entire Backup"
        record_restore_run(backup_id, backup_label, dest_root, file_count, restore_type)
        return templates.TemplateResponse("restore.html", {
            "request": request,
            "logo": logo,
            "success": f"Restored {file_count} file(s) to {dest_root}",
            **_restore_page_context(),
        })
    except ValueError as e:
        return templates.TemplateResponse("restore.html", {
            "request": request,
            "logo": logo,
            "error": str(e),
            **_restore_page_context(),
        })


@app.post("/restore/compile", response_class=HTMLResponse)
async def restore_compile(request: Request):
    new_path, file_count = compile_latest_snapshot()
    if new_path is None:
        return templates.TemplateResponse("restore.html", {
            "request": request,
            "logo": logo,
            "error": "No snapshots to compile yet -- this needs at least one Incremental or Differential snapshot beyond the full backup.",
            **_restore_page_context(),
        })
    return templates.TemplateResponse("restore.html", {
        "request": request,
        "logo": logo,
        "success": f"Compiled {file_count} file(s) into a new merged snapshot at {new_path}.",
        **_restore_page_context(),
    })


@app.get("/find-file", response_class=HTMLResponse)
async def find_file(request: Request, query: str = ""):
    results = []
    truncated = False
    if query.strip():
        results, truncated = search_all_backups(query.strip())
    return templates.TemplateResponse("find-file.html", {
        "request": request,
        "logo": logo,
        "query": query,
        "results": results,
        "truncated": truncated,
    })


@app.post("/find-file/restore", response_class=HTMLResponse)
async def find_file_restore(
    request: Request,
    backup_id: str = Form(...),
    rel_path: str = Form(...),
    target: str = Form(...),
    query: str = Form(""),
):
    context = {"request": request, "logo": logo, "query": query}
    try:
        if target == "source":
            target_dir = load_env_value('SRC_DIR')
            dest = restore_single_path(backup_id, rel_path, target_dir, safety_backup=True)
            context["success"] = (
                f"Restored '{rel_path}' directly into your source directory ({dest}). "
                "If something already existed at that exact path, it was moved aside "
                "(suffixed with a timestamp) rather than overwritten, in case this wasn't what you meant to do."
            )
        else:
            timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
            label = backup_id.replace('/', '-').replace(':', '-')
            item_name = os.path.basename(rel_path.rstrip('/')) or rel_path
            target_dir = os.path.join(load_env_value('BASE_DIR'), "Restored", f"{timestamp}_{label}_{item_name}")
            os.makedirs(target_dir, exist_ok=True)
            dest = restore_single_path(backup_id, rel_path, target_dir, safety_backup=False)
            context["success"] = f"Restored '{rel_path}' to {dest}."

        file_count = 1 if (os.path.isfile(dest) or os.path.islink(dest)) else summarize_folder(dest)[0]
        matching_backup = next((b for b in list_backups() if b["id"] == backup_id), None)
        backup_label = matching_backup["label"] if matching_backup else backup_id
        restore_type = f"Single item ({'Source' if target == 'source' else 'Recovery Folder'})"
        record_restore_run(backup_id, backup_label, dest, file_count, restore_type)
    except ValueError as e:
        context["error"] = str(e)

    results, truncated = search_all_backups(query.strip()) if query.strip() else ([], False)
    context["results"] = results
    context["truncated"] = truncated
    return templates.TemplateResponse("find-file.html", context)


def _read_file_exclusions():
    # logs/custom_exclude.txt (one pattern per line) is the single,
    # fully user-editable source of truth for what's excluded from
    # backup -- re-joined with ", " here only for display in the
    # Settings form field.
    path = load_other_variables('custom_exclude_file')
    with open(path, 'r') as f:
        return ', '.join(line.strip() for line in f if line.strip())


def _write_file_exclusions(raw):
    patterns = [p.strip() for p in raw.split(',') if p.strip()]
    path = load_other_variables('custom_exclude_file')
    with open(path, 'w') as f:
        for pattern in patterns:
            f.write(pattern + '\n')
    return patterns


def _settings_page_context():
    """Every value settings.html needs, read fresh from .env/other
    persisted state. Shared by every render point (plain GET, both
    /submit/ validation-error paths, save success, and the database-
    management actions below, which used to render their own separate
    manage-databases.html) so a field added here can't silently go
    missing on one of them -- the exact bug this session already hit
    twice, for index.html and for this same page.

    Callers that need to echo back not-yet-saved, just-submitted form
    values (the /submit/ validation-error paths) do
    {**_settings_page_context(), "the_one_field": user_submitted_value}
    afterward, overriding just what needs to reflect the in-flight
    submission rather than what's currently on disk.
    """
    logfile = load_other_variables('logfile')
    active_database = load_env_value('DATABASE')
    return {
        "log_dir": os.path.dirname(logfile),
        "src_dir": load_env_value('SRC_DIR'),
        "base_dir": load_env_value('BASE_DIR'),
        "full_name": load_env_value('FULL_NAME'),
        "database": active_database,
        "monitor_checked": load_env_value('MONITOR') == '1',
        "interval": load_env_value('BACKUP_INTERVAL'),
        "max_snapshots": load_env_value('MAX_SNAPSHOTS'),
        "login_enabled": bool(load_env_value('ADMIN_PASSWORD_HASH')),
        "notify_urls": load_env_value('NOTIFY_URLS'),
        "notify_send_always": load_env_value('NOTIFY_SEND_ALWAYS') == '1',
        "backup_method": load_env_value('BACKUP_METHOD') or 'differential',
        "file_exclusions": _read_file_exclusions(),
        "monitoring_delay_seconds": _current_monitoring_delay_seconds(),
        "monitoring_delay_min": MONITORING_DELAY_MIN,
        "monitoring_delay_max": MONITORING_DELAY_MAX,
        "active_database": os.path.basename(active_database or ""),
        "previous_databases": list_previous_databases(),
    }


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "logo": logo,
        **_settings_page_context(),
    })


@app.post("/test-notification")
async def test_notification(notify_urls: str = Form(""), notify_send_always: str = Form("0")):
    urls = [u.strip() for u in notify_urls.split(',') if u.strip()]
    send_desktop = notify_send_always == '1'

    if not urls and not send_desktop:
        return JSONResponse({"success": False, "message": "No notification URL(s) provided, and desktop notifications aren't enabled."})

    messages = []
    any_success = False

    if urls:
        apobj = apprise.Apprise()
        valid_count = sum(1 for url in urls if apobj.add(url))
        if valid_count == 0:
            messages.append("No valid URL(s) — check the format.")
        else:
            try:
                result = apobj.notify(title="FolderW Test Notification", body="If you're seeing this, your notification setup works.")
                if result:
                    any_success = True
                    messages.append("URL(s): sent, check your device.")
                else:
                    messages.append("URL(s): Apprise reported failure — check the URL and service status.")
            except Exception as e:
                logger.error(f"Test notification failed: {e}")
                messages.append(f"URL(s): error ({e}).")

    if send_desktop:
        # notify-send doesn't report success/failure back to the caller
        # the way Apprise does (it's fire-and-forget over D-Bus) -- if
        # this doesn't raise, treat it as sent and let the user's own
        # eyes confirm whether it actually showed up on screen.
        _notify_desktop("FolderW Test Notification", "If you're seeing this, your notification setup works.", 'normal')
        any_success = True
        messages.append("Desktop: sent — check your screen.")

    return JSONResponse({"success": any_success, "message": " ".join(messages)})


@app.get("/backup-hooks", response_class=HTMLResponse)
async def backup_hooks_page(request: Request):
    return templates.TemplateResponse("backup_hooks.html", {
        "request": request,
        "logo": logo,
        "pre_backup_script": load_env_value('PRE_BACKUP_SCRIPT'),
        "post_backup_script": load_env_value('POST_BACKUP_SCRIPT'),
    })


@app.post("/backup-hooks/save", response_class=HTMLResponse)
async def backup_hooks_save(request: Request, pre_backup_script: str = Form(""), post_backup_script: str = Form("")):
    save_env_values({
        "PRE_BACKUP_SCRIPT": pre_backup_script.strip(),
        "POST_BACKUP_SCRIPT": post_backup_script.strip(),
    })
    return templates.TemplateResponse("backup_hooks.html", {
        "request": request,
        "logo": logo,
        "success": "Backup hooks saved.",
        "pre_backup_script": pre_backup_script.strip(),
        "post_backup_script": post_backup_script.strip(),
    })


@app.post("/backup-hooks/test")
async def backup_hooks_test(script_path: str = Form(...), which: str = Form(...)):
    # Tests whatever's currently typed into the field client-side, not
    # necessarily the saved value -- lets a script be verified before
    # trusting it to run (and potentially abort a real backup) during
    # an actual backup, matching Settings' "Send Test Notification"
    # pattern of testing the form's live contents.
    script_path = script_path.strip()
    label = 'Pre-backup' if which == 'pre' else 'Post-backup'
    if not script_path:
        return JSONResponse({"success": False, "message": "No script path entered."})
    success, message = run_hook_script(script_path, label)
    return JSONResponse({"success": success, "message": message})


def _is_valid_folder_name(name):
    # Linux (the only supported platform) only actually forbids '/' and
    # the null byte in a directory name -- everything else is technically
    # legal at the filesystem level. '.' and '..' are reserved. Mirrors
    # setup.py's own _is_valid_folder_name() (kept separate, not shared --
    # different processes/files).
    return bool(name) and name not in ('.', '..') and '/' not in name and '\x00' not in name


@app.post("/submit/", response_class=HTMLResponse)
async def submit_settings(
    request: Request,
    src_dir: str = Form(""),
    base_dir: str = Form(""),
    full_name: str = Form(""),
    database: str = Form(""),
    interval: str = Form(""),
    max_snapshots: str = Form(""),
    monitor: str = Form(None),
    new_password: str = Form(""),
    require_login: str = Form(None),
    notify_urls: str = Form(""),
    notify_send_always: str = Form(None),
    backup_method: str = Form("differential"),
    file_exclusions: str = Form(""),
    monitoring_delay_seconds: str = Form(str(MONITORING_DELAY_DEFAULT)),
):
    logger.info("Response received from Front End for /submit/")
    monitor_enabled = monitor is not None
    # Clamped rather than rejected with an error -- the slider itself
    # can't produce an out-of-range value, so this only ever matters for
    # a raw/malformed POST, and it's not an identity-affecting setting
    # worth interrupting the save over.
    try:
        monitoring_delay_value = int(monitoring_delay_seconds)
    except (TypeError, ValueError):
        monitoring_delay_value = MONITORING_DELAY_DEFAULT
    monitoring_delay_value = max(MONITORING_DELAY_MIN, min(MONITORING_DELAY_MAX, monitoring_delay_value))

    # Captured before save_env_values overwrites .env, so we can tell
    # afterward whether the backup identity itself changed (as opposed to,
    # say, just the snapshot retention count or interval). BACKUP_METHOD
    # is included too: switching between incremental (--link-dest, each
    # snapshot a complete tree) and differential (--compare-dest, each
    # snapshot a delta against the original) mid-stream would otherwise
    # leave a mix of both snapshot shapes under the same Snapshots
    # folder, which cleanup_old_snapshots()/list_backups() don't
    # distinguish between.
    # file_exclusions (what actually gets copied) is included here for the
    # same reason as BACKUP_METHOD: changing it means the "backup" a
    # snapshot represents is no longer the same set of files, so old
    # history/validation results shouldn't carry over silently.
    old_identity = (
        load_env_value('SRC_DIR'),
        load_env_value('BASE_DIR'),
        load_env_value('FULL_NAME'),
        load_env_value('BACKUP_METHOD'),
        _read_file_exclusions(),
    )

    def _settings_error(error, max_snapshots_value):
        return templates.TemplateResponse("settings.html", {
            "request": request,
            "logo": logo,
            "error": error,
            **_settings_page_context(),
            "monitor_checked": monitor_enabled,
            "max_snapshots": max_snapshots_value,
            "file_exclusions": file_exclusions,
            "monitoring_delay_seconds": monitoring_delay_value,
        })

    max_snapshots = max_snapshots.strip()
    if max_snapshots and (not max_snapshots.isdigit() or int(max_snapshots) <= 0):
        return _settings_error("Snapshots to Keep must be a positive whole number, or left blank.", max_snapshots)

    # Database must end in .db -- create_all_tables() below happily
    # creates a SQLite file under any name at all, so nothing else
    # catches a typo'd extension (or none at all) until much later, if
    # ever.
    if database.strip() and not database.strip().lower().endswith('.db'):
        return _settings_error("Database file name must end with .db", max_snapshots)

    if full_name.strip() and not _is_valid_folder_name(full_name.strip()):
        return _settings_error("Full Backup Folder Name can't contain '/', be empty, or be '.' / '..'.", max_snapshots)

    require_login_enabled = require_login is not None
    new_password = new_password.strip()
    if new_password:
        # Typing a new password always (re-)enables login, regardless of
        # the checkbox — setting a password is an unambiguous "yes".
        admin_password_hash = hash_password(new_password)
    elif not require_login_enabled:
        admin_password_hash = ""  # Unchecked with no new password: disable login.
    else:
        admin_password_hash = load_env_value('ADMIN_PASSWORD_HASH') or ""

    new_values = {
        "SRC_DIR": src_dir.strip() or load_env_value('SRC_DIR'),
        "BASE_DIR": base_dir.strip() or load_env_value('BASE_DIR'),
        "FULL_NAME": full_name.strip() or load_env_value('FULL_NAME'),
        "DATABASE": database.strip() or load_env_value('DATABASE'),
        "MONITOR": "1" if monitor_enabled else "0",
        "BACKUP_INTERVAL": "False" if monitor_enabled else (interval or load_env_value('BACKUP_INTERVAL') or "hourly"),
        "MAX_SNAPSHOTS": max_snapshots,
        "ADMIN_PASSWORD_HASH": admin_password_hash,
        "NOTIFY_URLS": notify_urls.strip(),
        "NOTIFY_SEND_ALWAYS": "1" if notify_send_always is not None else "0",
        "BACKUP_METHOD": "incremental" if backup_method == "incremental" else "differential",
        "MONITORING_DELAY_SECONDS": str(monitoring_delay_value),
        # PRE_BACKUP_SCRIPT/POST_BACKUP_SCRIPT deliberately NOT included
        # here -- moved to their own page/save route (see /backup-hooks
        # below). Writing them from this form too, even just re-saving
        # load_env_value()'s own current value, would risk silently
        # clearing whatever the dedicated page's own save last set if
        # this form's fields (now gone from settings.html) ever came
        # back empty/stale.
    }

    # BASE_DIR (module-level, this file's own directory) is FolderW's own
    # install location -- container_dir (BASE_DIR/FULL_NAME from the form,
    # confusingly the same setting name but a user-chosen backup
    # destination) must never collide with it. Found live: FULL_NAME set
    # to the same name as the app's own folder, with BASE_DIR pointing at
    # its parent, made the backup container the exact same directory as
    # the app's install -- ensure_backup_folder_icon() then tagged
    # FolderW's own code directory with a custom folder icon (gio
    # metadata persists per-path independent of file content, so it kept
    # showing even after a fresh reinstall at the same path). Checked
    # here, before saving, rather than after the fact.
    container_dir = os.path.realpath(os.path.join(new_values["BASE_DIR"], new_values["FULL_NAME"]))
    app_dir_real = os.path.realpath(BASE_DIR)
    if (container_dir == app_dir_real
            or app_dir_real.startswith(container_dir + os.sep)
            or container_dir.startswith(app_dir_real + os.sep)):
        return templates.TemplateResponse("settings.html", {
            "request": request,
            "logo": logo,
            "error": (
                f"Base Backup Directory + Full Backup Folder Name ({container_dir}) "
                f"can't overlap with FolderW's own install directory ({app_dir_real}) -- "
                "choose a different Full Backup Folder Name or Base Backup Directory."
            ),
            **_settings_page_context(),
            "monitor_checked": monitor_enabled,
            "file_exclusions": file_exclusions,
            "monitoring_delay_seconds": monitoring_delay_value,
        })

    save_env_values(new_values)
    saved_file_exclusions = _write_file_exclusions(file_exclusions)

    if new_values["DATABASE"]:
        create_all_tables(new_values["DATABASE"])

        new_identity = (new_values["SRC_DIR"], new_values["BASE_DIR"], new_values["FULL_NAME"], new_values["BACKUP_METHOD"], ', '.join(saved_file_exclusions))
        if new_identity != old_identity:
            # Pointed FolderW at a different backup (source, destination, or
            # container folder) — old session/change/statistics history
            # belongs to the previous backup and would otherwise corrupt
            # session numbering and historical stats for the new one.
            reset_backup_history(new_values["DATABASE"])
            # Old validation/evaluation results (does the source exist, is
            # there enough destination space) were computed against the
            # PREVIOUS paths -- no longer meaningful for the new ones.
            # Scoped to an actual identity change only: found live, this
            # used to run on every settings save (password, notifications,
            # MAX_SNAPSHOTS included), which cleared validation_status/
            # evaluation_status even though paths never changed --
            # ready_for_manual_snapshot (index.html) requires both to be
            # True, so the dashboard silently reverted from "Create a
            # Manual Snapshot" back to "Start Full Backup" after ANY save,
            # and clicking the only button left forced a full-backup redo.
            clear_persisted_checks()

    return templates.TemplateResponse("settings.html", {
        "request": request,
        "logo": logo,
        "success": "Settings saved successfully.",
        **_settings_page_context(),
    })


def is_sqlite_file(path):
    if not os.path.isfile(path):
        # Skip directories, sockets, FIFOs, devices, etc. — opening a FIFO
        # for reading blocks indefinitely waiting for a writer, which would
        # hang this request (and the whole event loop) forever.
        return False
    try:
        with open(path, 'rb') as f:
            return f.read(16) == b'SQLite format 3\x00'
    except (OSError, IOError):
        return False


def list_previous_databases():
    active_database = os.path.abspath(load_env_value('DATABASE') or "")
    db_dir = os.path.dirname(active_database) or "."
    if not os.path.isdir(db_dir):
        return []
    previous = []
    for filename in os.listdir(db_dir):
        full_path = os.path.abspath(os.path.join(db_dir, filename))
        if full_path == active_database:
            continue
        if is_sqlite_file(full_path):
            previous.append(filename)
    return previous


HISTORY_PAGE_SIZE = 25

@app.get("/backup-history", response_class=HTMLResponse)
async def backup_history(request: Request, page: int = 1):
    total = count_backup_runs()
    total_pages = max(1, (total + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE)
    page = min(max(1, page), total_pages)
    runs = list_backup_runs(HISTORY_PAGE_SIZE, (page - 1) * HISTORY_PAGE_SIZE)
    return templates.TemplateResponse("backup-history.html", {
        "request": request,
        "logo": logo,
        "runs": runs,
        "page": page,
        "total_pages": total_pages,
    })


@app.get("/dashboard-stats")
async def dashboard_stats():
    # Lightweight JSON refresh for the dashboard's Backup Statistics
    # grid -- those values are otherwise only ever computed at page
    # render time, so a backup that runs in the background (scheduled,
    # or watchdog-triggered) left the numbers on an already-open
    # dashboard silently stale until a manual reload. Polled
    # periodically from index.html; deliberately excludes the CPU/RAM
    # System cards and the sidebar's watchdog/service status, which
    # have their own reasons not to need this (System is polled by the
    # existing SSE stream while a backup is active; the sidebar's
    # status is cheap enough to just come from the context processor on
    # every navigation).
    database = load_env_value('DATABASE')
    stats = get_backup_stats_context(database)
    return JSONResponse({
        "src_size": get_database_value('LAST_SRC_SIZE', 'settings') or None,
        "current_backup_size": stats["current_backup_size"],
        "dest_total": stats["dest_total"],
        "dest_used": stats["dest_used"],
        "last_session": stats["last_session"],
        "last_session_files": stats["last_session_files"],
        "snapshot_count": stats["snapshot_count"],
    })


@app.get("/recovery-history", response_class=HTMLResponse)
async def recovery_history(request: Request, page: int = 1):
    total = count_restore_runs()
    total_pages = max(1, (total + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE)
    page = min(max(1, page), total_pages)
    runs = list_restore_runs(HISTORY_PAGE_SIZE, (page - 1) * HISTORY_PAGE_SIZE)
    return templates.TemplateResponse("recovery-history.html", {
        "request": request,
        "logo": logo,
        "runs": runs,
        "page": page,
        "total_pages": total_pages,
    })


@app.get("/statistics", response_class=HTMLResponse)
async def statistics_page(request: Request):
    series = get_backup_stats_series()
    summary = get_backup_stats_summary()
    return templates.TemplateResponse("statistics.html", {
        "request": request,
        "logo": logo,
        # Serialized here (not left to the template) so the client-side
        # chart JS gets exactly the types it needs (numbers, not Jinja-
        # stringified ones) -- the whole series is small enough (capped
        # at 200 runs) to embed as one JSON blob and filter/redraw
        # entirely client-side, rather than a server round-trip per
        # filter click.
        "series_json": _json_for_script(series),
        "summary": summary,
    })


@app.get("/statistics/session-detail")
async def statistics_session_detail(session: int):
    # Drill-down target: clicking a point/bar on the Statistics page asks
    # for exactly the files changed in that one run, reusing the same
    # `changes` table the Restore/dashboard pages already draw from.
    return JSONResponse({"session": session, "changes": get_changes_by_session(session)})


@app.post("/delete-old-database/", response_class=HTMLResponse)
async def delete_old_database(request: Request, database_to_delete: str = Form(...)):
    active_database = os.path.abspath(load_env_value('DATABASE') or "")
    db_dir = os.path.dirname(active_database) or "."
    target = os.path.abspath(os.path.join(db_dir, os.path.basename(database_to_delete)))

    error = None
    success = None
    if target == active_database:
        error = "Cannot delete the currently active database."
    elif not is_sqlite_file(target):
        error = "Selected file is not a valid database."
    else:
        try:
            os.remove(target)
            success = f"Deleted {database_to_delete}."
            logger.info(f"Deleted old database: {target}")
        except OSError as e:
            error = f"Failed to delete database: {e}"
            logger.error(error)

    return templates.TemplateResponse("settings.html", {
        "request": request,
        "logo": logo,
        "error": error,
        "success": success,
        **_settings_page_context(),
    })


@app.post("/reset-active-database/", response_class=HTMLResponse)
async def reset_active_database(request: Request, confirm_name: str = Form(...)):
    active_database = os.path.abspath(load_env_value('DATABASE') or "")
    active_basename = os.path.basename(active_database)

    error = None
    success = None
    if confirm_name.strip() != active_basename:
        error = f"Type the database file name exactly ({active_basename}) to confirm."
    else:
        wipe_database_for_fresh_start(active_database)
        # A cleared database alone wouldn't actually start a fresh full
        # backup: _run_initial_full_backup_if_needed() (rsync_incremental
        # .py) decides that by checking whether Full Backup already has
        # this marker on disk, not anything in the database. Removing
        # just this one small metadata file -- not any actual backed-up
        # content -- is what makes the next backup run genuinely redo the
        # full backup instead of being treated as already-complete and
        # continuing as a normal snapshot.
        full_backup = load_other_variables('full_backup')
        marker_path = os.path.join(full_backup, COMPLETION_MARKER)
        try:
            if os.path.exists(marker_path):
                os.remove(marker_path)
        except OSError as e:
            logger.warning(f"Could not remove completion marker at {marker_path}: {e}")
        success = "Database reset. The next backup run will be a fresh full backup, using the same settings."
        logger.warning(f"Active database {active_database} reset for a fresh start by user request.")

    return templates.TemplateResponse("settings.html", {
        "request": request,
        "logo": logo,
        "error": error,
        "success": success,
        **_settings_page_context(),
    })


def open_dashboard_in_browser(port):
    try:
        webbrowser.open(f"http://127.0.0.1:{port}")
    except Exception as e:
        logger.warning(f"Could not auto-open the dashboard in a browser: {e}")

server_port = load_env_value('SERVER_PORT')
if __name__ == "__main__":
    # FOLDERW_OPEN_BROWSER: only set by setup.py's genuine first-ever
    # launch. Every other way this process starts (systemd on boot, a
    # crash-restart, update.sh restarting it to apply new code) doesn't set
    # it, so it doesn't pop a browser window -- found live, the hard way:
    # without this check, ANY restart (including every cycle of a restart
    # loop) opened a new browser window.
    if os.environ.get("FOLDERW_OPEN_BROWSER") == "1":
        # Give uvicorn a moment to bind before opening the dashboard in the browser
        threading.Timer(1.0, open_dashboard_in_browser, args=[server_port]).start()
    uvicorn.run(app, host="0.0.0.0", port=int(server_port))
