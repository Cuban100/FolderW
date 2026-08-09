// Loaded as a blocking <script src> in <head> (no defer/async) so this runs
// before the sidebar paints — applying the collapsed state here, rather
// than after DOMContentLoaded, avoids a flash of the expanded sidebar on
// every page load.
if (localStorage.getItem('folderw_sidebar_collapsed') === '1') {
    document.documentElement.classList.add('sidebar-collapsed');
}

function toggleSidebar() {
    const collapsed = document.documentElement.classList.toggle('sidebar-collapsed');
    localStorage.setItem('folderw_sidebar_collapsed', collapsed ? '1' : '0');
}

// [?] popover toggle -- shared by Settings' form fields and the sidebar
// controls below the nav links (see templates/_sidebar.html), both
// present on every page since sidebar.js itself is loaded everywhere.
function toggleHelp(event, id) {
    event.stopPropagation();
    const popover = document.getElementById(id);
    const btn = event.currentTarget;
    document.querySelectorAll('.help-popover').forEach(p => {
        if (p !== popover) p.classList.add('hidden');
    });
    document.querySelectorAll('.help-icon').forEach(b => {
        if (b !== btn) b.classList.remove('active');
    });
    popover.classList.toggle('hidden');
    btn.classList.toggle('active');
}
document.addEventListener('click', (event) => {
    if (!event.target.closest('.help-wrap')) {
        document.querySelectorAll('.help-popover').forEach(p => p.classList.add('hidden'));
        document.querySelectorAll('.help-icon').forEach(b => b.classList.remove('active'));
    }
});

// Stops folderw-backup.service (or, without a systemd unit, kills the
// backup/watchdog processes directly) -- never folderw.service, so the
// dashboard itself stays reachable regardless. Shared by the sidebar's
// Stop Watchdog / Stop Backup Service buttons and index.html's own
// "Stop Full Backup" button (shown while a backup is actively running).
async function stopWatchdog(event) {
    if (!confirm('Stop the watchdog and any backup currently in progress?')) {
        return;
    }
    // systemctl stop blocks until the service (and rsync under it)
    // actually exits — if rsync doesn't respond to SIGTERM right away
    // (mid-transfer, cleaning up a partial file), that wait can run to
    // systemd's stop timeout. Give immediate feedback so the page
    // doesn't look frozen/unresponsive in the meantime.
    const btn = event && event.target;
    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Stopping… (this can take a moment)';
        btn.style.cursor = 'wait';
    }
    try {
        const response = await fetch('/stop-backup', { method: 'POST' });
        await response.json();
    } catch (error) {
        console.error('Error stopping backup:', error);
    }
    // Not reload(): this can be clicked from /run-all-steps (reached via
    // startBackup()'s navigation) or, now that these controls live in
    // the sidebar, from any page -- and /run-all-steps has a side effect
    // where every GET to it restarts the backup service if checks pass.
    // Reloading THAT url after a stop would immediately re-trigger a
    // fresh backup, undoing the stop that just succeeded. Navigating to
    // / always lands on the dashboard without re-running that side effect.
    window.location.href = '/';
}

// Counterpart to stopWatchdog() -- starts folderw-backup.service (only
// meaningful when a systemd unit exists; see /start-backup-service).
async function startBackupService(event) {
    const btn = event && event.target;
    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Starting…';
        btn.style.cursor = 'wait';
    }
    try {
        const response = await fetch('/start-backup-service', { method: 'POST' });
        await response.json();
    } catch (error) {
        console.error('Error starting backup service:', error);
    }
    window.location.href = '/';
}
