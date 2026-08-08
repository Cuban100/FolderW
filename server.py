from fastapi import FastAPI, Request, WebSocket, Form
from pydantic import BaseModel
import subprocess
import os
import sys
import time
import threading
import queue
import webbrowser
import psutil
import apprise
from statistics_operations import check_env_variables, validate_all_conditions, evaluation_of_resources, destination_space
from db_operations import load_env_value, load_other_variables, save_env_values, create_all_tables, get_last_session_number, list_items_by_session, get_database_value, set_database_value, reset_backup_history, has_completed_backup
from restore_operations import list_backups, get_backup_path, list_files_in_backup, restore_backup, cleanup_old_snapshots
from auth import hash_password, verify_password, get_or_create_secret_key
from loguru import logger
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
import uvicorn

# Initialize FastAPI app
app = FastAPI()

# Resolve relative to this file, not the process's working directory, so
# templates/static still load correctly no matter where server.py is launched from
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Initialize Jinja2 templates
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
logo = '/static/logo.png'
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

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

@app.get("/check-evaluation", response_class=HTMLResponse)
async def validate_conditions(request: Request):
    logger.info("Response received from Front End for /check-evaluation")
    src_dir = load_env_value('SRC_DIR')
    base_dir = load_env_value('BASE_DIR')
    full_name = load_env_value('FULL_NAME')
    database = load_env_value('DATABASE')
    monitor = load_env_value('MONITOR')
    backup_interval = load_env_value('BACKUP_INTERVAL')
    validation_status, validation_message = validate_all_conditions(src_dir, base_dir)
    settings_sent, settings, missing_vars = check_env_variables()
    src_size, dest_space, can_backup, evaluation_message = evaluation_of_resources()

    status.update({
        "src_size": src_size,
        "dest_space": dest_space,
        "can_backup": can_backup
    })
    persist_check_results(
        settings_sent=settings_sent,
        validation_status=validation_status,
        evaluation_status=can_backup,
        LAST_SRC_SIZE=src_size,
        LAST_DEST_SPACE=dest_space,
    )

    if can_backup:
        success_message = "All settings, validations, and evaluations are correct. READY"
        return templates.TemplateResponse("index.html", {
            "request": request,
            "success_message": success_message,
            "settings_sent": settings_sent,
            "validation_status": validation_status,
            "validation_message": validation_message,
            "evaluation_status": can_backup,
            "evaluation_message": evaluation_message,
            "logo": logo,
            "settings": settings,
            "src_dir": src_dir,
            "base_dir": base_dir,
            "full_name": full_name,
            "database": database,
            "monitor": monitor,
            "interval": backup_interval,
            "src_size": src_size,  # Human-readable format
            "dest_space": dest_space,  # Human-readable format
            "can_backup": 'Yes' if can_backup else 'No',
            "has_completed_backup": has_completed_backup(database),
            **get_backup_stats_context(database),
        })
    else:
        return templates.TemplateResponse("index.html", {
            "request": request,
            "logo": logo,
            "missing_settings": missing_vars,
            "settings_sent": settings_sent,
            "validation_status": validation_status,
            "validation_message": validation_message,
            "evaluation_status": can_backup,
            "evaluation_message": evaluation_message,
            "src_size": src_size,  # Human-readable format
            "dest_space": dest_space,  # Human-readable format
            "can_backup": 'No',
            "has_completed_backup": has_completed_backup(database),
            **get_backup_stats_context(database),
        })



