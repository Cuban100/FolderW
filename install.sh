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

# Set the project root directory
current_dir=$(pwd)


# Create the virtual environment
log "Creating virtual environment in '$current_dir'..."
python3 -m venv "$current_dir"
if [ $? -ne 0 ]; then
    log "ERROR: Failed to create virtual environment."
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
