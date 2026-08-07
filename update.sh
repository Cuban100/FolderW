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
#
# Checked once here and reused below for both services: restarting
# folderw-backup.service (main_backup.py, the watchdog supervisor) while a
# backup is running would kill rsync_incremental.py as its child process,
# interrupting the backup — same reason folderw.service (just the dashboard,
# not part of the backup itself, but kept consistent) waits too rather than
# restarting out from under an in-progress run.
if pgrep -f "rsync_incremental.py" > /dev/null; then
    backup_running=1
    log "A backup appears to be running right now — skipping automatic restarts so it isn't interrupted."
else
    backup_running=0
fi

if systemctl --user list-unit-files folderw.service &>/dev/null; then
    if [ "$backup_running" -eq 1 ]; then
        log "Once it finishes, apply the update with: systemctl --user restart folderw"
    else
        log "Restarting the folderw service (dashboard) so the update takes effect..."
        systemctl --user restart folderw
        log "folderw service restarted."
    fi
else
    log "No systemd autostart service found for the dashboard. If FolderW is currently running manually (setup.py/server.py), stop and restart it now — the update won't take effect until you do."
fi

if systemctl --user list-unit-files folderw-backup.service &>/dev/null; then
    if [ "$backup_running" -eq 1 ]; then
        log "Once it finishes, apply the update with: systemctl --user restart folderw-backup"
    else
        log "Restarting the folderw-backup service (watchdog worker) so the update takes effect..."
        systemctl --user restart folderw-backup
        log "folderw-backup service restarted."
    fi
fi