@app.get("/check-settings", response_class=HTMLResponse)
async def check_settings(request: Request):
    logger.info("Response received from Front End for /check-settings")
    src_dir = load_env_value('SRC_DIR')
    base_dir = load_env_value('BASE_DIR')
    full_name = load_env_value('FULL_NAME')
    database = load_env_value('DATABASE')
    monitor = load_env_value('MONITOR')
    backup_interval = load_env_value('BACKUP_INTERVAL')
    settings_sent, settings, missing_vars = check_env_variables()
    logger.info(f"settings_send: {settings_sent}, missing_vars: {missing_vars}")
    persist_check_results(settings_sent=settings_sent)
    # If settings_sent is True, all variables are set correctly
    if settings_sent == True:
        success_message = "All settings are present."
        return templates.TemplateResponse("index.html", {
            "request": request,
            "logo": logo,
            "settings": settings,
            "success_message": success_message,
            "settings_sent": settings_sent,
            "src_dir": src_dir,
            "base_dir": base_dir,
            "full_name": full_name,
            "database": database,
            "monitor": monitor,
            "interval": backup_interval,
            "has_completed_backup": has_completed_backup(database),
            **get_backup_stats_context(database),

        })
    else:
        # If there are missing variables, show which ones are missing
        return templates.TemplateResponse("index.html", {
            "request": request,
            "logo": logo,
            "missing_vars": missing_vars,
            "settings_sent": settings_sent,
            **get_backup_stats_context(database),
        })

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
    if get_database_value('BACKUP_PREPARING', 'settings') == '1':
        return True
    for proc in psutil.process_iter(['cmdline']):
        try:
            cmdline = proc.info['cmdline'] or []
            if any('rsync_incremental.py' in part for part in cmdline):
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return False

@app.get("/backup-status")
async def backup_status_endpoint():
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
    return JSONResponse({
        "running": is_backup_running(),
        "percent": percent or None,
        "eta": eta or None,
        "elapsed_seconds": elapsed_seconds,
        "current_backup_size": get_database_value('CURRENT_BACKUP_SIZE', 'settings') or None,
        "src_size": get_database_value('LAST_SRC_SIZE', 'settings') or None,
        "rsync_tail": rsync_tail,
        "watchdog_active": is_watchdog_active(),
    })

@app.post("/stop-backup")
async def stop_backup():
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

@app.get("/run-all-steps", response_class=HTMLResponse)
async def run_all_steps(request: Request):
    logger.info("Response received from Front End for /run-all-steps")
    src_dir = load_env_value('SRC_DIR')
    base_dir = load_env_value('BASE_DIR')
    full_name = load_env_value('FULL_NAME')
    database = load_env_value('DATABASE')
    monitor = load_env_value('MONITOR')
    backup_interval = load_env_value('BACKUP_INTERVAL')
    settings_sent, settings, missing_vars = check_env_variables()
    if not settings_sent:
        return templates.TemplateResponse("index.html", {
            "request": request,
            "logo": logo,
            "setting": settings,
            "missing_settings": missing_vars,
            "settings_sent": settings_sent,
            **get_backup_stats_context(database),
        })

    validation_status, validation_message = validate_all_conditions(src_dir, base_dir)
    if not validation_status:
        return templates.TemplateResponse("index.html", {
            "request": request,
            "logo": logo,
            "settings": settings,
            "missing_settings": missing_vars,
            "settings_sent": settings_sent,
            "validation_status": validation_status,
            "validation_message": validation_message,
            **get_backup_stats_context(database),
        })

    src_size, dest_space, can_backup, evaluation_message = evaluation_of_resources()
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
            "validation_status": validation_status,
            "validation_message": validation_message,
            "evaluation_status": can_backup,
            "evaluation_message": evaluation_message,
            "src_size": src_size,
            "dest_space": dest_space,
            **get_backup_stats_context(database),
        })

    success_message = "All settings, validations, and evaluations are correct. READY"
    
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
        "validation_status": validation_status,
        "validation_message": validation_message,
        "evaluation_status": can_backup,
        "evaluation_message": "",
        "src_dir": src_dir,
        "base_dir": base_dir,
        "full_name": full_name,
        "database": database,
        "monitor": monitor,
        "interval": backup_interval,
        "backup_status": backup_status,
        "src_size": src_size,
        "dest_space": dest_space,
        "can_backup": can_backup,
        "has_completed_backup": has_completed_backup(database),
        **get_backup_stats_context(database),
    })
