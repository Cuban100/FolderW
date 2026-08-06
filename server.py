from fastapi import FastAPI, Request, WebSocket, Form
from pydantic import BaseModel
import subprocess
import os
import sys
import threading
import queue
import webbrowser
import psutil
from statistics_operations import check_env_variables, validate_all_conditions, evaluation_of_resources, destination_space, get_folder_size_du
from db_operations import load_env_value, load_other_variables, save_env_values, create_all_tables, get_last_session_number, list_items_by_session, get_database_value, set_database_value
from restore_operations import list_backups, get_backup_path, list_files_in_backup, restore_backup, cleanup_old_snapshots
from loguru import logger
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.templating import Jinja2Templates
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

# Define the form data model
class FormData(BaseModel):
    server_port: int
    src_dir: str
    base_dir: str
    database: str
    full_name: str
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

@app.get("/check-evaluation", response_class=HTMLResponse)
async def validate_conditions(request: Request):
    logger.info("Response received from Front End for /check-evaluation")
    src_dir = load_env_value('SRC_DIR')
    base_dir = load_env_value('BASE_DIR')
    database = load_env_value('DATABASE')
    full_name = load_env_value('FULL_NAME')
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
            "database": database,
            "full_name": full_name,
            "monitor": monitor,
            "interval": backup_interval,
            "src_size": src_size,  # Human-readable format
            "dest_space": dest_space,  # Human-readable format
            "can_backup": 'Yes' if can_backup else 'No'
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
            "can_backup": 'No'
        })



@app.get("/check-settings", response_class=HTMLResponse)
async def check_settings(request: Request):
    logger.info("Response received from Front End for /check-settings")
    src_dir = load_env_value('SRC_DIR')
    base_dir = load_env_value('BASE_DIR')
    database = load_env_value('DATABASE')
    full_name = load_env_value('FULL_NAME')
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
            "database": database,
            "full_name": full_name,
            "monitor": monitor,
            "interval": backup_interval

        })
    else:
        # If there are missing variables, show which ones are missing
        return templates.TemplateResponse("index.html", {
            "request": request,
            "logo": logo,
            "missing_vars": missing_vars,
            "settings_sent": settings_sent
        })

def is_backup_running():
    for proc in psutil.process_iter(['cmdline']):
        try:
            cmdline = proc.info['cmdline'] or []
            if any('main_backup.py' in part or 'rsync_incremental.py' in part for part in cmdline):
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return False

@app.get("/backup-status")
async def backup_status_endpoint():
    return JSONResponse({"running": is_backup_running()})

@app.get("/run-all-steps", response_class=HTMLResponse)
async def run_all_steps(request: Request):
    logger.info("Response received from Front End for /run-all-steps")
    src_dir = load_env_value('SRC_DIR')
    base_dir = load_env_value('BASE_DIR')
    database = load_env_value('DATABASE')
    full_name = load_env_value('FULL_NAME')
    monitor = load_env_value('MONITOR')
    backup_interval = load_env_value('BACKUP_INTERVAL')
    settings_sent, settings, missing_vars = check_env_variables()
    if not settings_sent:
        return templates.TemplateResponse("index.html", {
            "request": request,
            "logo": logo,
            "setting": settings,
            "missing_settings": missing_vars,
            "settings_sent": settings_sent
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
            "validation_message": validation_message
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
            "dest_space": dest_space
        })

    success_message = "All settings, validations, and evaluations are correct. READY"
    
    try:
        # start_new_session detaches main_backup.py from this process's session,
        # so it survives server.py restarting or its terminal closing. stdout=PIPE
        # without ever being read risks the child blocking once the OS pipe buffer
        # fills, so redirect to a log file instead.
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
        "database": database,
        "full_name": full_name,
        "monitor": monitor,
        "interval": backup_interval,
        "backup_status": backup_status,
        "src_size": src_size,
        "dest_space": dest_space,
        "can_backup": can_backup
    })
@app.get("/check-validation", response_class=HTMLResponse)
async def validate_conditions(request: Request):
    logger.info("Response received from Front End for /check-validation")
    src_dir = load_env_value('SRC_DIR')
    base_dir = load_env_value('BASE_DIR')
    database = load_env_value('DATABASE')
    full_name = load_env_value('FULL_NAME')
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
            "database": database,
            "full_name": full_name,
            "monitor": monitor,
            "interval": backup_interval
        })
    else:
        return templates.TemplateResponse("index.html", {
            "request": request,
            "logo": logo,
            "settings": settings,
            "missing_settings": missing_vars,
            "settings_sent": settings_sent,
            "validation_status": validation_status,
            "validation_message": validation_message
        })

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    src_dir = load_env_value('SRC_DIR')
    base_dir = load_env_value('BASE_DIR')
    database = load_env_value('DATABASE')
    full_name = load_env_value('FULL_NAME')
    monitor = load_env_value('MONITOR')
    backup_interval = load_env_value('BACKUP_INTERVAL')
    # Restore the last known Settings/Validation/Evaluation check results,
    # so returning to the dashboard doesn't require re-running them.
    persisted = load_persisted_checks()
    return templates.TemplateResponse("index.html",
        { "request": request,
        "logo": logo,
        "src_dir": src_dir,
        "base_dir": base_dir,
        "database": database,
        "full_name": full_name,
        "monitor": monitor,
        "interval": backup_interval,
        "settings_sent": persisted["settings_sent"],
        "validation_status": persisted["validation_status"],
        "evaluation_status": persisted["evaluation_status"],
        "src_size": persisted["src_size"],
        "dest_space": persisted["dest_space"],
        "can_backup": persisted["can_backup"],
        "backup_in_progress": is_backup_running(),
        })

