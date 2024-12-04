import os
import subprocess
import tkinter as tk
from tkinter import filedialog, Label, PhotoImage
from tkinter import ttk, StringVar
from ttkthemes import ThemedTk
from PIL import Image, ImageTk
from dotenv import load_dotenv, set_key, find_dotenv
from setuptools import setup, find_packages

# Function to upgrade packages based on requirements.txt
def upgrade_packages(requirements_file='requirements.txt'):
    with open(requirements_file, 'r') as file:
        packages = file.readlines()
    
    for package in packages:
        package_name = package.split('==')[0]  # Extract package name, ignoring version
        print(f"Upgrading {package_name}")
        subprocess.run(["pip", "install", "--upgrade", package_name])
# Function to toggle backup interval options



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
        
    # Initialize paths dictionary with essential fields
    paths = {
        'LOG_DIR': log_dir_entry.get(),
        'SRC_DIR': src_dir_entry.get(),
        'BASE_DIR': base_dir_entry.get(),
        'DATABASE': database_entry.get(),
        'FULL_NAME': full_folder_name_entry.get(),
        'MONITOR': str(monitor_var.get()),  # Save the monitor status (checkbox value)
    }
        
    # Only add BACKUP_INTERVAL if MONITOR is unchecked (monitor_var.get() == 0)
    if monitor_var.get() == 0:
        paths['BACKUP_INTERVAL'] = interval_var.get()  # Save backup interval when monitoring is not enabled
        
    # Validate if all fields are filled
    for key, value in paths.items():
        if not value or "Browse to select" in value:
            result_label.config(text=f"Error: {key.replace('_', ' ')} is required.", foreground='#FF0000')  # Set to red
            return
        
    # Save values to .env file
    for key, value in paths.items():
        set_key(env_path, key.upper(), value)
        
    result_label.config(text="Paths saved to .env file", foreground='#39FF14')  # Set to neon green

    # Upgrade packages
    upgrade_packages()

    # Schedule window close after 15 seconds and trigger main_backup.py
    root.after(15000, close_and_trigger_backup)


# Function to close the window and trigger the backup script
def close_and_trigger_backup():
    root.destroy()
    subprocess.run(["python", "main_backup.py"])

# Function to toggle backup interval options
def toggle_backup_options():
    if monitor_var.get() == 1:
        for widget in interval_widgets:
            widget.grid_remove()
    else:
        for widget in interval_widgets:
            widget.grid()

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
# Create the checkbox for monitoring source folder

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

# Load and display the logo with specific size 
logo_path = os.path.join(os.path.dirname(__file__), 'FolderW.png') 
logo_image = Image.open(logo_path) 
logo_image = logo_image.resize((100, 100), Image.Resampling.LANCZOS)
logo = ImageTk.PhotoImage(logo_image) 
logo_label = Label(root, image=logo) 
logo_label.image = logo
logo_label.place(x=485, y=300)


monitor_var = tk.IntVar(value=1)  
monitor_checkbox = tk.Checkbutton(root, text="Monitor source folder for automated backups on changes", variable=monitor_var, bg='#1a1a1a', fg='#ffffff', selectcolor='#2ecc71', command=toggle_backup_options)
monitor_checkbox.grid(row=len(labels), column=1, padx=10, pady=10, sticky='w')  # Aligned with other fields

# Create the label next to the checkbox
monitor_label = tk.Label(root, text="Watchdog Future:", fg='#ffffff', bg='#1a1a1a', padx=10, pady=10)
monitor_label.grid(row=len(labels), column=0, padx=0, pady=10, sticky='e')  # Place it in the same row as the checkbox, to the left (column 0)


# Dynamic Backup Interval Options
interval_var = StringVar(value='hourly')
interval_label = tk.Label(root, text="Backup Interval:", fg='#ffffff', bg='#1a1a1a')
interval_dropdown = ttk.Combobox(root, textvariable=interval_var, values=["hourly", "half-day", "daily", "weekly"])
interval_widgets = [interval_label, interval_dropdown]

interval_label.grid(row=len(labels) + 1, column=0, padx=10, pady=10, sticky='e')
interval_dropdown.grid(row=len(labels) + 1, column=1, padx=10, pady=10)

save_button = ttk.Button(root, text="Save Paths", command=save_paths)
save_button.grid(row=len(labels) + 2, column=1, pady=25)

result_label = ttk.Label(root, text="", background='#1a1a1a', foreground='#ffffff')
result_label.grid(row=len(labels) + 3, column=1, pady=25)

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
def toggle_backup_options():
    if monitor_var.get() == 1:
        # Hide the interval widgets when the monitor is checked
        for widget in interval_widgets:
            widget.grid_remove()
    else:
        # Show the interval widgets when the monitor is unchecked
        for widget in interval_widgets:
            widget.grid()

# Call the toggle function immediately to adjust the interval options
toggle_backup_options()
# Start the Tkinter loop
root.mainloop()


# Metadata setup for the package
setup(
    name='FolderW',
    version='0.1',
    packages=find_packages(),
    include_package_data=True,
    install_requires=[
        'os',
        'python-dotenv',
        'subprocess',
        'schedule',
        'time',
        'loguru',
        'watchdog',
        'sqlite3',
        'shutil',
        'datetime'

    ],
    package_data={
        '': ['FolderW.png'],
    },
    entry_points={
        'console_scripts': [
            'upgrade-packages=upgrade_packages:upgrade_packages',
        ],
    },
    author='Erick Vladimir Salgado', 
    author_email='cuban100@yahoo.com',  # Replace with your email
    description='Backup Automation with Tkinter GUI and Event-Driven Monitoring',
    long_description=open('README.md').read(),
    long_description_content_type='text/markdown',
    url='https://github.com/Cuban100/FolderW',  # Replace with your project's URL
    classifiers=[
        'Programming Language :: Python :: 3',
        'License :: OSI Approved :: MIT License',  # Update with your license if different
        'Operating System :: OS Independent',
    ],
    keywords='backup automation tkinter monitoring',
    license='MIT',  # Update with your license
    python_requires='>=3.6',
)