@app.get("/check-validation", response_class=HTMLResponse)
async def validate_conditions(request: Request):
    logger.info("Response received from Front End for /check-validation")
    src_dir = load_env_value('SRC_DIR')
    base_dir = load_env_value('BASE_DIR')
    full_name = load_env_value('FULL_NAME')
    database = load_env_value('DATABASE')
    monitor = load_env_value('MONITOR')
    backup_interval = load_env_value('BACKUP_INTERVAL')
    settings_sent, settings, missing_vars = check_env_variables()

    validation_status, validation_message = validate_all_conditions(src_dir, base_dir)
    persist_check_results(settings_sent=settings_sent, validation_status=validation_status)

    if validation_status:
        success_message = "All settings and Validations are correct."
        return templates.TemplateResponse("index.html", {
            "request": request,
            "logo": logo,
            "settings": settings,
            "success_message": success_message,
            "settings_sent": settings_sent,
            "validation_status": validation_status,
            "validation_message": validation_message,
            "src_dir": src_dir,
            "base_dir": base_dir,
            "full_name": full_name,
            "database": database,
            "monitor": monitor,
            "interval": backup_interval,
            "has_completed_backup": has_completed_backup(database),
            **get_backup_stats_context(database),
        })
    else:
        return templates.TemplateResponse("index.html", {
            "request": request,
            "logo": logo,
            "settings": settings,
            "missing_settings": missing_vars,
            "settings_sent": settings_sent,
            "validation_status": validation_status,
            "validation_message": validation_message,
            **get_backup_stats_context(database),
        })

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    src_dir = load_env_value('SRC_DIR')
    base_dir = load_env_value('BASE_DIR')
    full_name = load_env_value('FULL_NAME')
    database = load_env_value('DATABASE')
    monitor = load_env_value('MONITOR')
    backup_interval = load_env_value('BACKUP_INTERVAL')
    # Restore the last known Validation/Evaluation check results, so
    # returning to the dashboard doesn't require re-running them — those
    # involve real filesystem/disk-usage checks, worth caching. Settings is
    # different: it's just reading already-loaded env vars (cheap, no I/O),
    # and a persisted flag defaults to false on a brand-new database with
    # nothing ever checked yet — showing "Missing settings" on a fresh
    # install's first load even with a fully filled-in .env. Always fresh.
    settings_sent, _settings, missing_vars = check_env_variables()
    persisted = load_persisted_checks()
    return templates.TemplateResponse("index.html",
        { "request": request,
        "logo": logo,
        "src_dir": src_dir,
        "base_dir": base_dir,
        "full_name": full_name,
        "database": database,
        "monitor": monitor,
        "interval": backup_interval,
        "settings_sent": settings_sent,
        "missing_settings": missing_vars,
        "validation_status": persisted["validation_status"],
        "evaluation_status": persisted["evaluation_status"],
        "src_size": persisted["src_size"],
        "dest_space": persisted["dest_space"],
        "can_backup": persisted["can_backup"],
        "backup_in_progress": is_backup_running(),
        "has_completed_backup": has_completed_backup(database),
        **get_backup_stats_context(database),
        })

@app.get("/restore", response_class=HTMLResponse)
async def restore_page(request: Request):
    return templates.TemplateResponse("restore.html", {
        "request": request,
        "logo": logo,
        "backups": list_backups(),
    })

@app.get("/restore/browse/{backup_id:path}", response_class=HTMLResponse)
async def restore_browse(request: Request, backup_id: str):
    backup_path = get_backup_path(backup_id)
    if backup_path is None:
        return templates.TemplateResponse("restore.html", {
            "request": request,
            "logo": logo,
            "backups": list_backups(),
            "error": f"Backup not found: {backup_id}",
        })
    files, truncated = list_files_in_backup(backup_path)
    return templates.TemplateResponse("restore_browse.html", {
        "request": request,
        "logo": logo,
        "backup_id": backup_id,
        "files": files,
        "truncated": truncated,
    })

