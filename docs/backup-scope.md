[← Back to README](../README.md)

## What Gets Backed Up

By default, FolderW backs up **everything** under `SRC_DIR` — every file and folder, recursively — except what's listed in `logs/rsync_exclude.txt`. There's no allowlist or file-type filtering; if it's not excluded, it's included.

### Default Exclusions

A handful of patterns are excluded out of the box, for a few different reasons:

**FolderW's own files** — if `SRC_DIR` is (or contains) FolderW's own install folder, these stop it from backing up itself:
- `logs/`, `rsync.log`, `rsync.txt`, `.log` — FolderW's own log files
- `lib`, `__pycache__` — the Python virtual environment and bytecode cache
- `folder-icon.png`, `FolderW.png`, `.directory` — the branding files FolderW writes into the backup folder itself (see [Branded Backup Folder](../README.md#features))

**Generic junk** — has no value in a backup regardless of what app created it:
- `.cache/` — cache directories in general
- `*.tmp`, `*.swp`, `*.swx`, `~*` — temp files and editor swap/backup files
- `*.db-journal` — transient SQLite rollback journals, recreated every transaction, never meaningful to restore

**Sparse virtual-disk and container/VM tooling state** — the important one to understand. Tools like Docker Desktop and dev VM sandboxes create disk image files with a huge *apparent* size but a much smaller *real* size on disk (a "sparse" file — mostly empty space, not actually allocated). A naive backup doesn't know the difference: it reads through the whole apparent size and can end up writing a multi-hundred-GB file to your backup destination for a few real GB of content, and along the way it can make progress percentages look wildly wrong (rsync reports having "transferred" the file's huge logical size long before real progress reflects it). None of this is data you'd actually want to restore anyway — it's regenerable tooling state, not personal files:
- `overlay2/`, `**/.local/share/docker` — Docker's storage driver and full data directory (images, containers, volumes, build cache)
- `**/.docker/desktop/vms` — Docker Desktop's own VM disk (`Docker.raw`)
- `**/.config/Claude/vm_bundles` — Claude Code's sandboxed VM disk images
- `**/.npm/_cacache` — npm's downloaded-package cache (not sparse, just large and fully regenerable)

If you use other tools with similar sparse virtual-disk files (VirtualBox, VMware, QEMU/libvirt, other container runtimes), consider excluding those too — see below.

### Excluding Your Own Files

`logs/rsync_exclude.txt` lists patterns, one per line, to skip — add your own for anything specific to your `SRC_DIR` (build output, another app's cache, a specific large file, etc.).

Pattern syntax follows `rsync`'s filter rules: a bare name (`node_modules`) matches that name anywhere in the tree; a pattern with a `/` in it (other than a trailing one) is anchored to `SRC_DIR`'s root; use a `**/` prefix (e.g. `**/some/nested/path`) to match a nested path at any depth, and to reliably exclude that path from FolderW's own size/progress calculations too (which use a slightly different wildcard engine than `rsync` itself — `**/` is the form verified to work correctly for both).

This one file drives both **what gets backed up** (rsync's `--exclude-from`) and **what the watchdog reacts to** — a change inside an excluded path won't reset the 2-minute debounce timer either, so a busy cache directory can't perpetually delay a real backup. Changes to this file take effect the next time a backup runs or the watchdog restarts.
