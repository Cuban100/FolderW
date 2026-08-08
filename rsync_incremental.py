import os
import re
import sys
import time
import threading
from db_operations import get_last_session_number, store_changes_in_db, load_other_variables, load_env_value, record_backup_run, set_database_value, get_database_value
from restore_operations import cleanup_old_snapshots
from statistics_operations import get_folder_size_du, get_folder_size_bytes_du, human_readable_size
from notifications import notify
from dotenv import load_dotenv
import sqlite3
import subprocess
from datetime import datetime
from loguru import logger
import shutil

logfile = load_other_variables('logfile')
base_dir = load_env_value('BASE_DIR')
exclude_file = load_other_variables('exclude_file')
src_dir = load_env_value('SRC_DIR')
full_backup = load_other_variables('full_backup')
snapshots_root = load_other_variables('snapshots_root')
rsync_txt = load_other_variables('rsync_txt')
database = load_env_value('DATABASE')
logger.add(logfile, level="INFO", format="{time} - {level} - {message}")



def record_backup_statistics(changes, last_session_number, incremental_folder):
    """Record that this backup run happened — the full backup (session 1)
    or a specific incremental snapshot — plus per-run numeric stats.
    Recording the run itself matters even when 0 files changed, which is
    why this is separate from (and unconditional on) the numeric stats
    below. Deliberately doesn't re-walk the whole source tree (could be
    huge, e.g. a home directory), so this stays cheap on every run: only
    sums the sizes of files that actually changed this session.
    """
    total_files_processed = len(changes)
    total_size_processed = 0
    for _, rel_path in changes:
        fp = os.path.join(src_dir, rel_path)
        if os.path.isfile(fp):
            try:
                total_size_processed += os.path.getsize(fp)
            except OSError as e:
                logger.warning(f"Could not get size of {fp}: {e}")
    try:
        conn = sqlite3.connect(database)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO statistics (timestamp, total_files_processed, total_size_processed, source_size, destination_size, deleted_empty_folders, average_speed)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            total_files_processed,
            total_size_processed,
            None,
            None,
            0,
            None,
        ))
        conn.commit()
        conn.close()
        logger.success(f"Recorded backup statistics: {total_files_processed} file(s), {total_size_processed} bytes.")
    except sqlite3.Error as e:
        logger.error(f"Error recording backup statistics: {e}")

    if last_session_number <= 1:
        backup_type, label = 'full', 'Full Backup'
    else:
        backup_type, label = 'incremental', incremental_folder
    record_backup_run(last_session_number, backup_type, label, total_files_processed)

    # Cached here (once per backup run) rather than computed on every
    # dashboard page load — `du` walks the entire full_backup tree, which
    # can take minutes on a large backup and would make the dashboard feel
    # hung if run synchronously on every visit. full_backup is a symlink to
    # the snapshot this run just created (already repointed by the time
    # this runs) — get_folder_size_du/-D dereferences it correctly.
    current_size = get_folder_size_du(full_backup)
    if current_size:
        set_database_value('CURRENT_BACKUP_SIZE', current_size)

COMPLETION_MARKER = '.folderw_complete'


