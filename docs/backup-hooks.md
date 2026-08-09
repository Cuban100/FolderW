[← Back to README](../README.md)

## Backup Hooks

The **Backup Hooks** dashboard page lets you run your own scripts around every backup, regardless of what triggers it — scheduled, watchdog-triggered, or a manual run. Both are entirely optional; leave either blank if you don't need it.

- **Pre-Backup Script** — run before every backup. If it exits non-zero, the backup is skipped entirely for that run — useful for a database dump or stopping a service beforehand, so a failed pre-step never lets a backup capture inconsistent state.
- **Post-Backup Script** — run after every backup, **always**, whether the backup (or the pre-backup script) succeeded or not. Typically the counterpart to the pre-script — e.g. restarting a service it stopped — so a failed backup can't leave things down indefinitely.

Each script must be an executable file (`chmod +x`) — enter its full path.

Use the **Test Script** button next to each field to run whatever's currently typed in (even before saving) and see the result inline — exit code and any error output on failure, so a script can be verified before trusting it to run during a real backup and potentially abort one.

A hook that hangs is capped at a 5-minute timeout, so a stuck "stop service"/"dump database" command can't wedge every future backup forever.
