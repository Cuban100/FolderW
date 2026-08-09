[← Back to README](../README.md)

## What Gets Backed Up

By default, FolderW backs up **everything** under `SRC_DIR` — every file and folder, recursively, with no exclusions at all. There's no allowlist or file-type filtering, and nothing is skipped unless you say so on the Settings page.

### Excluding Your Own Files

The **File Exclusions** field on the Settings page (saved to `logs/custom_exclude.txt`, one pattern per line) is the only exclusion mechanism — a comma-separated list of patterns to skip. Leave it blank to exclude nothing.

Pattern syntax follows `rsync`'s filter rules: a bare name (`node_modules`) matches that name anywhere in the tree; a pattern with a `/` in it (other than a trailing one) is anchored to `SRC_DIR`'s root; use a `**/` prefix (e.g. `**/some/nested/path`) to match a nested path at any depth, and to reliably exclude that path from FolderW's own size/progress calculations too (which use a slightly different wildcard engine than `rsync` itself — `**/` is the form verified to work correctly for both).

This one file drives both **what gets backed up** (rsync's `--exclude-from`) and **what the watchdog reacts to** — a change inside an excluded path won't reset the 2-minute debounce timer either, so a busy cache directory can't perpetually delay a real backup. Changes take effect the next time a backup runs or the watchdog restarts.

### Things worth excluding, if they apply to you

Nothing below is excluded automatically — these are just the cases we've actually hit that are worth knowing about:

**If `SRC_DIR` is (or contains) FolderW's own install folder** — otherwise it backs up itself, including its own venv:
- `lib`, `lib64`, `__pycache__` — the Python virtual environment and bytecode cache (`lib64` is usually just a symlink to `lib`; if you exclude `lib` and not `lib64`, you'll get a dangling symlink in the backup — exclude both together)
- `logs/`, `rsync.log`, `rsync.txt`, `.log` — FolderW's own log files
- `folder-icon.png`, `FolderW.png`, `.directory` — the branding files FolderW writes into the backup folder itself (see [Branded Backup Folder](../README.md#features))

**Sparse virtual-disk and container/VM tooling state** — the one to actually pay attention to. Tools like Docker Desktop and dev VM sandboxes create disk image files with a huge *apparent* size but a much smaller *real* size on disk (a "sparse" file — mostly empty space, not actually allocated). A backup doesn't know the difference: it reads through the whole apparent size and can end up trying to write a multi-hundred-GB (sometimes ~1TB) file to your backup destination for a few real GB of content, and along the way it can make progress percentages look wildly wrong. None of it is data you'd actually want to restore anyway — it's regenerable tooling state, not personal files:
- `overlay2/`, `**/.local/share/docker` — Docker's storage driver and full data directory (images, containers, volumes, build cache)
- `**/.docker/desktop/vms` — Docker Desktop's own VM disk (`Docker.raw`)
- `**/.config/Claude/vm_bundles` — Claude Code's sandboxed VM disk images
- `**/.npm/_cacache` — npm's downloaded-package cache (not sparse, just large and fully regenerable)

If you use other tools with similar sparse virtual-disk files (VirtualBox, VMware, QEMU/libvirt, other container runtimes), the same reasoning applies to those too.

**Generic junk**, if it bothers you — harmless to back up, just has no restore value:
- `.cache/` — cache directories in general
- `*.tmp`, `*.swp`, `*.swx`, `~*` — temp files and editor swap/backup files
- `*.db-journal` — transient SQLite rollback journals, recreated every transaction, never meaningful to restore
