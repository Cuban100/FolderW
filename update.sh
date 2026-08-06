#!/bin/bash

# Updates an existing FolderW installation to the latest version from the
# repository. Safe to run repeatedly. Your .env file and database are never
# touched — both are gitignored, so `git pull` never sees them at all.

log() {
    echo -e "[$(date +'%Y-%m-%d %H:%M:%S')] $1"
}

if [ ! -d ".git" ]; then
    log "ERROR: This doesn't look like a FolderW git checkout (no .git directory found)."
    log "Run this script from inside your FolderW installation folder."
    exit 1
fi

log "Pulling latest changes from origin/main..."
# --autostash shelves any local edits to tracked files (e.g. a manually
# tweaked script) before pulling, then reapplies them after, so a dirty
# working tree can't block the update.
if ! git pull --autostash origin main; then
    log "ERROR: git pull failed. Resolve the error above, then re-run this script."
    exit 1
fi

if [ ! -f "bin/activate" ]; then
    log "ERROR: No virtual environment found (bin/activate missing). Run install.sh first for a new setup."
    exit 1
fi

log "Activating virtual environment..."
source bin/activate

log "Updating dependencies..."
if ! pip install -r requirements.txt; then
    log "ERROR: Failed to update dependencies."
    exit 1
fi

log "Code and dependencies updated successfully."

# A running Python process keeps executing whatever code was loaded when it
# started — pulling new files to disk has no effect on it until it restarts.
if systemctl --user list-unit-files folderw.service &>/dev/null; then
    if pgrep -f "main_backup.py" > /dev/null || pgrep -f "rsync_incremental.py" > /dev/null; then
        log "A backup appears to be running right now — skipping automatic restart so it isn't interrupted."
        log "Once it finishes, apply the update with: systemctl --user restart folderw"
    else
        log "Restarting the folderw service so the update takes effect..."
        systemctl --user restart folderw
        log "Service restarted."
    fi
else
    log "No systemd autostart service found. If FolderW is currently running manually (setup.py/server.py), stop and restart it now — the update won't take effect until you do."
fi
