#!/bin/bash

# Completely removes a FolderW installation from this system: stops and
# kills every FolderW process (systemd-tracked or not), removes the
# systemd service files, and deletes this entire cloned repository —
# including your .env configuration and the folderw.db database.
#
# Your actual backed-up data (the destination configured as BASE_DIR) is
# never touched by this script.

log() {
    echo -e "[$(date +'%Y-%m-%d %H:%M:%S')] $1"
}

SERVICE_NAME="folderw.service"
BACKUP_SERVICE_NAME="folderw-backup.service"
SERVICE_DIR="$HOME/.config/systemd/user"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=================================================================="
echo "WARNING: This will completely uninstall FolderW."
echo ""
echo "This will:"
echo "  - Stop and kill every FolderW process (dashboard, backup worker,"
echo "    watchdog, any in-progress rsync)"
echo "  - Remove the systemd service files ($SERVICE_NAME, $BACKUP_SERVICE_NAME)"
echo "  - PERMANENTLY DELETE this entire cloned repository:"
echo "      $APP_DIR"
echo "    including your .env configuration and the folderw.db database"
echo ""
echo "Your actual backed-up data (wherever BASE_DIR points to) will NOT"
echo "be touched or deleted."
echo "=================================================================="
echo ""
read -p "Type 'yes' to continue, anything else to cancel: " confirm
if [ "$confirm" != "yes" ]; then
    log "Cancelled. Nothing was changed."
    exit 0
fi

log "Stopping systemd services..."
systemctl --user stop "$BACKUP_SERVICE_NAME" 2>/dev/null
systemctl --user stop "$SERVICE_NAME" 2>/dev/null
systemctl --user disable "$SERVICE_NAME" 2>/dev/null
systemctl --user disable "$BACKUP_SERVICE_NAME" 2>/dev/null

log "Killing any lingering FolderW processes (systemd-tracked or orphaned)..."
# Orphaned processes (started outside systemd — it happens) never respond
# to `systemctl stop` at all, so kill by matching the actual script paths
# directly too, not just via the service.
pkill -9 -f "$APP_DIR/server.py" 2>/dev/null
pkill -9 -f "$APP_DIR/main_backup.py" 2>/dev/null
pkill -9 -f "$APP_DIR/rsync_incremental.py" 2>/dev/null
pkill -9 -f "$APP_DIR/rsync_event_handler.py" 2>/dev/null
sleep 1

log "Removing systemd unit files..."
rm -f "$SERVICE_DIR/$SERVICE_NAME"
rm -f "$SERVICE_DIR/$BACKUP_SERVICE_NAME"
systemctl --user daemon-reload
systemctl --user reset-failed 2>/dev/null

log "Note: if setup enabled 'loginctl enable-linger' for this user account"
log "so FolderW could start at boot, that setting is account-wide and is"
log "left as-is (it may be relied on by other services). To undo it"
log "manually: loginctl disable-linger \$USER"

log "Deleting the cloned repository: $APP_DIR"
cd "$HOME" || exit 1
rm -rf "$APP_DIR"

log "FolderW has been uninstalled. Your backed-up data was left untouched."
