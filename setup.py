import os
import subprocess
from db_operations import create_all_tables, set_database_value
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
APP_DIR = os.path.dirname(os.path.abspath(__file__))

def configure_systemd_autostart(enable):
    service_dir = os.path.join(os.path.expanduser("~"), ".config", "systemd", "user")
    service_path = os.path.join(service_dir, SERVICE_NAME)

    if enable:
        print(f"Configuring systemd to start FolderW at boot ({service_path})")
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
        try:
            os.makedirs(service_dir, exist_ok=True)
            with open(service_path, "w") as f:
                f.write(service_content)
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

    result_label.config(text="Configuration saved to .env", foreground='#39FF14')  # Set to neon green

    create_all_tables(paths['DATABASE'])
    # Config saved/changed here — clear any previously persisted check
    # results so the dashboard doesn't show stale Settings/Validation/
    # Evaluation results from before this save.
    for check_key in ('SETTINGS_CHECK_PASSED', 'VALIDATION_CHECK_PASSED', 'EVALUATION_CHECK_PASSED'):
        set_database_value(check_key, '0')

    show_terminal()

    upgrade_packages()

    configure_systemd_autostart(autostart_var.get() == 1)

    # start_new_session detaches server.py from this terminal's session, so
    # closing the terminal (which sends SIGHUP) doesn't kill the dashboard.
    # stdout/stderr are redirected to a log file rather than inherited from
    # the terminal, since writes to a closed terminal's tty would fail once
    # it's gone.
    log_dir = os.path.join(APP_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    server_log = open(os.path.join(log_dir, "server.log"), "a")
    subprocess.Popen(
        [sys.executable, os.path.join(APP_DIR, "server.py")],
        stdout=server_log,
        stderr=server_log,
        start_new_session=True,
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
    "Full Folder Name:": "Example: Jordan-Full-Backup or FULL",
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

save_button = ttk.Button(root, text="Save Configuration", command=save_paths)
save_button.grid(row=len(labels) + 4, column=1, pady=25)

result_label = ttk.Label(root, text="", background='#1a1a1a', foreground='#ffffff')
result_label.grid(row=len(labels) + 5, column=1, pady=25)

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