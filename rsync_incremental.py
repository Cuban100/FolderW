import os
import re
import sys
import time
import threading
import pwd
import grp
from db_operations import get_last_session_number, store_changes_in_db, load_other_variables, load_env_value, record_backup_run, set_database_value, get_database_value
from restore_operations import cleanup_old_snapshots, mark_snapshot_complete, COMPLETION_MARKER
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

# This process's own real user/group (not root -- rsync itself runs
# under sudo so it can read files owned by another UID, e.g. a Docker
# container's bind-mounted config, but the copies it WRITES should
# always end up owned by whoever actually runs FolderW, with a fixed
# permission mode, regardless of the source file's own owner/mode).
# os.getuid()/getgid() here reflect this script's own (non-root) process,
# not the elevated rsync subprocess it launches below.
OWNER_USER = pwd.getpwuid(os.getuid()).pw_name
OWNER_GROUP = grp.getgrgid(os.getgid()).gr_name



TOTAL_SIZE_RE = re.compile(r'^total size is ([\d,]+)\s+speedup')


def _parse_rsync_total_size(rsync_txt):
    """rsync's own closing summary line ("total size is X  speedup is Y")
    already reports the exact number record_backup_statistics() below
    needs for CURRENT_BACKUP_SIZE -- the sum of every file rsync's file
    list considered (already exclude-aware, since --exclude-from removes
    those before this sum is computed), as a natural byproduct of the
    transfer that just ran. Reading it here avoids a second, redundant
    full `du` scan of the multi-hundred-thousand-file tree rsync just
    spent minutes building. Only available in the closing summary (not
    knowable earlier -- see the -vv/--out-format comments on incremental
    recursion in rsync() above), so this can't help with the *live*
    in-progress percentage, only this final, post-completion number.
    """
    try:
        with open(rsync_txt, 'r') as f:
            for line in f:
                match = TOTAL_SIZE_RE.match(line.strip())
                if match:
                    return int(match.group(1).replace(',', ''))
    except OSError:
        pass
    return None