@app.post("/restore/execute", response_class=HTMLResponse)
async def restore_execute(request: Request, backup_id: str = Form(...), selected_paths: list[str] = Form(default=[])):
    try:
        dest_root, file_count = restore_backup(backup_id, selected_paths or None)
        return templates.TemplateResponse("restore.html", {
            "request": request,
            "logo": logo,
            "backups": list_backups(),
            "success": f"Restored {file_count} file(s) to {dest_root}",
        })
    except ValueError as e:
        return templates.TemplateResponse("restore.html", {
            "request": request,
            "logo": logo,
            "backups": list_backups(),
            "error": str(e),
        })


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    logfile = load_other_variables('logfile')
    monitor = load_env_value('MONITOR')
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "logo": logo,
        "log_dir": os.path.dirname(logfile),
        "src_dir": load_env_value('SRC_DIR'),
        "base_dir": load_env_value('BASE_DIR'),
        "full_name": load_env_value('FULL_NAME'),
        "database": load_env_value('DATABASE'),
        "monitor_checked": monitor == '1',
        "interval": load_env_value('BACKUP_INTERVAL'),
        "max_snapshots": load_env_value('MAX_SNAPSHOTS'),
        "login_enabled": bool(load_env_value('ADMIN_PASSWORD_HASH')),
        "notify_urls": load_env_value('NOTIFY_URLS'),
    })


@app.post("/test-notification")
async def test_notification(notify_urls: str = Form("")):
    urls = [u.strip() for u in notify_urls.split(',') if u.strip()]
    if not urls:
        return JSONResponse({"success": False, "message": "No notification URL(s) provided."})

    apobj = apprise.Apprise()
    valid_count = sum(1 for url in urls if apobj.add(url))
    if valid_count == 0:
        return JSONResponse({"success": False, "message": "No valid URL(s) — check the format."})

    try:
        result = apobj.notify(title="FolderW Test Notification", body="If you're seeing this, your notification setup works.")
    except Exception as e:
        logger.error(f"Test notification failed: {e}")
        return JSONResponse({"success": False, "message": f"Error: {e}"})

    if result:
        return JSONResponse({"success": True, "message": "Sent! Check your device."})
    return JSONResponse({"success": False, "message": "Apprise reported failure — check the URL and service status."})


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
):
    logger.info("Response received from Front End for /submit/")
    monitor_enabled = monitor is not None

    # Captured before save_env_values overwrites .env, so we can tell
    # afterward whether the backup identity itself changed (as opposed to,
    # say, just the snapshot retention count or interval).
    old_identity = (
        load_env_value('SRC_DIR'),
        load_env_value('BASE_DIR'),
        load_env_value('FULL_NAME'),
    )

    max_snapshots = max_snapshots.strip()
    if max_snapshots and (not max_snapshots.isdigit() or int(max_snapshots) <= 0):
        logfile = load_other_variables('logfile')
        return templates.TemplateResponse("settings.html", {
            "request": request,
            "logo": logo,
            "error": "Snapshots to Keep must be a positive whole number, or left blank.",
            "log_dir": os.path.dirname(logfile),
            "src_dir": load_env_value('SRC_DIR'),
            "base_dir": load_env_value('BASE_DIR'),
            "full_name": load_env_value('FULL_NAME'),
            "database": load_env_value('DATABASE'),
            "monitor_checked": monitor_enabled,
            "interval": load_env_value('BACKUP_INTERVAL'),
            "max_snapshots": max_snapshots,
            "login_enabled": bool(load_env_value('ADMIN_PASSWORD_HASH')),
            "notify_urls": load_env_value('NOTIFY_URLS'),
        })

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
    }
    save_env_values(new_values)

    if new_values["DATABASE"]:
        create_all_tables(new_values["DATABASE"])
        # Settings changed — old check results no longer reflect reality
        clear_persisted_checks()

        new_identity = (new_values["SRC_DIR"], new_values["BASE_DIR"], new_values["FULL_NAME"])
        if new_identity != old_identity:
            # Pointed FolderW at a different backup (source, destination, or
            # container folder) — old session/change/statistics history
            # belongs to the previous backup and would otherwise corrupt
            # session numbering and historical stats for the new one.
            reset_backup_history(new_values["DATABASE"])

    logfile = load_other_variables('logfile')
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "logo": logo,
        "success": "Settings saved successfully.",
        "log_dir": os.path.dirname(logfile),
        "src_dir": new_values["SRC_DIR"],
        "base_dir": new_values["BASE_DIR"],
        "full_name": new_values["FULL_NAME"],
        "database": new_values["DATABASE"],
        "monitor_checked": monitor_enabled,
        "interval": new_values["BACKUP_INTERVAL"],
        "max_snapshots": new_values["MAX_SNAPSHOTS"],
        "login_enabled": bool(new_values["ADMIN_PASSWORD_HASH"]),
        "notify_urls": new_values["NOTIFY_URLS"],
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


