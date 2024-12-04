import os
import subprocess
import tkinter as tk
from tkinter import filedialog
from tkinter import ttk
from ttkthemes import ThemedTk
from dotenv import load_dotenv, set_key, find_dotenv

# Function to create Python virtual environment
def create_virtualenv():
    env_name = "rsync-backups"
    subprocess.run(["python3", "-m", "venv", env_name])
    print(f"Virtual environment '{env_name}' created successfully.")

# Function to install and upgrade required packages
def install_and_upgrade_packages():
    env_name = "rsync-backups"
    # Activate virtual environment
    if os.name == 'nt':
        activate_script = os.path.join(env_name, 'Scripts', 'activate')
    else:
        activate_script = os.path.join(env_name, 'bin', 'activate')

    # Commands to install, upgrade and freeze packages
    commands = [
        f"source {activate_script} && pip install --upgrade pip",
        f"source {activate_script} && pip install -r requirements.txt",
        f"source {activate_script} && pip install --upgrade --upgrade-strategy eager",
        f"source {activate_script} && pip freeze > requirements.txt"
    ]

    # Run commands
    for command in commands:
        subprocess.run(command, shell=True)
    
    print("Required packages installed and upgraded successfully.")
    print("Updated requirements.txt with the current package versions.")

# Function to browse for directory
def browse_directory(entry):
    directory = filedialog.askdirectory()
    entry.delete(0, tk.END)
    entry.insert(0, directory)

# Function to browse for file
def browse_file(entry):
    file_path = filedialog.askopenfilename()
    entry.delete(0, tk.END)
    entry.insert(0, file_path)

# Function to save paths to .env file
def save_paths():
    env_path = find_dotenv()
    if not env_path:
        env_path = '.env'
    
    paths = {
        'LOG_DIR': log_dir_entry.get(),
        'SRC_DIR': src_dir_entry.get(),
        'BASE_DIR': base_dir_entry.get(),
        'DATABASE': database_entry.get(),
        'FULL_NAME': full_folder_name_entry.get(),
        'MONITOR': str(monitor_var.get())
    }
    
    # Validate if all fields are filled
    for key, value in paths.items():
        if not value or "Browse to select" in value:
            result_label.config(text=f"Error: {key.replace('_', ' ')} is required.", foreground='#FF0000')  # Set to red
            return
    
    for key, value in paths.items():
        set_key(env_path, key.upper(), value)
    
    result_label.config(text="Paths saved to .env file", foreground='#39FF14')  # Set to neon green

    # Create virtual environment and install packages
    create_virtualenv()
    install_and_upgrade_packages()

    # Schedule window close after 30 seconds and trigger main_backup.py
    root.after(15000, close_and_trigger_backup)

# Function to close the window and trigger the backup script
def close_and_trigger_backup():
    root.destroy()
    subprocess.run(["python", "main_backup.py"])

# Load existing .env file if available
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

# Create and place widgets
labels = [
    "Log file Directory:",  
    "Source Directory:", 
    "Base Backup Directory:", 
    "Database:", 
    "Full Folder Name:", 
]
entries = []

for i, label in enumerate(labels):
    tk.Label(root, text=label, fg='#ffffff', bg='#1a1a1a').grid(row=i, column=0, padx=10, pady=10, sticky='e')
    entry = ttk.Entry(root, width=50)
    entry.grid(row=i, column=1, padx=10, pady=10)
    entries.append(entry)
    if "Directory" in label or "Backup" in label:
        button = ttk.Button(root, text="Browse...", command=lambda e=entry: browse_directory(e))
        button.grid(row=i, column=2, padx=10, pady=10)
    elif "File" in label:
        button = ttk.Button(root, text="Browse...", command=lambda e=entry: browse_file(e))
        button.grid(row=i, column=2, padx=10, pady=10)

log_dir_entry, src_dir_entry, base_dir_entry, database_entry, full_folder_name_entry = entries

# Add a checkbox for monitoring option
monitor_var = tk.IntVar()
monitor_checkbox = tk.Checkbutton(root, text="Monitor source folder for automated backups on modifications or creation", variable=monitor_var, bg='#1a1a1a', fg='#ffffff', selectcolor='#2ecc71')
monitor_checkbox.grid(row=len(labels), column=1, pady=10)

save_button = ttk.Button(root, text="Save Paths", command=save_paths)
save_button.grid(row=len(labels) + 1, column=1, pady=25)

result_label = ttk.Label(root, text="", background='#1a1a1a', foreground='#ffffff')
result_label.grid(row=len(labels) + 2, column=1, pady=25)

# Load existing values if available
log_dir_entry.insert(0, os.getenv('LOG_DIR', 'Browse to select the log directory'))
src_dir_entry.insert(0, os.getenv('SRC_DIR', 'Browse to select the source directory'))
base_dir_entry.insert(0, os.getenv('BASE_DIR', 'Browse to select the backup directory'))
database_entry.insert(0, os.getenv('DATABASE', 'Enter the database file name'))
full_folder_name_entry.insert(0, os.getenv('FULL_NAME', 'Enter the full folder name'))

# Safely set the monitor variable
monitor_value = os.getenv('MONITOR')
if monitor_value is not None:
    monitor_var.set(int(monitor_value))
else:
    monitor_var.set(0)  # Default to 0 if MONITOR is not set

# Configure the root window background color
root.configure(bg='#1a1a1a')

# Center the window
center_window(root)

# Start the Tkinter loop
root.mainloop()