def record_backup_statistics(changes, last_session_number, incremental_folder, backup_type):
    """Record that this backup run happened — the initial full backup, an
    incremental snapshot, or a differential snapshot — plus per-run
    numeric stats. Recording the run itself matters even when 0 files
    changed, which is why this is separate from (and unconditional on)
    the numeric stats below. Deliberately doesn't re-walk the whole
    source tree (could be huge, e.g. a home directory), so this stays
    cheap on every run: only sums the sizes of files that actually
    changed this session.

    backup_type: 'full' (the one-time initial backup), 'incremental', or
    'differential' -- passed explicitly by the caller rather than
    inferred from last_session_number, since session number alone can no
    longer distinguish incremental from differential (both start at
    session 2).
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

    # label is the real snapshot folder id (e.g. "August/08/2:12-AM") for
    # incremental/differential runs, or the literal "Full Backup" for the
    # one-time initial backup (matching what list_backups()'s synthetic
    # "full" entry looks itself up by). Kept consistent so the Restore
    # page can look up a snapshot's changed-file count by matching this
    # label against its own id.
    record_backup_run(last_session_number, backup_type, incremental_folder, total_files_processed)

    # Cached here (once per backup run) rather than computed on every
    # dashboard page load. Prefer rsync's own "total size is X" summary
    # line (see _parse_rsync_total_size) over a fresh `du` scan -- rsync
    # already computed this exact number as part of the transfer that just
    # finished, so re-walking the tree a second time to ask the same
    # question again is pure waste. Falls back to `du` on full_backup
    # (a real directory -- get_folder_size_du/-D dereferences correctly
    # even though the -D dereference itself is a no-op for a non-symlink)
    # only if the summary line wasn't found for some reason.
    total_size_bytes = _parse_rsync_total_size(rsync_txt)
    current_size = human_readable_size(total_size_bytes) if total_size_bytes is not None else get_folder_size_du(full_backup)
    if current_size:
        set_database_value('CURRENT_BACKUP_SIZE', current_size)

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
        notify("FolderW: Backup Failed", message, level='critical')
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


def _most_recent_snapshot_path():
    """The --link-dest source for incremental mode: the most recently
    completed Month/Day/Time snapshot under snapshots_root, or None if
    none exist yet (the very first incremental run after the initial
    full backup -- caller falls back to full_backup itself in that case,
    see __main__ below).

    Walks snapshots_root directly rather than going through full_backup
    (which no longer means "the latest snapshot" -- it's the one-time,
    frozen initial backup now, see rsync_differential.py's design notes
    in __main__). Only considers folders carrying COMPLETION_MARKER, same
    filter restore_operations.list_backups() applies, so a partial/
    interrupted snapshot never becomes the reference for the next run.
    """
    if not os.path.isdir(snapshots_root):
        return None
    candidates = []
    for month in os.listdir(snapshots_root):
        month_path = os.path.join(snapshots_root, month)
        if not os.path.isdir(month_path):
            continue
        for day in os.listdir(month_path):
            day_path = os.path.join(month_path, day)
            if not os.path.isdir(day_path):
                continue
            for time_name in os.listdir(day_path):
                snap_path = os.path.join(day_path, time_name)
                if os.path.isdir(snap_path) and os.path.exists(os.path.join(snap_path, COMPLETION_MARKER)):
                    candidates.append(snap_path)
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def _migrate_legacy_full_backup():
    """One-time normalization for installs from before this design, where
    full_backup was either a real, continuously-synced mirror directory,
    a symlink to the newest snapshot, or a hardlinked mirror rebuilt
    every run -- all of which treated full_backup as something that
    keeps moving/updating. In the current design it's the opposite: a
    one-time, frozen backup, created once and never touched again, that
    every differential snapshot is measured against forever.

    Whatever full_backup currently is when this first runs under the new
    code becomes that frozen baseline, as-is -- a symlink gets resolved
    and replaced with a real copy of its target (a fast, same-filesystem
    hardlink clone, not a re-transfer); a real directory (from either
    older design) is left in place and just marked complete/normalized.
    This is a one-time acknowledgment that whatever content was most
    recently "the full backup" under the previous design becomes the
    fixed original going forward -- there's no way to recover an even
    older true original if an intermediate design already overwrote it.

    Safe to call on every startup: no-ops immediately if full_backup
    already carries COMPLETION_MARKER (already normalized) or doesn't
    exist at all (fresh install, nothing to migrate -- the normal
    __main__ flow below creates it as a real initial full backup).
    """
    if os.path.isdir(full_backup) and os.path.exists(os.path.join(full_backup, COMPLETION_MARKER)):
        return
    if os.path.islink(full_backup):
        target = os.path.realpath(full_backup)
        if not os.path.isdir(target):
            logger.error(
                f"full_backup ({full_backup}) is a symlink to a missing target "
                f"({target}) -- can't migrate it. Remove the symlink manually if "
                "starting fresh is intended."
            )
            sys.exit(1)
        os.remove(full_backup)
        shutil.copytree(target, full_backup, copy_function=os.link, symlinks=True)
        logger.warning(f"Migrated full_backup from a symlink (target: {target}) to a real, frozen directory.")
    elif os.path.isdir(full_backup):
        logger.warning(f"Normalizing existing full_backup directory as the frozen baseline: {full_backup}")
    else:
        if get_database_value('FULL_BACKUP_COMPLETED', 'settings') == '1':
            # full_backup missing but a completed backup was recorded --
            # something is inconsistent (manual deletion, interrupted
            # migration). Do NOT silently fall through to a fresh full
            # backup transfer -- surface this loudly instead of guessing.
            logger.error(
                "full_backup is missing but FULL_BACKUP_COMPLETED is set -- "
                "refusing to silently start a fresh full backup. Restore "
                "full_backup manually, or clear FULL_BACKUP_COMPLETED if a "
                "fresh backup is really intended."
            )
            sys.exit(1)
        return
    mark_snapshot_complete(full_backup)
    # Keep the DB flag consistent with the filesystem marker -- other
    # code (main_backup.py's skip-the-full-backup check, dashboard
    # status) reads this flag directly rather than checking the
    # filesystem itself.
    set_database_value('FULL_BACKUP_COMPLETED', '1')

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
    # full_backup is a real, one-time, frozen directory (see
    # _run_initial_full_backup_if_needed/_migrate_legacy_full_backup) --
    # never a live rsync destination after that initial run, so it's
    # safe to brand normally with physical files, same as its container
    # folder. This also gets the KDE Dolphin .directory-file fallback
    # that a symlink couldn't have, and is more reliable than gio-
    # metadata-only tagging (confirmed live: a symlink-only tag from an
    # earlier design wasn't rendering as a custom icon in the file
    # manager actually used).
    #
    # FolderW.png for Full Backup itself (the author's own 3D folder-
    # with-badge render, already styled as a folder); folder-icon.png (a
    # flatter pre-rendered folder with the badge embedded, also the
    # author's own asset) for its container folder. gio's metadata::
    # custom-icon replaces the folder glyph entirely with whatever image
    # is given -- it doesn't composite either onto a folder shape
    # automatically, which is why both assets are already pre-rendered
    # to look like one instead of a floating badge.
    _set_folder_icon(full_backup, 'FolderW.png')
    _set_folder_icon(os.path.dirname(full_backup), 'folder-icon.png')

def rsync(destination, link_dest=None, compare_dest=None, result_holder=None):
    # destination: the path this run writes into -- full_backup itself on
    # the very first-ever run, otherwise a new dated Snapshots/ folder.
    # link_dest: incremental mode's reference (the most recent snapshot,
    # or full_backup if none exist yet) -- unchanged files become instant
    # hardlinks, so destination ends up a complete, space-efficient
    # point-in-time tree. compare_dest: differential mode's reference
    # (always full_backup, the one-time frozen original) -- unchanged
    # files are simply skipped, not hardlinked, so destination ends up
    # containing ONLY what's new or changed since that original -- a true
    # delta, not a complete tree (see rsync's own distinction between
    # these two flags; --link-dest and --compare-dest are mutually
    # exclusive, only one is ever passed by a caller). Both None on the
    # very first-ever backup (nothing exists yet to compare against).
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
    # --link-dest=<reference> (incremental mode): the Timeshift-style
    # mechanism -- unchanged files (matching by quick-check against the
    # reference) become instant hardlinks instead of being re-transferred,
    # so destination is a complete, space-efficient point-in-time tree
    # every run, not just a delta.
    # --compare-dest=<reference> (differential mode): unchanged files are
    # simply skipped -- not hardlinked, not present at all in destination
    # -- so destination ends up containing only what's new or changed
    # since the reference. A true delta, matching the industry-standard
    # definition of "differential backup" (confirmed against Wikipedia/
    # Acronis/Redstor -- restoring needs only the full backup + the
    # latest differential, not a hardlink-complete tree per snapshot).
    # Both omitted on the first-ever backup (nothing exists yet to
    # compare against).
    # --delete-excluded (not just --delete): matches Timeshift's actual
    # command. Safe now specifically because the icon files are no longer
    # written into this rsync-managed tree at all (see _set_folder_icon),
    # so nothing excluded needs protecting from active deletion.
    # --chown/--chmod: -a (via sudo, root) would otherwise preserve each
    # source file's own owner/group/permissions verbatim -- exactly what
    # lets rsync read another UID's files in the first place, but not
    # what should land in the backup itself. Every copy should come out
    # owned by whoever actually runs FolderW (not root, and not whatever
    # UID happened to own it at the source, e.g. a Docker container's UID
    # 2000) with a fixed, predictable permission mode, regardless of the
    # source's own mode. Both apply after -a's normal owner/group/perms
    # handling, overriding just those, not the rest of -a (mtimes,
    # symlinks, recursion, etc.).
    rsync_command = ["sudo", "-n", "rsync", "-avv", "--sparse", "--delete", "--delete-excluded", "--info=progress2", "--out-format=CHANGED:%n", f'--exclude-from={exclude_file}', f'--chown={OWNER_USER}:{OWNER_GROUP}', '--chmod=775']
    if link_dest:
        rsync_command.append(f'--link-dest={link_dest}')
    elif compare_dest:
        rsync_command.append(f'--compare-dest={compare_dest}')
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


def _run_initial_full_backup_if_needed():
    """If full_backup doesn't exist yet or isn't marked complete (the
    very first backup ever, regardless of which BACKUP_METHOD is
    configured), runs it as a direct, complete rsync transfer straight
    into full_backup itself -- no --link-dest, no --compare-dest,
    nothing exists yet to compare against. Marks it complete, sets
    FULL_BACKUP_COMPLETED, notifies, and returns True.

    Returns False (does nothing else) if full_backup is already a
    complete, frozen backup -- the caller should proceed with its own
    mode-specific snapshot logic in that case.

    Shared between rsync_incremental.py and rsync_differential.py's
    __main__ blocks (imported by the latter) so whichever mode happens
    to run first produces the exact same one-time full backup -- the
    mode choice only affects every run after this one.
    """
    if os.path.exists(os.path.join(full_backup, COMPLETION_MARKER)):
        return False

    os.makedirs(full_backup, exist_ok=True)
    logger.info(f"Initial full backup destination: {full_backup}")

    set_database_value('BACKUP_PROGRESS_PERCENT', '')
    set_database_value('BACKUP_ETA', '')
    set_database_value('CURRENT_BACKUP_SIZE', 'Calculating…')
    set_database_value('BACKUP_PREPARING', '')
    set_database_value('BACKUP_START_TIME', str(time.time()))

    baselines = {'source_total': None, 'last_transferred': 0}

    def _compute_source_total():
        baselines['source_total'] = get_folder_size_bytes_du(src_dir, exclude_from=exclude_file)
        logger.info(f"Source total size: {baselines['source_total']}")
        if baselines['source_total'] is not None:
            set_database_value('LAST_SRC_SIZE', human_readable_size(baselines['source_total']))

    threading.Thread(target=_compute_source_total, daemon=True).start()

    rsync_result = {'success': None}
    last_percent = None
    last_eta = None
    DB_WRITE_MIN_INTERVAL = 2
    last_db_write_time = 0.0
    for progress in rsync(full_backup, result_holder=rsync_result):
        match = re.search(r'^([\d,]+)\s+(\d+)%\s+\S+\s+(\d+:\d+:\d+)', progress)
        if not match:
            continue
        transferred_bytes = int(match.group(1).replace(',', ''))
        baselines['last_transferred'] = transferred_bytes
        eta = match.group(3)
        source_total = baselines['source_total']
        # No dest_baseline needed here (unlike the incremental/
        # differential loop below) -- destination starts genuinely
        # empty, nothing is "already there for free" on the very first
        # backup, so transferred/source_total alone is the right ratio.
        percent_str = str(min(100, round(transferred_bytes / source_total * 100))) if source_total else None
        now_ts = time.time()
        if (percent_str != last_percent or eta != last_eta) and now_ts - last_db_write_time >= DB_WRITE_MIN_INTERVAL:
            last_db_write_time = now_ts
            if percent_str != last_percent:
                last_percent = percent_str
                set_database_value('BACKUP_PROGRESS_PERCENT', percent_str or '')
            if eta != last_eta:
                last_eta = eta
                set_database_value('BACKUP_ETA', eta)
    set_database_value('BACKUP_PROGRESS_PERCENT', '')
    set_database_value('BACKUP_ETA', '')
    set_database_value('BACKUP_START_TIME', '')

    if rsync_result['success'] is False:
        logger.error("Initial full backup failed — leaving full_backup unmarked (incomplete).")
        sys.exit(1)

    mark_snapshot_complete(full_backup)
    ensure_backup_folder_icon()

    changes = parse_logfile(rsync_txt)
    store_changes_in_db(changes)
    record_backup_statistics(changes, 1, "Full Backup", backup_type='full')

    notify("FolderW: Full Backup Complete", f"The initial full backup to {full_backup} finished successfully.", level='normal')
    # Lets main_backup.py skip straight to monitoring/scheduling on a
    # future restart instead of re-running the initial full backup --
    # cleared by reset_backup_history() if SRC_DIR/BASE_DIR/FULL_NAME ever
    # changes, and by the dashboard's Start Full Backup button (which
    # should always force a real run regardless of this flag).
    set_database_value('FULL_BACKUP_COMPLETED', '1')
    return True



if __name__ == "__main__":
    # First thing, before any other work: if this is going to fail because
    # sudo isn't configured, fail now with a specific, actionable message
    # rather than after migration/baseline scans/etc. have already run.
    _check_sudo_rsync_available()

    # One-time (idempotent, cheap to re-check every run) normalization for
    # installs from before this design -- see the function's own docstring.
    _migrate_legacy_full_backup()

    # The initial full backup, regardless of which BACKUP_METHOD is
    # configured -- every install's very first run goes straight into
    # full_backup itself, not a dated snapshot (see the function's own
    # docstring). Every run after this one is incremental, since this
    # script only runs when BACKUP_METHOD == incremental.
    if _run_initial_full_backup_if_needed():
        sys.exit(0)

    # Most recent completed snapshot under Snapshots/, or full_backup
    # itself if none exist yet (the first incremental run after the
    # initial full backup chains directly against it). Captured now,
    # before this run's rsync touches anything.
    previous_snapshot = _most_recent_snapshot_path() or full_backup

    # Computed once and reused everywhere below (the rsync destination
    # and record_backup_statistics()'s label) -- generate_incremental_
    # folder() uses datetime.now(), so calling it more than once in the
    # same run risks two different folder names if the run straddles a
    # minute boundary.
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

    # Cleared up front so a stale percentage/ETA from a previous run can't
    # be shown on the dashboard during the icon-setup gap before rsync's
    # first progress line actually arrives.
    set_database_value('BACKUP_PROGRESS_PERCENT', '')
    set_database_value('BACKUP_ETA', '')
    # Set once, here, rather than live-updated per progress line: without
    # the periodic du-based re-anchoring (removed -- see the comment above
    # the main progress loop), the running estimate has no correction for
    # the rest of a long run and can drift into a number that looks
    # precise but isn't. Better to say plainly that it isn't known yet
    # than show a value that might be wrong. The real number appears the
    # moment the run finishes (record_backup_statistics, sourced from
    # rsync's own closing summary, not an estimate).
    set_database_value('CURRENT_BACKUP_SIZE', 'Calculating…')
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
    # dest_baseline was measured. The live per-progress-line estimate is
    # dest_baseline + (transferred_bytes - transferred_offset) -- anchored
    # once, at the start, not re-corrected during the run (see the comment
    # below on why periodic `du`-based re-anchoring was removed).
    baselines = {'dest_baseline': None, 'source_total': None, 'transferred_offset': 0, 'last_transferred': 0}

    def _compute_dest_baseline():
        # The *previous* snapshot, not the new (currently empty) one: at
        # t=0, new_snapshot_path has nothing in it yet -- previous_snapshot
        # is the correct "what's already effectively there" anchor, since
        # --link-dest will hardlink most of it into the new snapshot at
        # near-zero transfer cost.
        size = get_folder_size_bytes_du(previous_snapshot)
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
    # immediate, but it double-counts modified files (the old version's
    # bytes are already in the baseline, and the new version's size gets
    # added on top too) and has no idea about deletions (--delete is on).
    # No periodic `du`-based re-anchoring anymore: `du` runs unprivileged
    # here, and turns out to be not just slow on a tree this size but
    # actually wrong -- found live, permission-denied on every Docker-
    # container-owned directory (WireGuard peer configs, MySQL/Postgres
    # data dirs), silently undercounting by several GB. Correcting toward
    # a periodically-wrong number isn't a real correction, and re-scanning
    # every 60s competed with the live transfer for disk I/O regardless.
    # The live number is now a running estimate that can drift somewhat on
    # a very long run; the final CURRENT_BACKUP_SIZE (record_backup_
    # statistics, below) is accurate regardless, sourced from rsync's own
    # closing summary rather than `du` at all.
    rsync_result = {'success': None}
    last_percent = None
    last_eta = None
    # rsync's ETA counts down every second, so it changes on nearly every
    # progress line -- without a time floor, this loop was writing to
    # folderw.db (which lives inside the watched source tree, since it's
    # just a relative path resolved under the app's own working directory)
    # thousands of times per run. Each write's journal file registered as
    # a "file changed" event to the watchdog, which rescheduled another
    # backup shortly after -- a self-sustaining loop that kept firing new
    # backups all night with zero real user activity. Capping DB writes to
    # once every 2s breaks that loop while keeping the dashboard responsive.
    DB_WRITE_MIN_INTERVAL = 2
    last_db_write_time = 0.0
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
            # CURRENT_BACKUP_SIZE itself is no longer updated here (see the
            # 'Calculating…' comment above) -- current_total is still
            # needed for percent, which is a self-correcting ratio (both
            # sides drift together) even though the absolute byte count
            # underneath it isn't perfectly anchored for the rest of a
            # long run.
            current_total = dest_baseline + (transferred_bytes - baselines['transferred_offset'])
            percent = min(100, round(current_total / source_total * 100))
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
        now_ts = time.time()
        if (percent_str != last_percent or eta != last_eta) and now_ts - last_db_write_time >= DB_WRITE_MIN_INTERVAL:
            last_db_write_time = now_ts
            if percent_str != last_percent:
                last_percent = percent_str
                set_database_value('BACKUP_PROGRESS_PERCENT', percent_str or '')
            if eta != last_eta:
                last_eta = eta
                set_database_value('BACKUP_ETA', eta)
    set_database_value('BACKUP_PROGRESS_PERCENT', '')
    set_database_value('BACKUP_ETA', '')
    set_database_value('BACKUP_START_TIME', '')

    if rsync_result['success'] is False:
        # rsync itself failed — recording stats against a failed/partial
        # sync would misrepresent it as a completed backup. new_snapshot_
        # path is left in place without a completion marker, so the
        # restore/cleanup side ignores it as a partial run rather than
        # treating it as valid. Exit non-zero so main_backup.py's
        # subprocess.run(check=True) sees it as a failure and notifies.
        logger.error("Backup failed — skipping snapshot bookkeeping.")
        sys.exit(1)

    mark_snapshot_complete(new_snapshot_path)
    ensure_backup_folder_icon()

    changes = parse_logfile(rsync_txt)
    store_changes_in_db(changes)
    last_session_number = get_last_session_number(database)
    record_backup_statistics(changes, last_session_number, incremental_folder, backup_type='incremental')
    cleanup_old_snapshots(load_env_value('MAX_SNAPSHOTS'))
