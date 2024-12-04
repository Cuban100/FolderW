# rsync-backups-python-incremental
Backup Automation with Tkinter GUI and Event-Driven Monitoring

This project is designed to simplify and automate the process of backing up files from a specified source directory to a target directory. Key features include:

Graphical User Interface (GUI):

Built using Tkinter for a user-friendly configuration process.

Allows users to select directories and files through a GUI.

Offers options to enable or disable event-driven backups.

Environment Configuration:

Uses a .env file to store configuration settings securely.

Includes a setup script to create and configure the .env file.

Virtual Environment Management:

Provides an install.sh script to set up a Python virtual environment.

Automatically installs required packages from requirements.txt.

Automated Backups:

Supports both regular scheduled backups and event-driven backups.

Utilizes rsync to efficiently copy files from the source to the backup directory.

Logging and Error Handling:

Comprehensive logging to monitor the backup process.

Robust error handling to ensure reliable execution.

Integration with ttkthemes:

Enhances the Tkinter GUI with a visually appealing dark theme.

Key Components
install.sh: Bash script to create a virtual environment, install dependencies, and run the setup script.

setup.py: Tkinter-based GUI for configuring backup settings and initializing the environment.

rsync_incremental.py: Python script utilizing rsync for performing backups.

main_backup.py: Script triggered to start the backup process.

Usage
Setup:

Run install.sh to set up the project environment.

Use the Tkinter GUI (setup.py) to configure paths and settings.

Save the configuration to generate the .env file.

Execution:

The system can watch the source directory for changes and perform backups automatically.

Manual backups can also be initiated as needed.
