import os
import shutil
import subprocess
import tempfile
from db_operations import create_all_tables, set_database_value
from auth import hash_password
import tkinter as tk
from tkinter import filedialog, Label, PhotoImage
from tkinter import ttk, StringVar
from ttkthemes import ThemedTk
from PIL import Image, ImageTk
from dotenv import load_dotenv, set_key, find_dotenv
import sys

class Logger:
    def __init__(self, text_widget):
        self.text_widget = text_widget

    def write(self, message):
        # Insert the message into the Text widget with the "neon_green" tag
        self.text_widget.insert(tk.END, message, "neon_green")
        self.text_widget.yview(tk.END)  # Auto-scroll to the end
        self.text_widget.update_idletasks()  # Force update/redraw

    def flush(self):
        pass  # No need to do anything on flush

def upgrade_packages(requirements_file='requirements.txt'):
    print(f"Installing pinned versions from {requirements_file}\n")
    process = subprocess.Popen(
        ["pip", "install", "-r", requirements_file],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    for line in process.stdout:
        print(line, end="")
    process.wait()

def toggle_backup_options():
    if monitor_var.get() == 1:
        for widget in interval_widgets:
            widget.grid_remove()
    else:
        for widget in interval_widgets:
            widget.grid()

SERVICE_NAME = "folderw.service"
BACKUP_SERVICE_NAME = "folderw-backup.service"
APP_DIR = os.path.dirname(os.path.abspath(__file__))

def configure_systemd_autostart(enable):
    service_dir = os.path.join(os.path.expanduser("~"), ".config", "systemd", "user")
    service_path = os.path.join(service_dir, SERVICE_NAME)
    backup_service_path = os.path.join(service_dir, BACKUP_SERVICE_NAME)

    if enable:
        print(f"Configuring systemd to start FolderW at boot ({service_path})")
        # Two separate units, deliberately not tied together in the same
        # cgroup: this unit only ever runs server.py (the dashboard), so a
        # routine restart (e.g. from update.sh after every code update)
        # can't touch an active backup — and folderw-backup.service (below)
        # can be stopped/restarted on its own with normal systemd semantics
        # (default KillMode=control-group), cleanly killing main_backup.py
        # and everything it spawned (rsync_event_handler.py, rsync itself)
        # with no lingering processes.
        service_content = (
            "[Unit]\n"
            "Description=FolderW Backup Dashboard\n"
            "After=network.target\n\n"
            "[Service]\n"
            "Type=simple\n"
            f"WorkingDirectory={APP_DIR}\n"
            f"ExecStart={sys.executable} {os.path.join(APP_DIR, 'server.py')}\n"
            "Restart=on-failure\n"
            "RestartSec=5\n\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )
        # Not enabled/started here — server.py starts (or restarts) it
        # on-demand via systemctl when a backup is actually triggered, the
        # same way it always launched main_backup.py directly before.
        backup_service_content = (
            "[Unit]\n"
            "Description=FolderW Backup Worker\n"
            "After=network.target\n\n"
            "[Service]\n"
            "Type=simple\n"
            f"WorkingDirectory={APP_DIR}\n"
            f"ExecStart={sys.executable} {os.path.join(APP_DIR, 'main_backup.py')}\n"
            "Restart=on-failure\n"
            "RestartSec=10\n"
            # Default (90s) meant Stop Full Backup could block for a minute
            # and a half waiting on rsync mid-transfer before systemd gave
            # up and SIGKILLed it — the dashboard's stop request blocks on
            # this same wait. 15s is enough for a normal graceful exit
            # (finish the current write, clean up its temp file) without
            # leaving a stop request hanging for so long it looks broken.
            "TimeoutStopSec=15\n"
            # rsync now runs under sudo (see rsync_incremental.py) so it can
            # read files owned by another UID -- but that means the actual
            # rsync process is root-owned, and this is a --user service:
            # this user's own systemd session has no permission to signal a
            # root-owned process at all, cgroup membership notwithstanding.
            # Found live: KillMode=control-group's own SIGKILL attempt on
            # stop failed with "Operation not permitted", the service
            # reported itself Stopped anyway, and the actual rsync process
            # kept running regardless -- exactly what "I hit stop and it's
            # still running" looks like. sudo (already NOPASSWD for rsync
            # specifically) can signal it instead. The leading "-" tells
            # systemd to ignore this command's exit code, so "nothing to
            # kill" (the common case -- rsync usually already exited
            # cleanly on its own SIGTERM) isn't logged as a failure.
            f"ExecStopPost=-{shutil.which('sudo') or '/usr/bin/sudo'} -n pkill -9 -f 'rsync -avv --sparse --delete --info=progress2'\n"
        )
        try:
            os.makedirs(service_dir, exist_ok=True)
            with open(service_path, "w") as f:
                f.write(service_content)
            with open(backup_service_path, "w") as f:
                f.write(backup_service_content)
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
            subprocess.run(["systemctl", "--user", "enable", SERVICE_NAME], check=True)
            print("Autostart enabled. FolderW will start automatically on login/boot.")
            linger = subprocess.run(
                ["loginctl", "enable-linger", os.environ.get("USER", "")],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            if linger.returncode != 0:
                print("Note: could not enable linger automatically. If FolderW doesn't start "
                      "before you log in, run: loginctl enable-linger $USER")
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            print(f"Could not configure systemd autostart: {e}")
    else:
        if os.path.exists(service_path):
            print("Disabling systemd autostart")
            try:
                subprocess.run(["systemctl", "--user", "disable", SERVICE_NAME], check=True)
            except (subprocess.CalledProcessError, FileNotFoundError) as e:
                print(f"Could not disable systemd autostart: {e}")

RSYNC_SUDOERS_DROPIN = "/etc/sudoers.d/folderw-rsync"

def configure_rsync_sudo():
    """Grants NOPASSWD sudo for rsync specifically, so a backup triggered
    without an interactive terminal (the watchdog, a scheduled interval, a
    plain systemd restart) can still read files owned by another UID --
    e.g. a Docker container writing into a bind-mounted config directory
    under its own internal user, unreadable by this account otherwise
    (found the hard way: WireGuard peer configs owned by UID 2000, 700/600
    permissions). Without this, rsync just can't read those files -- a
    permission error it handles fine on its own (logs a warning, moves on),
    but the backup silently misses that data either way.

    Skipped if some NOPASSWD rule already covers rsync (e.g. a broader
    NOPASSWD: ALL the user already has for other reasons) -- no need to add
    a redundant rule on top of one that already works. This is what makes
    the backup behave the same way for every install, rather than only
    working around permission walls on machines that happen to already
    have broad passwordless sudo configured for unrelated reasons.
    """
    rsync_path = shutil.which("rsync") or "/usr/bin/rsync"
    username = os.environ.get("SUDO_USER") or os.environ.get("USER") or os.environ.get("LOGNAME")
    manual_hint = (f"To let backups read files owned by another user, add manually: "
                    f"echo '<username> ALL=(root) NOPASSWD: {rsync_path}' | sudo tee {RSYNC_SUDOERS_DROPIN} "
                    f"&& sudo chmod 0440 {RSYNC_SUDOERS_DROPIN}")
    if not username:
        print(f"Could not determine the current username -- skipping rsync sudo setup. {manual_hint}")
        return

    # Every privileged step below is wrapped in one try/except: sudo itself
    # (or visudo, its companion binary) isn't guaranteed to exist on every
    # Linux system this might run on -- a minimal server install, a distro
    # that ships doas instead, etc. FileNotFoundError there would otherwise
    # be unhandled and crash the entire setup flow (not just this feature)
    # for anyone on such a system. Degrading to "skip this, print how to do
    # it by hand" keeps the rest of setup (systemd autostart, starting the
    # dashboard) working regardless.
    try:
        check = subprocess.run(["sudo", "-n", "-l", rsync_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if check.returncode == 0:
            print("rsync can already run passwordlessly under sudo -- nothing to configure.")
            return

        print("Configuring passwordless sudo for rsync (lets backups read files owned by "
              "another user, e.g. a Docker container) -- you may be prompted for your password.")
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile("w", delete=False, suffix=".folderw-sudoers") as tmp:
                tmp.write(f"{username} ALL=(root) NOPASSWD: {rsync_path}\n")
                tmp_path = tmp.name

            # Validate BEFORE installing -- a syntax error in a live
            # sudoers.d file can break sudo system-wide, so this never
            # touches the real location without first confirming the
            # grammar is valid. Doesn't need root: just parses the given
            # file, doesn't touch system state.
            validate = subprocess.run(["visudo", "-c", "-f", tmp_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if validate.returncode != 0:
                print(f"Generated sudoers rule failed validation, skipping: {validate.stderr}")
                return

            # Plain sudo (not pkexec): setup.py's only documented entry
            # point is `./install.sh` -> `python3 setup.py`, always run
            # from an interactive terminal that sudo can prompt on
            # directly -- no need for polkit/pkexec, which isn't installed
            # on every system (especially headless/minimal ones) and would
            # just be an extra way for this to fail. root:root 0440 is
            # sudoers.d's required ownership/mode; sudo refuses to even
            # read a drop-in file with any other group/other write access,
            # as its own protection against tampering.
            install = subprocess.run(
                ["sudo", "install", "-m", "0440", "-o", "root", "-g", "root", tmp_path, RSYNC_SUDOERS_DROPIN],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            if install.returncode != 0:
                print(f"Could not configure sudo for rsync: {install.stderr.strip()}. {manual_hint}")
                return
            print("Configured passwordless sudo for rsync.")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
    except FileNotFoundError as e:
        print(f"sudo/visudo not found ({e}) -- skipping rsync sudo setup. Backups will still "
              f"work, just skipping any file they don't have permission to read. {manual_hint}")

def browse_directory(entry):
    directory = filedialog.askdirectory()
    entry.delete(0, tk.END)
    entry.insert(0, directory)

def browse_file(entry):
    file_path = filedialog.askopenfilename()
    entry.delete(0, tk.END)
    entry.insert(0, file_path)

def save_paths():
    env_path = find_dotenv()
    if not env_path:
        env_path = '.env'

    paths = {
        'SRC_DIR': src_dir_entry.get(),
        'BASE_DIR': base_dir_entry.get(),
        'DATABASE': database_entry.get(),
        'FULL_NAME': full_folder_name_entry.get(),
        'SERVER_PORT': server_port_entry.get(),
        'MONITOR': str(monitor_var.get()),
        'AUTOSTART': str(autostart_var.get()),
    }


    # Set BACKUP_INTERVAL based on the MONITOR value
    if monitor_var.get() == 1:
        paths['BACKUP_INTERVAL'] = 'False'
    else:
        paths['BACKUP_INTERVAL'] = interval_var.get()

    # Validate if all fields are filled
    for key, value in paths.items():
        if not value or "Browse to select" in value:
            result_label.config(text=f"Error: {key.replace('_', ' ')} is required.", foreground='#FF0000')  # Set to red
            return

    # Snapshots to keep is optional — blank means "keep everything" — so it's
    # validated separately rather than through the required-fields loop above.
    max_snapshots = max_snapshots_entry.get().strip()
    if "Leave blank" in max_snapshots:
        max_snapshots = ""
    if max_snapshots and (not max_snapshots.isdigit() or int(max_snapshots) <= 0):
        result_label.config(text="Error: Snapshots to Keep must be a positive whole number, or left blank.", foreground='#FF0000')
        return

    # Save values to .env file
    for key, value in paths.items():
        set_key(env_path, key.upper(), value)
    set_key(env_path, 'MAX_SNAPSHOTS', max_snapshots)

    # Blank means "leave unchanged" (existing install) or "no login"
    # (fresh install) — either way, never touch ADMIN_PASSWORD_HASH.
    dashboard_password = dashboard_password_entry.get().strip()
    if dashboard_password:
        set_key(env_path, 'ADMIN_PASSWORD_HASH', hash_password(dashboard_password))

    notify_urls = notify_urls_entry.get().strip()
    if "e.g." in notify_urls:  # untouched placeholder text
        notify_urls = ""
    set_key(env_path, 'NOTIFY_URLS', notify_urls)

    result_label.config(text="Configuration saved to .env", foreground='#39FF14')  # Set to neon green

    create_all_tables(paths['DATABASE'])
    # Config saved/changed here — clear any previously persisted check
    # results (including src/dest sizes from a prior check) so the
    # dashboard doesn't show stale Settings/Validation/Evaluation results
    # from before this save. create_all_tables() only ever CREATEs TABLE
    # IF NOT EXISTS, so a reused database file's old rows would otherwise
    # survive into what looks like a "fresh" setup.
    for check_key in ('SETTINGS_CHECK_PASSED', 'VALIDATION_CHECK_PASSED', 'EVALUATION_CHECK_PASSED'):
        set_database_value(check_key, '0')
    for stale_key in ('LAST_SRC_SIZE', 'LAST_DEST_SPACE'):
        set_database_value(stale_key, '')

    show_terminal()

    upgrade_packages()

    configure_systemd_autostart(autostart_var.get() == 1)
    configure_rsync_sudo()

    # start_new_session detaches server.py from this terminal's session, so
    # closing the terminal (which sends SIGHUP) doesn't kill the dashboard.
    # stdout/stderr are redirected to a log file rather than inherited from
    # the terminal, since writes to a closed terminal's tty would fail once
    # it's gone.
    log_dir = os.path.join(APP_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    server_log = open(os.path.join(log_dir, "server.log"), "a")
    # FOLDERW_OPEN_BROWSER=1: tells server.py this is the genuine first-ever
    # launch, worth popping a browser window for. Every OTHER way server.py
    # starts (systemd on boot, a crash-restart, update.sh restarting it to
    # apply new code) doesn't set this -- found live, the hard way: without
    # this distinction, server.py opened a browser window on every single
    # restart, and during any restart loop (a crash loop, or update.sh
    # hitting one) that meant a new browser window every few seconds.
    subprocess.Popen(
        [sys.executable, os.path.join(APP_DIR, "server.py")],
        stdout=server_log,
        stderr=server_log,
        start_new_session=True,
        env={**os.environ, "FOLDERW_OPEN_BROWSER": "1"},
    )
    root.destroy()

     
def show_terminal():
    log_text = tk.Text(root, height=10, width=50, bg='#2d2d2d', font=('Courier', 10))
    log_text.tag_configure("neon_green", foreground="#39FF14") 
    log_text.config(state=tk.NORMAL)
    sys.stdout = Logger(log_text)
    log_text.place(x=180, y=480, width=400, height=150)
    log_text.config(state=tk.NORMAL)
    new_height = 660
    new_width = 700
    root.geometry(f"{new_width}x{new_height}")


# Function to set placeholders in Entry fields, if corresponding .env value is empty
def set_placeholder(entry, placeholder, env_key):
    """
    This function will set the placeholder text on the Entry widget only if the corresponding value 
    is not present in the .env file. It will also handle focus-in and focus-out events.
    """
    # Check if the value exists in the .env file
    env_value = os.getenv(env_key)

    # If the .env value is empty, set the placeholder
    if not env_value:
        def on_focus_in(event):
            """Clear placeholder text when the field is focused."""
            if entry.get() == placeholder:
                entry.delete(0, tk.END)
                entry.config(foreground='black')  # Reset the text color to black when user starts typing

        def on_focus_out(event):
            """Restore placeholder text if the field is empty."""
            if entry.get() == "":
                entry.insert(0, placeholder)
                entry.config(foreground='grey')  # Set the text color to grey for placeholder text

        # Insert placeholder text and set the text color to grey
        entry.insert(0, placeholder)
        entry.config(foreground='grey')  # Placeholder text should be grey

        # Bind the focus-in and focus-out events
        entry.bind("<FocusIn>", on_focus_in)
        entry.bind("<FocusOut>", on_focus_out)
    else:
        # If the .env value is present, insert it directly into the entry field
        entry.insert(0, env_value)
        entry.config(foreground='black')  # Set text color to black if there's a value

# Load existing .env values if available
load_dotenv()

# Create the Tkinter window with a dark theme
root = ThemedTk(theme="black")
root.title("Backup Configuration")

# Function to center the window
def center_window(window):
    window.update_idletasks()
    screen_width = window.winfo_screenwidth()
    screen_height = window.winfo_screenheight()
    size = tuple(int(_) for _ in window.geometry().split('+')[0].split('x'))
    x = screen_width // 2 - size[0] // 2
    y = screen_height // 2 - size[1] // 2
    window.geometry(f"{size[0]}x{size[1]}+{x}+{y}")

# Create and place widgets with placeholders
labels = [
    ("Source Directory:", "SRC_DIR"),
    ("Base Backup Directory:", "BASE_DIR"),
    ("Database:", "DATABASE"),
    ("Full Folder Name:", "FULL_NAME"),
    ("Server Port:", "SERVER_PORT"),  # Add server port label
]
entries = []
placeholders = {
    "Source Directory:": "Select the Source Folder to be Backed up",
    "Base Backup Directory:": "Destination directory to place the backup",
    "Database:": "Enter the database file name, Example: backup.db",
    "Full Folder Name:": "Example: Documents — contains Full Backup/ and Snapshots/ inside it",
    "Server Port:": "Enter the server port, Example: 8000"
}

# Create and place widgets
for i, (label, env_key) in enumerate(labels):
    tk.Label(root, text=label, foreground='#ffffff', background='#1a1a1a').grid(row=i, column=0, padx=10, pady=10, sticky='e')
    entry = ttk.Entry(root, width=50)
    entry.grid(row=i, column=1, padx=10, pady=10)
    entries.append(entry)
    
    # Set placeholder text
    placeholder = placeholders.get(label, "")
    set_placeholder(entry, placeholder, env_key)  # Pass the corresponding env key
    
    if "Directory" in label or "Backup" in label:
        button = ttk.Button(root, text="Browse...", command=lambda e=entry: browse_directory(e))
        button.grid(row=i, column=2, padx=10, pady=10)
    elif "File" in label:
        button = ttk.Button(root, text="Browse...", command=lambda e=entry: browse_file(e))
        button.grid(row=i, column=2, padx=10, pady=10)

src_dir_entry, base_dir_entry, database_entry, full_folder_name_entry, server_port_entry = entries  # Include server port entry

# Monitor checkbox setup
monitor_var = tk.IntVar(value=1)  
monitor_checkbox = tk.Checkbutton(root, text="Monitor source folder for automated backups on changes", variable=monitor_var, background='#1a1a1a', foreground='#ffffff', selectcolor='#2ecc71', command=toggle_backup_options)
monitor_checkbox.grid(row=len(labels), column=1, padx=10, pady=10, sticky='w')  # Aligned with other fields

# Short hint: monitoring and scheduled backups are mutually exclusive
monitor_hint = tk.Label(root, text="Backs up as soon as a file changes.", font=('TkDefaultFont', 8), foreground='#888888', bg='#1a1a1a')
monitor_hint.grid(row=len(labels), column=2, padx=10, pady=10, sticky='w')

# Create the label next to the checkbox
monitor_label = tk.Label(root, text="Watchdog Future:", foreground='#ffffff', bg='#1a1a1a', padx=10, pady=10)
monitor_label.grid(row=len(labels), column=0, padx=0, pady=10, sticky='e')  # Place it in the same row as the checkbox, to the left (column 0)

# Dynamic Backup Interval Options
interval_var = StringVar(value='hourly')
interval_label = tk.Label(root, text="Backup Interval:", foreground='#ffffff', bg='#1a1a1a')
interval_dropdown = ttk.Combobox(root, textvariable=interval_var, values=["hourly", "half-day", "daily", "weekly"])
interval_hint = tk.Label(root, text="Backs up on this fixed schedule instead.", font=('TkDefaultFont', 8), foreground='#888888', bg='#1a1a1a')
interval_widgets = [interval_label, interval_dropdown, interval_hint]

interval_label.grid(row=len(labels) + 1, column=0, padx=10, pady=10, sticky='e')
interval_dropdown.grid(row=len(labels) + 1, column=1, padx=10, pady=10)
interval_hint.grid(row=len(labels) + 1, column=2, padx=10, pady=10, sticky='w')

# Autostart on boot checkbox setup
autostart_var = tk.IntVar(value=0)
autostart_checkbox = tk.Checkbutton(root, text="Start FolderW automatically at system boot", variable=autostart_var, background='#1a1a1a', foreground='#ffffff', selectcolor='#2ecc71')
autostart_checkbox.grid(row=len(labels) + 2, column=1, padx=10, pady=10, sticky='w')

autostart_hint = tk.Label(root, text="Installs a systemd user service that runs the dashboard on login.", font=('TkDefaultFont', 8), foreground='#888888', bg='#1a1a1a')
autostart_hint.grid(row=len(labels) + 2, column=2, padx=10, pady=10, sticky='w')

autostart_label = tk.Label(root, text="Autostart:", foreground='#ffffff', bg='#1a1a1a', padx=10, pady=10)
autostart_label.grid(row=len(labels) + 2, column=0, padx=0, pady=10, sticky='e')

# Snapshot retention: optional, blank means keep every snapshot forever
max_snapshots_label = tk.Label(root, text="Snapshots to Keep:", foreground='#ffffff', bg='#1a1a1a', padx=10, pady=10)
max_snapshots_label.grid(row=len(labels) + 3, column=0, padx=0, pady=10, sticky='e')

max_snapshots_entry = ttk.Entry(root, width=50)
max_snapshots_entry.grid(row=len(labels) + 3, column=1, padx=10, pady=10)
set_placeholder(max_snapshots_entry, "Leave blank to keep all snapshots", 'MAX_SNAPSHOTS')

max_snapshots_hint = tk.Label(root, text="Oldest incremental snapshots beyond this count are deleted automatically.", font=('TkDefaultFont', 8), foreground='#888888', bg='#1a1a1a')
max_snapshots_hint.grid(row=len(labels) + 3, column=2, padx=10, pady=10, sticky='w')

# Dashboard password: optional, never prefilled (only the hash is ever
# stored) — leave blank on a fresh install for no login, or on an existing
# one to keep whatever password is already set.
dashboard_password_label = tk.Label(root, text="Dashboard Password:", foreground='#ffffff', bg='#1a1a1a', padx=10, pady=10)
dashboard_password_label.grid(row=len(labels) + 4, column=0, padx=0, pady=10, sticky='e')

dashboard_password_entry = ttk.Entry(root, width=50, show='*')
dashboard_password_entry.grid(row=len(labels) + 4, column=1, padx=10, pady=10)

dashboard_password_hint = tk.Label(root, text="Leave blank for no login, or to keep the current password unchanged.", font=('TkDefaultFont', 8), foreground='#888888', bg='#1a1a1a')
dashboard_password_hint.grid(row=len(labels) + 4, column=2, padx=10, pady=10, sticky='w')

# Notification URL(s): optional, comma-separated Apprise URLs
notify_urls_label = tk.Label(root, text="Notification URL(s):", foreground='#ffffff', bg='#1a1a1a', padx=10, pady=10)
notify_urls_label.grid(row=len(labels) + 5, column=0, padx=0, pady=10, sticky='e')

notify_urls_entry = ttk.Entry(root, width=50)
notify_urls_entry.grid(row=len(labels) + 5, column=1, padx=10, pady=10)
set_placeholder(notify_urls_entry, "e.g. pover://user@token, ntfy://topic", 'NOTIFY_URLS')

notify_urls_hint = tk.Label(root, text="Comma-separated Apprise URLs, notified on backup failure/completion. Leave blank to disable.", font=('TkDefaultFont', 8), foreground='#888888', bg='#1a1a1a')
notify_urls_hint.grid(row=len(labels) + 5, column=2, padx=10, pady=10, sticky='w')

save_button = ttk.Button(root, text="Save Configuration", command=save_paths)
save_button.grid(row=len(labels) + 6, column=1, pady=25)

result_label = ttk.Label(root, text="", background='#1a1a1a', foreground='#ffffff')
result_label.grid(row=len(labels) + 7, column=1, pady=25)

# Safely set the monitor variable
monitor_value = os.getenv('MONITOR')
if monitor_value is not None:
    monitor_var.set(int(monitor_value))
else:
    monitor_var.set(0)  # Default to 0 if MONITOR is not set

# Safely set the autostart variable
autostart_value = os.getenv('AUTOSTART')
autostart_var.set(int(autostart_value)) if autostart_value is not None else autostart_var.set(0)

# Sync the interval widgets' visibility with the loaded monitor value
toggle_backup_options()

# Configure the root window background color
root.configure(bg='#1a1a1a')

# Center the window
center_window(root)


root.mainloop()