@app.get("/backup-history", response_class=HTMLResponse)
async def backup_history(request: Request):
    return templates.TemplateResponse("backup-history.html", {
        "request": request,
        "logo": logo,
        "previous_databases": list_previous_databases()
    })


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

    return templates.TemplateResponse("backup-history.html", {
        "request": request,
        "logo": logo,
        "previous_databases": list_previous_databases(),
        "error": error,
        "success": success
    })


rsync_progress_queue = queue.Queue()
rsync_running = False
rsync_lock = threading.Lock()


def _run_rsync_job():
    global rsync_running
    from rsync_incremental import rsync, parse_logfile, copy_files, record_backup_statistics
    from db_operations import store_changes_in_db
    try:
        for line in rsync():
            rsync_progress_queue.put(line)
        rsync_txt = load_other_variables('rsync_txt')
        changes = parse_logfile(rsync_txt)
        store_changes_in_db(changes)
        last_session_number, incremental_folder = copy_files()
        record_backup_statistics(changes, last_session_number, incremental_folder)
        cleanup_old_snapshots(load_env_value('MAX_SNAPSHOTS'))
        rsync_progress_queue.put("DONE: 100% - Backup complete")
    except Exception as e:
        logger.error(f"Error running backup job: {e}")
        rsync_progress_queue.put(f"ERROR: {e}")
    finally:
        rsync_running = False


@app.get("/progress", response_class=HTMLResponse)
async def progress_page(request: Request):
    return templates.TemplateResponse("progess.html", {"request": request})


@app.get("/start_rsync")
async def start_rsync():
    global rsync_running
    with rsync_lock:
        if rsync_running:
            return JSONResponse({"message": "Backup already running."})
        rsync_running = True
        threading.Thread(target=_run_rsync_job, daemon=True).start()
    return JSONResponse({"message": "Backup started."})


@app.get("/rsync_progress")
async def rsync_progress():
    def event_stream():
        while True:
            line = rsync_progress_queue.get()
            yield f"data: {line}\n\n"
            if line.startswith("DONE") or line.startswith("ERROR"):
                break
    return StreamingResponse(event_stream(), media_type="text/event-stream")


def open_dashboard_in_browser(port):
    try:
        webbrowser.open(f"http://127.0.0.1:{port}")
    except Exception as e:
        logger.warning(f"Could not auto-open the dashboard in a browser: {e}")

server_port = load_env_value('SERVER_PORT')
if __name__ == "__main__":
    # Give uvicorn a moment to bind before opening the dashboard in the browser
    threading.Timer(1.0, open_dashboard_in_browser, args=[server_port]).start()
    uvicorn.run(app, host="0.0.0.0", port=int(server_port))
