[← Back to README](../README.md)

## Permissions

The actual `rsync` transfer runs as **root**, via a passwordless `sudo` rule scoped specifically to the `rsync` binary (`setup.py` configures this automatically — see `configure_rsync_sudo()`). Nothing else in FolderW runs elevated; the dashboard, the watchdog, and everything else stay as your regular user.

Why: files under `SRC_DIR` aren't always owned by you. A common case is Docker containers writing into a bind-mounted config directory using their own internal user — found in practice with a WireGuard container's peer configs, owned by a UID with no relation to the host account, `700`/`600` permissions, unreadable by a plain user-level process. Without root, rsync can't read those files at all; it isn't a bug it can work around on its own.

This is scoped as narrowly as sudo allows: the rule grants `NOPASSWD` on the `rsync` binary specifically (found via `shutil.which`), not blanket root access, and only after checking a rule allowing this doesn't already exist (some systems already have broader passwordless sudo configured for other reasons — in that case nothing new is added). The sudoers file is written to `/etc/sudoers.d/folderw-rsync` and validated with `visudo -c` before ever being installed, so a malformed rule can't break `sudo` system-wide.

If setup couldn't configure this automatically (`sudo`/`visudo` not installed, etc.), it prints the exact command to run manually — or backups will still work either way, just skipping (not hanging on) any file they don't have permission to read.

**Every copy in the backup always comes out owned by whoever runs FolderW, mode `775`** — regardless of the source file's own owner or permissions. Reading as root is only what lets rsync see files it otherwise couldn't; what actually lands in the backup is deliberately normalized (`--chown`/`--chmod`) rather than preserving another UID or a restrictive mode from the source.