def _check_sudo_rsync_available():
    """Fail fast with a clear, specific message if passwordless sudo isn't
    configured for rsync, rather than letting it surface later as a
    generic "Rsync command failed with return code 1" -- correct on its
    own (sudo -n already refuses to hang waiting for a password nobody can
    type from a headless service, and the exit-code handling below already
    catches and notifies on that failure), but not obviously actionable
    without knowing sudo was the actual cause. `rsync --version` is a
    read-only, side-effect-free command, so this only tests whether sudo
    itself would prompt -- it doesn't touch the source or destination.
    """
    check = subprocess.run(
        ["sudo", "-n", "rsync", "--version"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    if check.returncode != 0:
        message = (
            "Passwordless sudo isn't configured for rsync -- backups can't "
            "run. This is normally set up automatically by setup.py "
            "(configure_rsync_sudo()); re-run it, or see the README's "
            "Permissions section to configure it manually."
        )
        logger.error(f"{message} (sudo -n rsync --version: {check.stderr.strip()})")
        notify("FolderW: Backup Failed", message)
        sys.exit(1)


def _new_snapshot_path(incremental_folder):
    return os.path.join(snapshots_root, incremental_folder)


def _unique_new_snapshot_path(incremental_folder):
    """Like _new_snapshot_path, but guarantees a path that doesn't already
    exist, appending -2/-3/... if needed. Two callers can legitimately
    generate the same minute-granularity folder name in quick succession
    -- confirmed live: the migration step below and this run's own
    incremental_folder computation happen milliseconds apart in the same
    process invocation, so a same-minute collision isn't a rare edge case,
    it's the *normal* case on any install that needs migrating. Without
    this, the first real backup after a migration would rsync directly
    into (and use as its own --link-dest source) the very snapshot that
    migration just created from the legacy directory, corrupting its
    point-in-time integrity. The -N suffix is accepted by restore_
    operations.py's folder-name validators (kept in lockstep, see
    _TIME_FOLDER_RE there) so a disambiguated snapshot still shows up
    normally in the restore UI.
    """
    base_folder = incremental_folder
    suffix = 1
    path = _new_snapshot_path(base_folder)
    while os.path.exists(path):
        suffix += 1
        path = _new_snapshot_path(base_folder) + f"-{suffix}"
    return path


def _previous_snapshot_path():
    """The snapshot full_backup currently points at, or None if this is the
    very first backup ever (full_backup doesn't exist yet). Used both as
    the --link-dest source and as the size baseline for progress
    percentage — always a *complete*, successfully-finished snapshot,
    never a partial one, since full_backup is only ever repointed after a
    run's completion marker is written (see the atomic repoint below)."""
    if os.path.exists(full_backup):
        return os.path.realpath(full_backup)
    return None


def _repoint_full_backup(target_path):
    """Atomically make full_backup a symlink pointing at target_path.
    Writes a temp symlink alongside it, then os.replace() over the real
    path -- so a reader (dashboard du/isdir calls, etc.) never observes a
    missing or broken full_backup mid-update. Removes any stale temp name
    left behind by a prior crash between the write and the replace."""
    parent = os.path.dirname(full_backup)
    os.makedirs(parent, exist_ok=True)
    tmp_path = full_backup + '.tmp-relink'
    if os.path.lexists(tmp_path):
        if os.path.isdir(tmp_path) and not os.path.islink(tmp_path):
            shutil.rmtree(tmp_path)
        else:
            os.remove(tmp_path)
    os.symlink(target_path, tmp_path)
    os.replace(tmp_path, full_backup)
    logger.info(f"full_backup now points at: {target_path}")


def _migrate_legacy_full_backup():
    """One-time migration for installs from before the --link-dest redesign,
    where full_backup was a real, continuously-synced mirror directory
    instead of a symlink to the newest snapshot. Folds that existing real
    directory into the new scheme as a proper dated snapshot (a fast,
    atomic same-filesystem rename -- not a copy, regardless of size) so
    existing backup data isn't discarded and can still be used as a
    --link-dest source for the very next run.

    Safe to call on every startup: no-ops immediately if full_backup is
    already a symlink (already migrated) or doesn't exist (fresh install,
    nothing to migrate).
    """
    if os.path.islink(full_backup):
        return
    if not os.path.exists(full_backup):
        if get_database_value('FULL_BACKUP_COMPLETED', 'settings') == '1':
            # full_backup missing but a completed backup was recorded --
            # something is inconsistent (interrupted migration, or the
            # symlink was deleted manually). Do NOT silently fall through
            # to a fresh full backup: that would re-transfer everything
            # from scratch while an already-migrated snapshot might be
            # sitting under snapshots_root, orphaned and unlinked. Surface
            # this loudly instead of guessing.
            logger.error(
                "full_backup is missing but FULL_BACKUP_COMPLETED is set -- "
                "refusing to silently start a fresh full backup. Check "
                f"{snapshots_root} for an orphaned snapshot and either "
                "restore the full_backup symlink manually or clear "
                "FULL_BACKUP_COMPLETED if a fresh backup is really intended."
            )
            sys.exit(1)
        return

    logger.warning(f"Migrating legacy full_backup directory into the snapshot scheme: {full_backup}")
    new_path = _unique_new_snapshot_path(generate_incremental_folder())
    os.makedirs(os.path.dirname(new_path), exist_ok=True)
    os.rename(full_backup, new_path)
    with open(os.path.join(new_path, COMPLETION_MARKER), 'w') as f:
        f.write(datetime.now().isoformat())
    _repoint_full_backup(new_path)
    logger.success(f"Migrated legacy full_backup to snapshot: {new_path}")

def _set_folder_icon(folder, icon_filename, write_physical_files=True):
    """Make `folder` itself show a branded icon in the file manager, rather
    than just containing a PNG a user has to open to notice.

    write_physical_files=False for full_backup specifically: it's a symlink
    to whichever snapshot is newest, not a stable directory of its own.
    Copying a PNG/.directory file "into" a symlink writes into whatever
    real snapshot directory it currently resolves to — confirmed live,
    this would silently plant two housekeeping files inside every single
    dated snapshot, inflating file counts and appearing in restored
    output. gio metadata is applied with --nofollow-symlinks instead,
    which tags the symlink path itself (confirmed this persists correctly
    even after the symlink is later deleted and recreated pointing
    elsewhere) rather than whatever it currently points at. This does mean
    full_backup loses the KDE Dolphin .directory-file fallback (gio
    metadata alone doesn't help non-GVFS file managers) -- a UX tradeoff,
    not a data-integrity one, and unavoidable without polluting snapshots.
    """
    if write_physical_files:
        icon_source = os.path.join(os.path.dirname(os.path.abspath(__file__)), icon_filename)
        icon_dest = os.path.join(folder, icon_filename)
        os.makedirs(folder, exist_ok=True)
        if os.path.exists(icon_source) and not os.path.exists(icon_dest):
            shutil.copy2(icon_source, icon_dest)
            logger.info(f"Added icon to folder: {icon_dest}")

        directory_file = os.path.join(folder, '.directory')
        if not os.path.exists(directory_file):
            with open(directory_file, 'w') as f:
                f.write(f"[Desktop Entry]\nIcon={icon_dest}\n")
            logger.info(f"Set folder icon via {directory_file}")

    try:
        gio_command = ["gio", "set"]
        if not write_physical_files:
            gio_command.append("--nofollow-symlinks")
        icon_uri = f"file://{os.path.join(folder, icon_filename)}" if write_physical_files else f"file://{os.path.join(os.path.dirname(os.path.abspath(__file__)), icon_filename)}"
        gio_command += [folder, "metadata::custom-icon", icon_uri]
        subprocess.run(gio_command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        logger.info(f"Set folder icon via gio metadata::custom-icon: {folder}")
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logger.warning(f"Could not set folder icon via gio (non-fatal): {e}")

def ensure_backup_folder_icon():
    # full_backup is a symlink to the newest snapshot -- tag the symlink
    # itself via gio (see _set_folder_icon), never write physical icon
    # files through it. Its FULL_NAME container folder (one level up --
    # e.g. "Caveman", holding both the symlink and Snapshots/) is never an
    # rsync destination itself, so it's still safe to brand normally.
    _set_folder_icon(full_backup, 'FolderW.png', write_physical_files=False)
    _set_folder_icon(os.path.dirname(full_backup), 'logo.png')

def rsync(destination, link_dest=None, result_holder=None):
    # destination: the NEW dated snapshot path this run writes into (not
    # the old fixed full_backup target) -- see the --link-dest redesign.
    # link_dest: realpath of the previous snapshot (full_backup's current
    # target before this run), or None on the very first-ever backup.
    #
    # result_holder (optional): a dict this function sets 'success' on once
    # the rsync command finishes — lets __main__ below tell a real failure
    # apart from a clean run without changing this generator's yield
    # contract (still just progress lines, unchanged for existing callers).
    # Trailing slash on the source makes rsync copy src_dir's *contents*
    # into destination, instead of nesting it as destination/<src_dir basename>/.
    src_dir_contents = src_dir.rstrip('/') + '/'
    # --info=progress2 reports overall transfer progress (a single running
    # percentage across the whole run) rather than per-file progress, which
    # is what a single dashboard progress bar needs.
    #
    # Deliberately WITHOUT --no-inc-recursive: that flag makes the
    # percentage numerically accurate by forcing rsync to build the
    # complete file list upfront, but on a large/deep source (e.g. an
    # entire home directory with hundreds of thousands of files) that
    # upfront scan can take many minutes with zero output — and unlike a
    # one-time cost, it repeats on every single run, including incremental
    # backups triggered by one changed file. Kept on the default
    # incremental recursion instead: the percentage understates true
    # progress for a while on such trees (denominator keeps growing as
    # rsync discovers more), but transferring starts immediately every time.
    # --sparse: without it, a sparse file (a VM disk, database file, etc. —
    # anything with a huge logical size but mostly-empty real content) gets
    # written to the destination in full, holes included, actually
    # consuming destination space matching its logical size instead of its
    # real one. Excluding Docker Desktop's VM disk (found the hard way)
    # only protects against that one specific file; --sparse protects
    # against every other sparse file nobody's found yet.
    # -vv (not just -v): the single -v was silent for files rsync decides
    # not to touch — which, deep in a large tree's file-list scan, is most
    # of what it's doing at any given moment (the "ir-chk" counter climbing
    # with nothing else printed). The second -v adds an explicit line per
    # already-up-to-date file too, so the activity log actually shows
    # what's being checked instead of going quiet for the whole scan.
    # sudo -n: runs rsync as root so files owned by another UID (a Docker
    # container writing into a bind-mounted config dir under a different
    # internal user, e.g. found live: WireGuard peer configs owned by UID
    # 2000, 700/600 permissions, unreadable by this process's own user) can
    # actually be read and backed up instead of silently being skipped or
    # (before a separate fix) hanging the whole run. setup.py's
    # configure_rsync_sudo() grants exactly this — NOPASSWD sudo scoped to
    # the rsync binary specifically — on every install, so this behaves the
    # same way for every user rather than only working around permission
    # walls on a machine that happens to already have broad passwordless
    # sudo configured for unrelated reasons. -n (non-interactive): if that
    # setup step was skipped or its sudoers rule removed later, this fails
    # immediately with a clear error instead of hanging forever waiting on
    # a password prompt that, from a headless service, can never come — the
    # same "never let it hang silently" reasoning as the stderr fix.
    # --out-format='CHANGED:%n': an unambiguous marker for parse_logfile()
    # to key off of. -vv's output mixes real transferred files in with a
    # lot of diagnostic noise (bracketed [sender]/[generator] messages,
    # "<file> is uptodate", internal buffer-expansion lines, exclude-
    # pattern notices) that looks like ordinary lines during the file-list
    # section, indistinguishable from real relative paths by the old
    # heuristic filtering. Found live: those diagnostic lines were getting
    # recorded as "changed files", including one literally containing a
    # path separator ("hiding directory .cache because of pattern
    # .cache/"), which the old copy_files() then tried to copy, creating a
    # real directory on disk named after that diagnostic message.
    # --out-format only affects the per-file transfer listing, not rsync's
    # own protocol/debug chatter, so prefixing it makes real changes
    # trivially and reliably distinguishable from everything else.
    # --link-dest=<previous snapshot>: the actual Timeshift-style mechanism
    # -- unchanged files (matching by quick-check against link_dest) become
    # instant hardlinks instead of being re-transferred, so destination is
    # a complete, space-efficient point-in-time tree every run, not just a
    # delta. Omitted entirely on the first-ever backup (link_dest is None).
    # --delete-excluded (not just --delete): matches Timeshift's actual
    # command. Safe now specifically because the icon files are no longer
    # written into this rsync-managed tree at all (see _set_folder_icon),
    # so nothing excluded needs protecting from active deletion.
    rsync_command = ["sudo", "-n", "rsync", "-avv", "--sparse", "--delete", "--delete-excluded", "--info=progress2", "--out-format=CHANGED:%n", f'--exclude-from={exclude_file}']
    if link_dest:
        rsync_command.append(f'--link-dest={link_dest}')
    rsync_command += [src_dir_contents, destination]
    logger.warning(f"Executing rsync command {rsync_command}")
        
    try:
        with open(rsync_txt, 'w') as log_f:
            # Run the rsync command and capture output in real-time.
            # stderr=STDOUT (not its own PIPE): found live, the hard way —
            # a separate stderr pipe that nothing ever reads deadlocks the
            # whole process once it fills. rsync hitting a real but
            # ordinarily-recoverable error (a permission-denied file, say)
            # writes a warning to stderr and moves on; with an unread pipe,
            # that write() itself blocks forever the moment the OS pipe
            # buffer (~64KB) fills up from accumulated warnings — confirmed
            # via strace: rsync's sender stuck mid-write() to fd 2, every
            # sub-process idle in pselect6 waiting on each other, the
            # dashboard showing a stall with no error surfaced anywhere.
            # Merging into the one stream we already read continuously
            # keeps it permanently drained, and as a bonus surfaces error
            # lines in the activity log/panel instead of losing them.
            process = subprocess.Popen(rsync_command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in process.stdout:
                log_f.write(line)
                if "%" in line:
                    yield line.strip()  # Stream progress to the frontend
            process.wait()
            # rsync exit codes 23 ("partial transfer due to error", e.g. one
            # file hit a permission problem) and 24 ("partial transfer due
            # to vanished source files") are routine on a large, actively-
            # used source tree during a long run -- not full failures. Found
            # live: a ~1.5 hour run that transferred cleanly the whole way
            # through, then hit code 24 because a handful of transient files
            # (temp files, browser caches) vanished while rsync was still
            # finishing its scan near the very end. Treating that as a hard
            # failure (as this used to) skipped session/stats recording and
            # the completion notification entirely for a backup that, in
            # practice, transferred everything it could reach.
            if process.returncode in (0, 23, 24):
                if process.returncode != 0:
                    logger.warning(f"Rsync completed with code {process.returncode} (partial transfer -- some files were skipped or vanished mid-run, not treated as a failure).")
                else:
                    logger.success(f"Rsync command executed successfully.")
                if result_holder is not None:
                    result_holder['success'] = True
            else:
                logger.error(f"Rsync command failed with return code {process.returncode}")
                if result_holder is not None:
                    result_holder['success'] = False
    except Exception as e:
        logger.error(f"Error executing rsync command: {e}")
        if result_holder is not None:
            result_holder['success'] = False

# Matches lines rsync prints because of --out-format='CHANGED:%n' (see the
# rsync_command construction above) -- an unambiguous marker for a file
# rsync actually transferred, deliberately distinct from -vv's diagnostic
# noise (bracketed [sender]/[generator] messages, "<file> is uptodate",
# buffer-expansion lines, exclude-pattern notices) which used to be
# indistinguishable from real relative paths by the old heuristic
# filtering. Verified empirically: deletions and "is uptodate" lines
# never get this prefix (they're logged via a separate, unformatted
# message path), so no separate exclusion logic is needed for either.
CHANGED_LINE_RE = re.compile(r'^CHANGED:(.+)$')

def parse_logfile(rsync_txt):
    changes = []
    try:
        with open(rsync_txt, 'r') as f:
            for line in f:
                match = CHANGED_LINE_RE.match(line.strip())
                if match:
                    path = match.group(1)
                    if path in ('.', './'):
                        # rsync emits this for the destination root itself
                        # (the top of the transfer, always "touched" since
                        # it's the target of the whole run) -- not a real
                        # file/subdirectory, would inflate "files changed"
                        # by one on every single run otherwise.
                        continue
                    changes.append(("update", path))
    except FileNotFoundError:
        logger.error(f"Rsync log file not found: {rsync_txt}")
    except Exception as e:
        logger.error(f"Error parsing rsync log file: {e}")
    logger.debug(f"Parsed changes: {changes}")
    return changes

def generate_incremental_folder():
    now = datetime.now() 
    month = now.strftime('%B') 
    day = now.strftime('%d') 
    hour = now.strftime('%I').lstrip('0')  
    minute = now.strftime('%M')
    am_pm = now.strftime('%p')
    folder = f"{month}/{day}/{hour}:{minute}-{am_pm}"
    logger.debug(f"Generated incremental folder name: {folder}")
    return folder



if __name__ == "__main__":
    # First thing, before any other work: if this is going to fail because
    # sudo isn't configured, fail now with a specific, actionable message
    # rather than after migration/baseline scans/etc. have already run.
    _check_sudo_rsync_available()

    # One-time (idempotent, cheap to re-check every run) migration for
    # installs from before the --link-dest redesign, where full_backup was
    # a real directory instead of a symlink to the newest snapshot.
    _migrate_legacy_full_backup()

    # Captured now, before this run's rsync touches anything -- this is
    # always a *complete*, previously-successful snapshot (or None on the
    # very first-ever backup), since full_backup is only ever repointed
    # after a run's completion marker is written further down. Used both
    # as the --link-dest source and as the progress-percent baseline.
    previous_snapshot = _previous_snapshot_path()

    # Computed once and reused everywhere below (the rsync destination,
    # --link-dest lookup already done above, the post-success symlink
    # target, and record_backup_statistics()'s label) -- generate_
    # incremental_folder() uses datetime.now(), so calling it more than
    # once in the same run risks two different folder names if the run
    # straddles a minute boundary.
    new_snapshot_path = _unique_new_snapshot_path(generate_incremental_folder())
    # Relative to snapshots_root, reflecting the -N suffix if a same-minute
    # collision was disambiguated above -- used as record_backup_statistics()'s
    # label, so it needs to match the real folder, not the pre-collision name.
    incremental_folder = os.path.relpath(new_snapshot_path, snapshots_root)
    # Created as this process's own user (caveman), not left for root's
    # sudo rsync to create via --mkpath -- confirmed empirically that
    # intermediate directories rsync-as-root creates end up root-owned,
    # silently locking cleanup_old_snapshots()/_prune_empty_parents() (both
    # run as caveman, both already swallow OSError silently) out of ever
    # deleting them again. Pre-creating here as caveman avoids that; rsync
    # then writes into an already-existing directory and leaves its
    # ownership alone.
    os.makedirs(new_snapshot_path, exist_ok=True)
    logger.info(f"New snapshot destination: {new_snapshot_path} (link-dest: {previous_snapshot})")

    # Cleared up front so a stale percentage/size/ETA from a previous run
    # can't be shown on the dashboard during the icon-setup gap before
    # rsync's first progress line actually arrives.
    set_database_value('BACKUP_PROGRESS_PERCENT', '')
    set_database_value('BACKUP_ETA', '')
    set_database_value('CURRENT_BACKUP_SIZE', '')
    # main_backup.py sets this while it's still running its own prerequisite
    # checks, before this process even exists — clear it now that this
    # script (the actual work is_backup_running() looks for) has taken over.
    set_database_value('BACKUP_PREPARING', '')
    # Elapsed time is computed by the dashboard (now - this), not rsync
    # itself — rsync only ever reports ETA (time remaining), never how
    # long the run has been going.
    set_database_value('BACKUP_START_TIME', str(time.time()))

    # rsync's own --info=progress2 percentage only counts bytes it actually
    # transfers this run — a file that already matches at the destination
    # is skipped and never counted, so on a resumed/interrupted backup its
    # percentage badly understates true progress (verified empirically: a
    # run that only needed to send one small new file into an otherwise
    # complete 50MB destination reported "0%" throughout). Computing our
    # own baseline — what's already effectively at the destination via
    # --link-dest hardlinks — lets us report (baseline + transferred-this-
    # run) / true-source-total instead. Both numbers come from `du`, run in
    # background threads so neither blocks rsync from starting and both run
    # concurrently with each other.
    # transferred_offset is the value of transferred_bytes (rsync's own
    # running counter, read in the main loop below) at the moment
    # dest_baseline was last measured. The live per-progress-line estimate
    # is dest_baseline + (transferred_bytes - transferred_offset) rather
    # than dest_baseline + transferred_bytes: both get re-anchored together
    # on every real `du` scan (initial and periodic, below), so the fast
    # estimate keeps converging back to ground truth instead of drifting
    # further from it, unbounded, for the rest of the run.
    baselines = {'dest_baseline': None, 'source_total': None, 'transferred_offset': 0, 'last_transferred': 0}

    def _compute_dest_baseline():
        # The *previous* snapshot, not the new (currently empty) one: at
        # t=0, new_snapshot_path has nothing in it yet -- previous_snapshot
        # is the correct "what's already effectively there" anchor, since
        # --link-dest will hardlink most of it into the new snapshot at
        # near-zero transfer cost.
        size = get_folder_size_bytes_du(previous_snapshot) if previous_snapshot else 0
        baselines['dest_baseline'] = size
        baselines['transferred_offset'] = baselines['last_transferred']
        logger.info(f"Destination baseline size: {size}")

    def _compute_source_total():
        # exclude_from matches what rsync itself will actually skip — without
        # it, anything excluded (e.g. Docker Desktop's sparse, ~1TB-apparent
        # VM disk) inflates this denominator far past what will ever really
        # be transferred, understating percent for the whole run.
        baselines['source_total'] = get_folder_size_bytes_du(src_dir, exclude_from=exclude_file)
        logger.info(f"Source total size: {baselines['source_total']}")
        if baselines['source_total'] is not None:
            # Keeps the dashboard's "Source Folder Size" stat in sync with
            # this run's own exclude-aware measurement, regardless of how
            # the run was started — that stat otherwise only refreshes when
            # someone clicks Check/Start Full Backup in the browser, so a
            # backup started any other way (systemd, a scheduled interval)
            # left it showing a stale, possibly pre-fix, unfiltered number.
            set_database_value('LAST_SRC_SIZE', human_readable_size(baselines['source_total']))

    threading.Thread(target=_compute_dest_baseline, daemon=True).start()
    threading.Thread(target=_compute_source_total, daemon=True).start()

    # CURRENT_BACKUP_SIZE and BACKUP_PROGRESS_PERCENT (set below, per
    # progress line) are a live estimate off dest_baseline — cheap and
    # immediate, but it double-counts modified files: the old version's
    # bytes are already in the baseline, and the new version's size gets
    # added on top too. It also has no idea about deletions (--delete is
    # on). This loop periodically re-`du`s the real destination and
    # re-anchors both dest_baseline and the estimate to ground truth.
    # Self-rescheduling — waits 60s after each scan *completes*, not on a
    # fixed clock — so scans on a huge tree can never overlap or pile up.
    stop_size_refresh = threading.Event()

    def _refresh_current_size_periodically():
        # Wait before the first scan too — _compute_dest_baseline above
        # already `du`s the previous snapshot once at startup; running a
        # redundant concurrent scan of a different (still mostly empty)
        # path at the same moment just adds I/O contention for no benefit.
        stop_size_refresh.wait(60)
        while not stop_size_refresh.is_set():
            # The NEW, currently-being-written snapshot -- not full_backup,
            # which stays pointed at the OLD snapshot for this run's entire
            # duration (only repointed after success, at the very end).
            # Measuring full_backup here would freeze CURRENT_BACKUP_SIZE/
            # percent at a stale value for the rest of a potentially
            # hours-long run.
            size_bytes = get_folder_size_bytes_du(new_snapshot_path)
            source_total = baselines['source_total']
            # Both gated on the same condition, updated together — size_bytes
            # not None on its own used to be enough to update
            # CURRENT_BACKUP_SIZE, leaving BACKUP_PROGRESS_PERCENT stuck at
            # whatever it was if source_total's own du scan (a separate,
            # independently-timed background thread) hadn't finished yet.
            # That gap let the dashboard show a freshly-updated size next to
            # a stale, unrelated percent for as long as source_total lagged.
            if size_bytes is not None and source_total:
                set_database_value('CURRENT_BACKUP_SIZE', human_readable_size(size_bytes))
                baselines['dest_baseline'] = size_bytes
                baselines['transferred_offset'] = baselines['last_transferred']
                set_database_value('BACKUP_PROGRESS_PERCENT', str(min(100, round(size_bytes / source_total * 100))))
            stop_size_refresh.wait(60)

    threading.Thread(target=_refresh_current_size_periodically, daemon=True).start()

    rsync_result = {'success': None}
    last_percent = None
    last_eta = None
    last_size_str = None
    for progress in rsync(new_snapshot_path, link_dest=previous_snapshot, result_holder=rsync_result):
        # Groups: bytes-transferred-so-far, rsync's own percent, ETA
        # (time *remaining* — verified empirically with a throttled
        # transfer that it counts down to 0:00:00, not up).
        match = re.search(r'^([\d,]+)\s+(\d+)%\s+\S+\s+(\d+:\d+:\d+)', progress)
        if not match:
            continue
        transferred_bytes = int(match.group(1).replace(',', ''))
        baselines['last_transferred'] = transferred_bytes
        eta = match.group(3)
        dest_baseline = baselines['dest_baseline']
        source_total = baselines['source_total']
        if dest_baseline is not None and source_total:
            current_total = dest_baseline + (transferred_bytes - baselines['transferred_offset'])
            percent = min(100, round(current_total / source_total * 100))
            # Was unconditional on every progress line -- with
            # --info=progress2 emitting potentially 1000+ lines/sec on a
            # fast transfer, that meant 1000+ SQLite connect+write+commit
            # (fsync) cycles per second purely for Python-side bookkeeping,
            # real backpressure on the pipe rsync writes progress to. Found
            # live: the wrapper process using MORE CPU than rsync itself
            # (18.6% vs a combined ~10% across rsync's own processes), and
            # a run measurably slower than Timeshift's bare rsync doing a
            # comparable job over the same disk. human_readable_size's
            # rounding means the string is unchanged across most updates
            # anyway, so this only writes when it's actually new information
            # -- same pattern already used for percent/eta right below.
            size_str = human_readable_size(current_total)
            if size_str != last_size_str:
                last_size_str = size_str
                set_database_value('CURRENT_BACKUP_SIZE', size_str)
            percent_str = str(percent)
        else:
            # Baselines not ready yet (source_total's du scan alone took 45s
            # on one observed run). rsync's own percentage reflects only
            # whatever small subset of files incremental recursion has
            # discovered so far, not real progress against the whole
            # backup — verified live: it climbed past 60% while barely 3%
            # of the real, much larger total had actually transferred.
            # Showing that number as our own percent isn't just imprecise,
            # it's actively misleading (a confident-looking wrong answer).
            # Leave it unset instead — the frontend already falls back to
            # an honest indeterminate animation when percent is blank.
            percent_str = None
        if percent_str != last_percent:
            last_percent = percent_str
            set_database_value('BACKUP_PROGRESS_PERCENT', percent_str or '')
        if eta != last_eta:
            last_eta = eta
            set_database_value('BACKUP_ETA', eta)
    set_database_value('BACKUP_PROGRESS_PERCENT', '')
    set_database_value('BACKUP_ETA', '')
    set_database_value('BACKUP_START_TIME', '')
    # record_backup_statistics() below does its own final accurate du scan,
    # so this periodic one has nothing left to correct — stop it here
    # rather than leaving it running (and holding the process open on its
    # 30s wait) through the rest of this script's work.
    stop_size_refresh.set()

    if rsync_result['success'] is False:
        # rsync itself failed — repointing full_backup or recording stats
        # against a failed/partial sync would misrepresent it as a
        # completed backup. full_backup is left untouched (still pointing
        # at the last good snapshot); new_snapshot_path is left in place
        # without a completion marker, so the restore/cleanup side ignores
        # it as a partial run rather than treating it as valid. Exit
        # non-zero so main_backup.py's subprocess.run(check=True) sees it
        # as a failure and notifies.
        logger.error("Backup failed — skipping snapshot bookkeeping.")
        sys.exit(1)

    # Mark the snapshot complete and atomically repoint full_backup to it
    # BEFORE any further bookkeeping — so a later failure in stats/
    # notification can't leave full_backup stale relative to a snapshot
    # that actually finished successfully.
    with open(os.path.join(new_snapshot_path, COMPLETION_MARKER), 'w') as f:
        f.write(datetime.now().isoformat())
    _repoint_full_backup(new_snapshot_path)
    # Only safe to call now that full_backup exists as a real symlink --
    # on the very first-ever backup, it doesn't exist until the repoint
    # above just happened.
    ensure_backup_folder_icon()

    changes = parse_logfile(rsync_txt)
    store_changes_in_db(changes)
    last_session_number = get_last_session_number(database)
    record_backup_statistics(changes, last_session_number, incremental_folder)
    cleanup_old_snapshots(load_env_value('MAX_SNAPSHOTS'))

    if last_session_number <= 1:
        notify("FolderW: Full Backup Complete", f"The initial full backup to {full_backup} finished successfully.")
        # Lets main_backup.py skip straight to monitoring/scheduling on a
        # future restart instead of re-running the initial full backup --
        # cleared by reset_backup_history() if SRC_DIR/BASE_DIR/FULL_NAME
        # ever changes, and by the dashboard's Start Full Backup button
        # (which should always force a real run regardless of this flag).
        set_database_value('FULL_BACKUP_COMPLETED', '1')
