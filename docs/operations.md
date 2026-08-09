[← Back to README](../README.md)

## Updating

The dashboard checks `origin/main` for new commits automatically (cached, refreshed once per page load — never blocks the page waiting on a live git call) and shows a banner with what's new when your install is behind. Click **Update Now** to pull, update dependencies, and restart, all from the browser — no terminal needed. The dashboard will be briefly unreachable while it restarts itself; the page polls automatically and reloads once it's back.

Prefer the terminal, or don't have an internet-facing dashboard? Run `update.sh` from inside your existing installation folder:

```bash
cd FolderW
chmod +x update.sh
./update.sh
```

It pulls the latest code, updates dependencies, and — if the autostart systemd service is set up — restarts it automatically so the update actually takes effect (a running Python process keeps running whatever code it started with until it's restarted; `git pull` alone doesn't affect it). If a backup happens to be running at the moment, the automatic restart is skipped so it isn't interrupted — restart manually once it finishes.

**Your `.env` file and database are never touched.** Both are already excluded from version control, so `git pull` doesn't even see them — nothing about the update process can overwrite your settings or backup history.

## Uninstalling

Run `uninstall.sh` from inside your installation folder to remove FolderW completely:

```bash
cd FolderW
chmod +x uninstall.sh
./uninstall.sh
```

It shows a warning and asks for confirmation before doing anything. Once confirmed, it stops and kills every FolderW process (the systemd services and any that ended up running outside of them), removes both systemd unit files, and then **permanently deletes the entire cloned repository folder — including your `.env` configuration and the `folderw.db` database.**

**Your actual backed-up data is never touched.** Only the tool itself is removed — whatever's at `BASE_DIR` (the full backup and any incremental snapshots) is left exactly as it was.

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
