#!/bin/bash

# Define log function
log() {
    echo -e "[$(date +'%Y-%m-%d %H:%M:%S')] $1"
}

# Function to check if command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Verify if Python is installed
if ! command_exists python3; then
    log "ERROR: Python3 is not installed. Please install Python3 and try again."
    exit 1
fi

# Best-effort system package install across common package managers --
# "Linux only" (see README) doesn't mean any one specific distro, so this
# doesn't assume apt. Returns 1 (caller decides how to react) if no
# supported package manager is found, rather than guessing at one.
install_system_package() {
    local apt_pkg="$1" dnf_pkg="$2" pacman_pkg="$3" zypper_pkg="$4" apk_pkg="$5"
    if command_exists apt-get; then
        sudo apt-get update && sudo apt-get install -y "$apt_pkg"
    elif command_exists dnf; then
        sudo dnf install -y "$dnf_pkg"
    elif command_exists yum; then
        sudo yum install -y "$dnf_pkg"
    elif command_exists pacman; then
        sudo pacman -Sy --noconfirm "$pacman_pkg"
    elif command_exists zypper; then
        sudo zypper install -y "$zypper_pkg"
    elif command_exists apk; then
        sudo apk add "$apk_pkg"
    else
        return 1
    fi
}

# Verify rsync is installed -- the actual backup engine every part of
# FolderW is built around. Previously never checked here at all: setup
# could complete successfully end to end and only fail, confusingly, the
# first time an actual backup ran.
if ! command_exists rsync; then
    log "rsync not found -- attempting to install it..."
    if ! install_system_package rsync rsync rsync rsync rsync; then
        log "ERROR: Could not detect a supported package manager (apt/dnf/yum/pacman/zypper/apk)."
        log "Please install rsync manually and re-run this script."
        exit 1
    fi
    if ! command_exists rsync; then
        log "ERROR: rsync installation failed. Please install it manually (e.g. 'sudo apt install rsync') and try again."
        exit 1
    fi
    log "rsync installed successfully."
fi

# Verify tkinter is available -- setup.py's GUI depends on it, and unlike
# every other Python dependency it can't come from requirements.txt/pip:
# it's a system package tied to the interpreter itself, named differently
# per distro family. Without this check, setup.py would only fail with a
# bare ModuleNotFoundError after everything else above already succeeded.
if ! python3 -c "import tkinter" >/dev/null 2>&1; then
    log "tkinter not found -- attempting to install it..."
    if ! install_system_package python3-tk python3-tkinter tk python3-tk py3-tkinter; then
        log "ERROR: Could not detect a supported package manager (apt/dnf/yum/pacman/zypper/apk)."
        log "Please install tkinter manually (e.g. 'sudo apt install python3-tk') and re-run this script."
        exit 1
    fi
    if ! python3 -c "import tkinter" >/dev/null 2>&1; then
        log "ERROR: tkinter installation failed. Please install it manually (e.g. 'sudo apt install python3-tk') and try again."
        exit 1
    fi
    log "tkinter installed successfully."
fi

# Set the project root directory
current_dir=$(pwd)


# Create the virtual environment
log "Creating virtual environment in '$current_dir'..."
python3 -m venv "$current_dir"
if [ $? -ne 0 ]; then
    log "ERROR: Failed to create virtual environment. On Debian/Ubuntu this is often because"
    log "the venv module isn't installed separately from python3 -- try: sudo apt install python3-venv"
    exit 1
fi

log "Virtual environment created successfully in '$current_dir'."

# Activate the virtual environment
source "$current_dir/bin/activate"
if [ $? -ne 0 ]; then
    log "ERROR: Failed to activate virtual environment."
    exit 1
fi

log "Virtual environment activated successfully."

# Install requirements
log "Installing requirements..."
pip install -r requirements.txt
if [ $? -ne 0 ]; then
    log "ERROR: Failed to install requirements."
    exit 1
fi

log "Requirements installed successfully."

# Run the setup script
log "Running setup script..."
python3 setup.py
if [ $? -ne 0 ]; then
    log "ERROR: Failed to run setup script."
    exit 1
fi

log "Setup script ran successfully. Setup completed!"
log "The dashboard is starting in the background and will open in your default browser in a few seconds."
