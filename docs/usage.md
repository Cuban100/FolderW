[← Back to README](../README.md)

## Key Components

- **`install.sh`:** Bash script to create a virtual environment, install dependencies, and run the setup script.
- **`update.sh`:** Bash script to update an existing installation to the latest version. See [Operations](operations.md#updating).
- **`setup.py`:** Tkinter-based GUI for configuring backup settings and initializing the environment.
- **`rsync_incremental.py`:** Runs the initial full backup (shared by both methods) and every Incremental-mode snapshot after it.
- **`rsync_differential.py`:** Runs every Differential-mode snapshot; reuses `rsync_incremental.py`'s shared machinery directly. See [Restore & Backup Method](restore-and-backup-method.md#backup-method).
- **`main_backup.py`:** Script triggered to start the backup process; picks whichever of the two above matches the configured Backup Method.

## Usage

1. **Setup:**
   - Run `install.sh` to set up the project environment.
   - Use the Tkinter GUI (`setup.py`) to configure paths and settings.
   - Save the configuration to generate the `.env` file.

   <p align="center"><img src="../screenshots/setup-window.png" alt="FolderW setup window" width="600"></p>

2. **Execution:**
   - The system can watch the source directory for changes and perform backups automatically.
   - Manual backups can also be initiated as needed.
