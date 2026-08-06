<p align="center"> <img src="https://github.com/Cuban100/FolderW/blob/main/logo.png" alt="FolderW Logo" width="200" height="200" style="vertical-align:middle; margin-right: 100px;"> </p>
<h1>FolderW</h1>

# A Python Incremental Backup with Watchdog or Scheduling, Backup Automation with Tkinter GUI and Event-Driven Monitoring


This project is designed to simplify and automate the process of backing up files from a specified source directory to a target directory. Key features include:

> **Platform support:** FolderW currently works on **Linux only**. It relies on `rsync` and other Linux-specific tooling, and hasn't been tested on macOS or Windows.

## Features

1. **Graphical User Interface (GUI):**
   - Built using Tkinter for a user-friendly configuration process.
   - Allows users to select directories and files through a GUI.
   - Offers options to enable or disable event-driven backups.

2. **Environment Configuration:**
   - Uses a `.env` file to store configuration settings securely.
   - Includes a setup script to create and configure the `.env` file.

3. **Virtual Environment Management:**
   - Provides an `install.sh` script to set up a Python virtual environment.
   - Automatically installs required packages from `requirements.txt`.

4. **Automated Backups:**
   - Supports both regular scheduled backups and event-driven backups.
   - Utilizes `rsync` to efficiently copy files from the source to the backup directory.

5. **Logging and Error Handling:**
   - Comprehensive logging to monitor the backup process.
   - Robust error handling to ensure reliable execution.

6. **Integration with `ttkthemes`:**
   - Enhances the Tkinter GUI with a visually appealing dark theme.

7. **Autostart on Boot:**
   - Optional checkbox in the setup GUI installs a systemd user service so the dashboard starts automatically on login/boot.

## Quick Start

Copy and run the commands below to deploy FolderW:

```bash
git clone https://github.com/Cuban100/FolderW.git
cd FolderW
chmod +x install.sh
./install.sh
```

## Key Components

- **`install.sh`:** Bash script to create a virtual environment, install dependencies, and run the setup script.
- **`setup.py`:** Tkinter-based GUI for configuring backup settings and initializing the environment.
- **`rsync_incremental.py`:** Python script utilizing `rsync` for performing backups.
- **`main_backup.py`:** Script triggered to start the backup process.

## Usage

1. **Setup:**
   - Run `install.sh` to set up the project environment.
   - Use the Tkinter GUI (`setup.py`) to configure paths and settings.
   - Save the configuration to generate the `.env` file.

2. **Execution:**
   - The system can watch the source directory for changes and perform backups automatically.
   - Manual backups can also be initiated as needed.

## Autostart on Boot (systemd)

Checking **"Start FolderW automatically at system boot"** in the setup GUI installs and enables a systemd **user** service (`folderw.service`) that runs `server.py` on login/boot. No root/sudo is required — it's managed entirely through `systemctl --user`.

The service unit lives at `~/.config/systemd/user/folderw.service`. Useful commands:

```bash
# Check whether it's running
systemctl --user status folderw

# Stop / start / restart it
systemctl --user stop folderw
systemctl --user start folderw
systemctl --user restart folderw

# Turn autostart off/on without deleting the unit file
systemctl --user disable folderw
systemctl --user enable folderw

# Tail its logs
journalctl --user -u folderw -f
```

**Note:** always use `--user` with these commands — this is a per-user service (`~/.config/systemd/user/`), not a system-wide one (`/etc/systemd/system/`), so plain `systemctl` (without `--user`) won't find it.

If the dashboard doesn't come up automatically before you log in (e.g. on a headless server), you may need to run `loginctl enable-linger $USER` once so your user services can start without an active login session; setup.py attempts this automatically and prints a note if it couldn't.

Unchecking the autostart box and saving again disables the service (`systemctl --user disable`), but does not delete the unit file.

This project aims to streamline backup operations, providing a reliable and user-friendly solution for both regular and event-driven file backups.