@app.get("/statistics", response_class=HTMLResponse)
async def statistics_page(request: Request):
    database = load_env_value('DATABASE')
    full_backup = load_other_variables('full_backup')

    src_size, dest_free, can_backup, _ = evaluation_of_resources()
    total, used, free = destination_space()
    current_backup_size = get_folder_size_du(full_backup)
    last_session = get_last_session_number(database)
    last_session_files = len(list_items_by_session(database))

    return templates.TemplateResponse("statistics.html", {
        "request": request,
        "src_size": src_size,
        "dest_total": f"{total:.2f} GB" if total is not None else "N/A",
        "dest_used": f"{used:.2f} GB" if used is not None else "N/A",
        "dest_free": dest_free,
        "can_backup": can_backup,
        "current_backup_size": current_backup_size or "N/A",
        "last_session": last_session,
        "last_session_files": last_session_files,
    })

@app.get("/restore", response_class=HTMLResponse)
async def restore_page(request: Request):
    return templates.TemplateResponse("restore.html", {
        "request": request,
        "backups": list_backups(),
    })

@app.get("/restore/browse/{backup_id:path}", response_class=HTMLResponse)
async def restore_browse(request: Request, backup_id: str):
    backup_path = get_backup_path(backup_id)
    if backup_path is None:
        return templates.TemplateResponse("restore.html", {
            "request": request,
            "backups": list_backups(),
            "error": f"Backup not found: {backup_id}",
        })
    files, truncated = list_files_in_backup(backup_path)
    return templates.TemplateResponse("restore_browse.html", {
        "request": request,
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
            "backups": list_backups(),
            "success": f"Restored {file_count} file(s) to {dest_root}",
        })
    except ValueError as e:
        return templates.TemplateResponse("restore.html", {
            "request": request,
            "backups": list_backups(),
            "error": str(e),
        })


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    logfile = load_other_variables('logfile')
    monitor = load_env_value('MONITOR')
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "log_dir": os.path.dirname(logfile),
        "src_dir": load_env_value('SRC_DIR'),
        "base_dir": load_env_value('BASE_DIR'),
        "database": load_env_value('DATABASE'),
        "full_name": load_env_value('FULL_NAME'),
        "monitor_checked": monitor == '1',
        "interval": load_env_value('BACKUP_INTERVAL'),
        "max_snapshots": load_env_value('MAX_SNAPSHOTS')
    })


@app.post("/submit/", response_class=HTMLResponse)
async def submit_settings(
    request: Request,
    src_dir: str = Form(""),
    base_dir: str = Form(""),
    database: str = Form(""),
    full_name: str = Form(""),
    interval: str = Form(""),
    max_snapshots: str = Form(""),
    monitor: str = Form(None)
):
    logger.info("Response received from Front End for /submit/")
    monitor_enabled = monitor is not None

    max_snapshots = max_snapshots.strip()
    if max_snapshots and (not max_snapshots.isdigit() or int(max_snapshots) <= 0):
        logfile = load_other_variables('logfile')
        return templates.TemplateResponse("settings.html", {
            "request": request,
            "error": "Snapshots to Keep must be a positive whole number, or left blank.",
            "log_dir": os.path.dirname(logfile),
            "src_dir": load_env_value('SRC_DIR'),
            "base_dir": load_env_value('BASE_DIR'),
            "database": load_env_value('DATABASE'),
            "full_name": load_env_value('FULL_NAME'),
            "monitor_checked": monitor_enabled,
            "interval": load_env_value('BACKUP_INTERVAL'),
            "max_snapshots": max_snapshots
        })

    new_values = {
        "SRC_DIR": src_dir.strip() or load_env_value('SRC_DIR'),
        "BASE_DIR": base_dir.strip() or load_env_value('BASE_DIR'),
        "DATABASE": database.strip() or load_env_value('DATABASE'),
        "FULL_NAME": full_name.strip() or load_env_value('FULL_NAME'),
        "MONITOR": "1" if monitor_enabled else "0",
        "BACKUP_INTERVAL": "False" if monitor_enabled else (interval or load_env_value('BACKUP_INTERVAL') or "hourly"),
        "MAX_SNAPSHOTS": max_snapshots
    }
    save_env_values(new_values)

    if new_values["DATABASE"]:
        create_all_tables(new_values["DATABASE"])
        # Settings changed — old check results no longer reflect reality
        clear_persisted_checks()

    logfile = load_other_variables('logfile')
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "success": "Settings saved successfully.",
        "log_dir": os.path.dirname(logfile),
        "src_dir": new_values["SRC_DIR"],
        "base_dir": new_values["BASE_DIR"],
        "database": new_values["DATABASE"],
        "full_name": new_values["FULL_NAME"],
        "monitor_checked": monitor_enabled,
        "interval": new_values["BACKUP_INTERVAL"],
        "max_snapshots": new_values["MAX_SNAPSHOTS"]
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
