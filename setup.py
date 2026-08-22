import os
import shutil
import subprocess
import tempfile
from db_operations import create_all_tables, set_database_value, reset_backup_history
from auth import hash_password
from cloud_backup import create_remote, KNOWN_REMOTE_TYPES
import tkinter as tk
from tkinter import filedialog, Label, PhotoImage, messagebox
from tkinter import ttk, StringVar
from ttkthemes import ThemedTk
from PIL import Image, ImageTk
from dotenv import load_dotenv, set_key, find_dotenv
import sys

class Logger:
    def __init__(self, text_widget):
        self.text_widget = text_widget

    def write(self, message):
        # Insert the message into the Text widget with the "neon_green" tag
        self.text_widget.insert(tk.END, message, "neon_green")
        self.text_widget.yview(tk.END)  # Auto-scroll to the end
        self.text_widget.update_idletasks()  # Force update/redraw

    def flush(self):
        pass  # No need to do anything on flush

def upgrade_packages(requirements_file='requirements.txt'):
    print(f"Installing pinned versions from {requirements_file}\n")
    process = subprocess.Popen(
        ["pip", "install", "-r", requirements_file],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    for line in process.stdout:
        print(line, end="")
    process.wait()

DIFFERENTIAL_EXPLANATION = (
    "Differential Backup (default)\n\n"
    "One initial full backup is made once, then frozen forever. Every "
    "snapshot after that compares the live source directly against that "
    "original full backup and saves ONLY the files that are new or "
    "changed since it -- not a complete copy of everything.\n\n"
    "Snapshots grow larger over time as more changes accumulate since "
    "the original. Restoring a specific point in time needs the full "
    "backup plus that one differential snapshot together, not the whole "
    "history -- the standard definition of a differential backup."
)

INCREMENTAL_EXPLANATION = (
    "Incremental Backup\n\n"
    "One initial full backup is made once. Every snapshot after that "
    "compares against the PREVIOUS snapshot (not the original) and is a "
    "complete, independently restorable copy of everything -- unchanged "
    "files are hardlinked in at no extra disk cost, changed files are "
    "written fresh.\n\n"
    "Snapshots stay small and roughly constant in size no matter how "
    "long the system has been running, and any single snapshot can be "
    "restored on its own. This is how Time Machine and Timeshift work."
)

def _show_backup_method_info(mode):
    text = DIFFERENTIAL_EXPLANATION if mode == 'differential' else INCREMENTAL_EXPLANATION
    messagebox.showinfo("Backup Method", text)

def _is_valid_folder_name(name):
    # Linux (the only supported platform) only actually forbids '/' and
    # the null byte in a filename/directory name -- everything else
    # (spaces, dots, unicode, etc.) is technically legal at the
    # filesystem level. '.' and '..' are reserved (they mean "this
    # directory" and "parent directory", not a real name). This doesn't
    # try to guess at "looks wrong" patterns like a .db extension --
    # that's not actually invalid for a directory name, just confusing
    # if it ended up there by mistake (a different, already-covered
    # concern: the Database field's own .db-required check).
    return bool(name) and name not in ('.', '..') and '/' not in name and '\x00' not in name

def toggle_backup_options():
    if monitor_var.get() == 1:
        for widget in interval_widgets:
            widget.grid_remove()
    else:
        for widget in interval_widgets:
            widget.grid()

SERVICE_NAME = "folderw.service"
BACKUP_SERVICE_NAME = "folderw-backup.service"
APP_DIR = os.path.dirname(os.path.abspath(__file__))

def configure_systemd_autostart(enable, mount_point=None):
    service_dir = os.path.join(os.path.expanduser("~"), ".config", "systemd", "user")
    service_path = os.path.join(service_dir, SERVICE_NAME)
    backup_service_path = os.path.join(service_dir, BACKUP_SERVICE_NAME)

    # Runs before ExecStart on every start (not just at boot -- cheap,
    # idempotent, and correct regardless of whether this particular
    # start actually needed it). Confirmed live, more than once: a
    # mount holding BASE_DIR (typically an external/USB-labeled drive)
    # can come up noexec at boot despite fstab saying exec -- something
    # in this machine's desktop mount stack doesn't reliably honor it,
    # even after also adding a udisks2 mount_options.conf override.
    # noexec breaks a Python venv's compiled .so files outright
    # (ImportError: failed to map segment from shared object),
    # crash-looping this very service. Rather than keep chasing the
    # exact upstream cause, defend against the symptom that actually
    # matters: this service simply cannot start correctly on a noexec
    # BASE_DIR, so make sure it isn't, every time, before trying.
    # Leading "-": don't fail the whole unit start if this specific
    # command fails for some unrelated reason (mount_exec_sudo wasn't
    # configured, the drive isn't mounted yet despite nofail, etc.) --
    # same convention already used for ExecStopPost below.
    exec_start_pre = ""
    if mount_point:
        sudo_path = shutil.which("sudo") or "/usr/bin/sudo"
        mount_path = shutil.which("mount") or "/usr/bin/mount"
        exec_start_pre = f"ExecStartPre=-{sudo_path} -n {mount_path} -o remount,exec {mount_point}\n"

    if enable:
        print(f"Configuring systemd to start FolderW at boot ({service_path})")
        # Two separate units, deliberately not tied together in the same
        # cgroup: this unit only ever runs server.py (the dashboard), so a
        # routine restart (e.g. from update.sh after every code update)
        # can't touch an active backup — and folderw-backup.service (below)
        # can be stopped/restarted on its own with normal systemd semantics
        # (default KillMode=control-group), cleanly killing main_backup.py
        # and everything it spawned (rsync_event_handler.py, rsync itself)
        # with no lingering processes.
        service_content = (
            "[Unit]\n"
            "Description=FolderW Backup Dashboard\n"
            "After=network.target\n\n"
            "[Service]\n"
            "Type=simple\n"
            f"WorkingDirectory={APP_DIR}\n"
            f"{exec_start_pre}"
            f"ExecStart={sys.executable} {os.path.join(APP_DIR, 'server.py')}\n"
            "Restart=on-failure\n"
            "RestartSec=5\n\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )
        # Enabled/started on boot too, same as the dashboard above -- a
        # reboot used to silently stop all automated backups (scheduled
        # AND watchdog) until this was clicked/re-triggered by hand, which
        # didn't match what "Autostart on Boot" actually promises. Found
        # live: after a restart, folderw.service (dashboard) came back on
        # its own, but folderw-backup.service stayed dead until manually
        # started, with the watchdog widget quietly showing stale state.
        # server.py can still start/stop/restart it on demand independent
        # of this -- enabling it here just means systemd also brings it
        # back up on boot/login, not that this is its only start path.
        backup_service_content = (
            "[Unit]\n"
            "Description=FolderW Backup Worker\n"
            "After=network.target\n\n"
            "[Service]\n"
            "Type=simple\n"
            f"WorkingDirectory={APP_DIR}\n"
            f"{exec_start_pre}"
            f"ExecStart={sys.executable} {os.path.join(APP_DIR, 'main_backup.py')}\n"
            "Restart=on-failure\n"
            "RestartSec=10\n"
            # Default (90s) meant Stop Full Backup could block for a minute
            # and a half waiting on rsync mid-transfer before systemd gave
            # up and SIGKILLed it — the dashboard's stop request blocks on
            # this same wait. 15s is enough for a normal graceful exit
            # (finish the current write, clean up its temp file) without
            # leaving a stop request hanging for so long it looks broken.
            "TimeoutStopSec=15\n"
            # rsync now runs under sudo (see rsync_incremental.py) so it can
            # read files owned by another UID -- but that means the actual
            # rsync process is root-owned, and this is a --user service:
            # this user's own systemd session has no permission to signal a
            # root-owned process at all, cgroup membership notwithstanding.
            # Found live: KillMode=control-group's own SIGKILL attempt on
            # stop failed with "Operation not permitted", the service
            # reported itself Stopped anyway, and the actual rsync process
            # kept running regardless -- exactly what "I hit stop and it's
            # still running" looks like. sudo (already NOPASSWD for rsync
            # specifically) can signal it instead. The leading "-" tells
            # systemd to ignore this command's exit code, so "nothing to
            # kill" (the common case -- rsync usually already exited
            # cleanly on its own SIGTERM) isn't logged as a failure.
            f"ExecStopPost=-{shutil.which('sudo') or '/usr/bin/sudo'} -n pkill -9 -f 'rsync -avv --sparse --delete --info=progress2'\n\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )
        try:
            os.makedirs(service_dir, exist_ok=True)
            with open(service_path, "w") as f:
                f.write(service_content)
            with open(backup_service_path, "w") as f:
                f.write(backup_service_content)
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
            subprocess.run(["systemctl", "--user", "enable", SERVICE_NAME], check=True)
            subprocess.run(["systemctl", "--user", "enable", BACKUP_SERVICE_NAME], check=True)
            print("Autostart enabled. FolderW (dashboard + backup worker) will start automatically on login/boot.")
            linger = subprocess.run(
                ["loginctl", "enable-linger", os.environ.get("USER", "")],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            if linger.returncode != 0:
                print("Note: could not enable linger automatically. If FolderW doesn't start "
                      "before you log in, run: loginctl enable-linger $USER")
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            print(f"Could not configure systemd autostart: {e}")
    else:
        if os.path.exists(service_path):
            print("Disabling systemd autostart")
            try:
                subprocess.run(["systemctl", "--user", "disable", SERVICE_NAME], check=True)
            except (subprocess.CalledProcessError, FileNotFoundError) as e:
                print(f"Could not disable systemd autostart: {e}")
        if os.path.exists(backup_service_path):
            try:
                subprocess.run(["systemctl", "--user", "disable", BACKUP_SERVICE_NAME], check=True)
            except (subprocess.CalledProcessError, FileNotFoundError) as e:
                print(f"Could not disable backup worker autostart: {e}")

RSYNC_SUDOERS_DROPIN = "/etc/sudoers.d/folderw-rsync"

def configure_rsync_sudo():
    """Grants NOPASSWD sudo for rsync specifically, so a backup triggered
    without an interactive terminal (the watchdog, a scheduled interval, a
    plain systemd restart) can still read files owned by another UID --
    e.g. a Docker container writing into a bind-mounted config directory
    under its own internal user, unreadable by this account otherwise
    (found the hard way: WireGuard peer configs owned by UID 2000, 700/600
    permissions). Without this, rsync just can't read those files -- a
    permission error it handles fine on its own (logs a warning, moves on),
    but the backup silently misses that data either way.

    Skipped if some NOPASSWD rule already covers rsync (e.g. a broader
    NOPASSWD: ALL the user already has for other reasons) -- no need to add
    a redundant rule on top of one that already works. This is what makes
    the backup behave the same way for every install, rather than only
    working around permission walls on machines that happen to already
    have broad passwordless sudo configured for unrelated reasons.
    """
    rsync_path = shutil.which("rsync")
    if not rsync_path:
        # install.sh checks for/installs rsync before this ever runs, but
        # setup.py can also be run standalone (venv already created by
        # hand, re-running just the wizard, etc.) -- without this, the
        # fallback below used to silently write a sudoers rule for a
        # binary that might not exist, with no indication rsync itself
        # was the actual problem. Every real backup would still fail
        # later regardless of what this function does, so surface it now.
        print("WARNING: rsync was not found on this system -- FolderW's backups will not work "
              "until it's installed (e.g. 'sudo apt install rsync', or the equivalent for your "
              "distro). Skipping rsync sudo setup for now; re-run setup.py after installing it.")
        return
    username = os.environ.get("SUDO_USER") or os.environ.get("USER") or os.environ.get("LOGNAME")
    manual_hint = (f"To let backups read files owned by another user, add manually: "
                    f"echo '<username> ALL=(root) NOPASSWD: {rsync_path}' | sudo tee {RSYNC_SUDOERS_DROPIN} "
                    f"&& sudo chmod 0440 {RSYNC_SUDOERS_DROPIN}")
    if not username:
        print(f"Could not determine the current username -- skipping rsync sudo setup. {manual_hint}")
        return

    # Every privileged step below is wrapped in one try/except: sudo itself
    # (or visudo, its companion binary) isn't guaranteed to exist on every
    # Linux system this might run on -- a minimal server install, a distro
    # that ships doas instead, etc. FileNotFoundError there would otherwise
    # be unhandled and crash the entire setup flow (not just this feature)
    # for anyone on such a system. Degrading to "skip this, print how to do
    # it by hand" keeps the rest of setup (systemd autostart, starting the
    # dashboard) working regardless.
    try:
        check = subprocess.run(["sudo", "-n", "-l", rsync_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if check.returncode == 0:
            print("rsync can already run passwordlessly under sudo -- nothing to configure.")
            return

        print("Configuring passwordless sudo for rsync (lets backups read files owned by "
              "another user, e.g. a Docker container) -- you may be prompted for your password.")
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile("w", delete=False, suffix=".folderw-sudoers") as tmp:
                tmp.write(f"{username} ALL=(root) NOPASSWD: {rsync_path}\n")
                tmp_path = tmp.name

            # Validate BEFORE installing -- a syntax error in a live
            # sudoers.d file can break sudo system-wide, so this never
            # touches the real location without first confirming the
            # grammar is valid. Doesn't need root: just parses the given
            # file, doesn't touch system state.
            validate = subprocess.run(["visudo", "-c", "-f", tmp_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if validate.returncode != 0:
                print(f"Generated sudoers rule failed validation, skipping: {validate.stderr}")
                return

            # Plain sudo (not pkexec): setup.py's only documented entry
            # point is `./install.sh` -> `python3 setup.py`, always run
            # from an interactive terminal that sudo can prompt on
            # directly -- no need for polkit/pkexec, which isn't installed
            # on every system (especially headless/minimal ones) and would
            # just be an extra way for this to fail. root:root 0440 is
            # sudoers.d's required ownership/mode; sudo refuses to even
            # read a drop-in file with any other group/other write access,
            # as its own protection against tampering.
            install = subprocess.run(
                ["sudo", "install", "-m", "0440", "-o", "root", "-g", "root", tmp_path, RSYNC_SUDOERS_DROPIN],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            if install.returncode != 0:
                print(f"Could not configure sudo for rsync: {install.stderr.strip()}. {manual_hint}")
                return
            print("Configured passwordless sudo for rsync.")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
    except FileNotFoundError as e:
        print(f"sudo/visudo not found ({e}) -- skipping rsync sudo setup. Backups will still "
              f"work, just skipping any file they don't have permission to read. {manual_hint}")

MOUNT_SUDOERS_DROPIN = "/etc/sudoers.d/folderw-mount-exec"

def configure_mount_exec_sudo(base_dir):
    """Grants NOPASSWD sudo for remounting BASE_DIR's filesystem exec,
    and returns the resolved mount point for configure_systemd_
    autostart()'s ExecStartPre -- or None if BASE_DIR is on the root
    filesystem (always exec already; no separate mount to defend, and
    remounting / is a much bigger blast radius for no benefit) or if
    the mount point couldn't be determined at all.

    See configure_systemd_autostart()'s own comment for why this
    exists: confirmed live, more than once, that a drive's fstab entry
    saying exec isn't reliably honored by the actual mount active at
    boot on every machine.
    """
    mount_check = subprocess.run(
        ["findmnt", "-n", "-o", "TARGET", "--target", base_dir],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if mount_check.returncode != 0 or not mount_check.stdout.strip():
        return None
    mount_point = mount_check.stdout.strip()
    if mount_point == "/":
        return None

    mount_path = shutil.which("mount") or "/usr/bin/mount"
    username = os.environ.get("SUDO_USER") or os.environ.get("USER") or os.environ.get("LOGNAME")
    remount_args = ["-o", "remount,exec", mount_point]
    manual_hint = (f"To let FolderW self-heal a noexec {mount_point} at boot, add manually: "
                    f"echo '<username> ALL=(root) NOPASSWD: {mount_path} -o remount,exec {mount_point}' "
                    f"| sudo tee {MOUNT_SUDOERS_DROPIN} && sudo chmod 0440 {MOUNT_SUDOERS_DROPIN}")
    if not username:
        print(f"Could not determine the current username -- skipping mount-exec sudo setup. {manual_hint}")
        return mount_point

    try:
        # Exact args, not just the bare binary (unlike configure_rsync_
        # sudo()'s check): the rule being granted below is scoped to
        # this exact command line, not the binary in general, so
        # checking with the same exact args is what correctly detects
        # whether it's already covered.
        check = subprocess.run(["sudo", "-n", "-l", mount_path, *remount_args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if check.returncode == 0:
            print(f"Remounting {mount_point} exec can already run passwordlessly under sudo -- nothing to configure.")
            return mount_point

        print(f"Configuring passwordless sudo to remount {mount_point} exec at startup (works around "
              f"this machine's mount not always honoring fstab's exec option) -- you may be prompted for your password.")
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile("w", delete=False, suffix=".folderw-sudoers") as tmp:
                tmp.write(f"{username} ALL=(root) NOPASSWD: {mount_path} -o remount,exec {mount_point}\n")
                tmp_path = tmp.name

            validate = subprocess.run(["visudo", "-c", "-f", tmp_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if validate.returncode != 0:
                print(f"Generated sudoers rule failed validation, skipping: {validate.stderr}")
                return mount_point

            install = subprocess.run(
                ["sudo", "install", "-m", "0440", "-o", "root", "-g", "root", tmp_path, MOUNT_SUDOERS_DROPIN],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            if install.returncode != 0:
                print(f"Could not configure mount-exec sudo: {install.stderr.strip()}. {manual_hint}")
                return mount_point
            print(f"Configured passwordless sudo to remount {mount_point} exec.")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
    except FileNotFoundError as e:
        print(f"sudo/visudo not found ({e}) -- skipping mount-exec sudo setup. {manual_hint}")
    return mount_point

def browse_directory(entry):
    directory = filedialog.askdirectory()
    entry.delete(0, tk.END)
    entry.insert(0, directory)

def browse_file(entry):
    file_path = filedialog.askopenfilename()
    entry.delete(0, tk.END)
    entry.insert(0, file_path)

def save_paths():
    env_path = find_dotenv()
    if not env_path:
        env_path = '.env'

    # Captured before anything below overwrites .env, so identity change
    # can be detected the same way server.py's web Settings page already
    # does -- see the reset_backup_history() call further down.
    # BACKUP_METHOD is included too: switching between incremental
    # (--link-dest, each snapshot a complete tree) and differential
    # (--compare-dest, each snapshot a delta against the original)
    # mid-stream would otherwise leave a mix of both snapshot shapes
    # under the same Snapshots folder.
    old_identity = (os.getenv('SRC_DIR'), os.getenv('BASE_DIR'), os.getenv('FULL_NAME'), os.getenv('BACKUP_METHOD'))

    paths = {
        'SRC_DIR': src_dir_entry.get(),
        'BASE_DIR': base_dir_entry.get(),
        'DATABASE': database_entry.get(),
        'FULL_NAME': full_folder_name_entry.get(),
        'SERVER_PORT': server_port_entry.get(),
        'MONITOR': str(monitor_var.get()),
        'AUTOSTART': str(autostart_var.get()),
    }


    # Set BACKUP_INTERVAL based on the MONITOR value
    if monitor_var.get() == 1:
        paths['BACKUP_INTERVAL'] = 'False'
    else:
        paths['BACKUP_INTERVAL'] = interval_var.get()

    paths['BACKUP_METHOD'] = backup_method_var.get()

    # Validate if all fields are filled
    for key, value in paths.items():
        if not value or "Browse to select" in value:
            result_label.config(text=f"Error: {key.replace('_', ' ')} is required.", foreground='#FF0000')  # Set to red
            return

    # Database must end in .db -- create_all_tables() below happily
    # creates a SQLite file under any name at all, so nothing else
    # catches a typo'd extension (or none at all) until much later,
    # if ever.
    if not paths['DATABASE'].lower().endswith('.db'):
        result_label.config(text="Error: Database file name must end with .db", foreground='#FF0000')
        return

    if not _is_valid_folder_name(paths['FULL_NAME']):
        result_label.config(text="Error: Full Backup Folder Name can't contain '/', be empty, or be '.' / '..'.", foreground='#FF0000')
        return

    # Snapshots to keep is optional — blank means "keep everything" — so it's
    # validated separately rather than through the required-fields loop above.
    max_snapshots = max_snapshots_entry.get().strip()
    if "Leave blank" in max_snapshots:
        max_snapshots = ""
    if max_snapshots and (not max_snapshots.isdigit() or int(max_snapshots) <= 0):
        result_label.config(text="Error: Snapshots to Keep must be a positive whole number, or left blank.", foreground='#FF0000')
        return

    # Base Backup Directory + Full Folder Name must never collide with
    # FolderW's own install directory (APP_DIR) -- confirmed live: with
    # Full Folder Name set to the same name as this install's own folder
    # and Base Backup Directory pointing at its parent, the backup
    # container ended up being the exact same directory as FolderW's own
    # code, and ensure_backup_folder_icon() tagged FolderW's own install
    # folder with a custom folder icon (gio metadata persists per-path
    # independent of file content, so it kept showing even after a fresh
    # reinstall at the same path).
    container_dir = os.path.realpath(os.path.join(paths['BASE_DIR'], paths['FULL_NAME']))
    app_dir_real = os.path.realpath(APP_DIR)
    if (container_dir == app_dir_real
            or app_dir_real.startswith(container_dir + os.sep)
            or container_dir.startswith(app_dir_real + os.sep)):
        result_label.config(
            text=f"Error: Base Backup Directory + Full Folder Name ({container_dir}) can't overlap with FolderW's own install directory ({app_dir_real}).",
            foreground='#FF0000'
        )
        return

    # Cloud Backup: validated (and the remote actually created) before
    # anything below is written, same "fail before the point of no
    # return" pattern as every check above -- create_remote() itself
    # never fails for a bad client_id/client_secret pairing until the
    # first real login attempt, so catching it here means an OAuth typo
    # doesn't quietly get carried all the way to the dashboard.
    cloud_backup_enabled = cloud_backup_enabled_var.get() == 1
    cloud_remote_name = ""
    if cloud_backup_enabled:
        cloud_remote_name = cloud_remote_name_entry.get().strip()
        if "e.g." in cloud_remote_name:  # untouched placeholder text
            cloud_remote_name = ""
        if not cloud_remote_name:
            result_label.config(text="Error: Remote Name is required when Cloud Backup is enabled.", foreground='#FF0000')
            return
        label_to_key = {label: key for key, label in KNOWN_REMOTE_TYPES}
        cloud_backend_type = label_to_key.get(cloud_remote_type_var.get())
        cloud_client_id = cloud_client_id_entry.get().strip()
        cloud_client_secret = cloud_client_secret_entry.get().strip()
        cloud_success, cloud_message = create_remote(cloud_remote_name, cloud_backend_type, cloud_client_id or None, cloud_client_secret or None)
        if not cloud_success:
            result_label.config(text=f"Error: {cloud_message}", foreground='#FF0000')
            return

    # Save values to .env file
    for key, value in paths.items():
        set_key(env_path, key.upper(), value)
    set_key(env_path, 'MAX_SNAPSHOTS', max_snapshots)

    set_key(env_path, 'CLOUD_SYNC_ENABLED', '1' if cloud_backup_enabled else '0')
    if cloud_backup_enabled:
        set_key(env_path, 'CLOUD_SYNC_REMOTE', cloud_remote_name)

    # Blank means "leave unchanged" (existing install) or "no login"
    # (fresh install) — either way, never touch ADMIN_PASSWORD_HASH.
    dashboard_password = dashboard_password_entry.get().strip()
    if dashboard_password:
        set_key(env_path, 'ADMIN_PASSWORD_HASH', hash_password(dashboard_password))

    notify_urls = notify_urls_entry.get().strip()
    if "e.g." in notify_urls:  # untouched placeholder text
        notify_urls = ""
    set_key(env_path, 'NOTIFY_URLS', notify_urls)
    set_key(env_path, 'NOTIFY_SEND_ALWAYS', str(notify_send_always_var.get()))

    pre_backup_script = pre_backup_script_entry.get().strip()
    if "e.g." in pre_backup_script:  # untouched placeholder text
        pre_backup_script = ""
    set_key(env_path, 'PRE_BACKUP_SCRIPT', pre_backup_script)

    post_backup_script = post_backup_script_entry.get().strip()
    if "e.g." in post_backup_script:  # untouched placeholder text
        post_backup_script = ""
    set_key(env_path, 'POST_BACKUP_SCRIPT', post_backup_script)

    result_label.config(text="Configuration saved to .env", foreground='#39FF14')  # Set to neon green

    create_all_tables(paths['DATABASE'])

    new_identity = (paths['SRC_DIR'], paths['BASE_DIR'], paths['FULL_NAME'], paths['BACKUP_METHOD'])
    if new_identity != old_identity:
        # Pointed FolderW at a different backup (source, destination, or
        # container folder) -- old session/change/statistics history
        # belongs to the previous backup and would otherwise corrupt
        # session numbering and historical stats for the new one. Also
        # clears FULL_BACKUP_COMPLETED, so main_backup.py actually runs a
        # fresh full backup for the new target instead of seeing the flag
        # still set from the old one and skipping straight to monitoring.
        # Web Settings (server.py's /submit/) already does this; this GUI
        # never did until now.
        reset_backup_history(paths['DATABASE'])

        # Old validation/evaluation results (does the source exist, is
        # there enough destination space) were computed against the
        # PREVIOUS paths -- no longer meaningful for the new ones. Scoped
        # to an actual identity change only -- found live (server.py had
        # the same bug): running this on every save, including a
        # password-only change, cleared validation_status/evaluation_
        # status even though paths never changed, which made the
        # dashboard's ready_for_manual_snapshot (index.html, requires
        # both True) revert from "Create a Manual Snapshot" back to
        # "Start Full Backup" -- so clicking the only button left forced
        # a full-backup redo for no reason.
        for check_key in ('SETTINGS_CHECK_PASSED', 'VALIDATION_CHECK_PASSED', 'EVALUATION_CHECK_PASSED'):
            set_database_value(check_key, '0')
        for stale_key in ('LAST_SRC_SIZE', 'LAST_DEST_SPACE'):
            set_database_value(stale_key, '')

    show_terminal()

    upgrade_packages()

    mount_point = configure_mount_exec_sudo(paths['BASE_DIR'])
    configure_systemd_autostart(autostart_var.get() == 1, mount_point)
    configure_rsync_sudo()

    # start_new_session detaches server.py from this terminal's session, so
    # closing the terminal (which sends SIGHUP) doesn't kill the dashboard.
    # stdout/stderr are redirected to a log file rather than inherited from
    # the terminal, since writes to a closed terminal's tty would fail once
    # it's gone.
    log_dir = os.path.join(APP_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    server_log = open(os.path.join(log_dir, "server.log"), "a")
    # FOLDERW_OPEN_BROWSER=1: tells server.py this is the genuine first-ever
    # launch, worth popping a browser window for. Every OTHER way server.py
    # starts (systemd on boot, a crash-restart, update.sh restarting it to
    # apply new code) doesn't set this -- found live, the hard way: without
    # this distinction, server.py opened a browser window on every single
    # restart, and during any restart loop (a crash loop, or update.sh
    # hitting one) that meant a new browser window every few seconds.
    # FOLDERW_OPEN_PATH: when Cloud Backup was just configured, land on
    # /cloud-backup instead of the dashboard homepage -- that's exactly
    # where "Log In via Browser" lives, the one step this wizard can't
    # do itself (see the Cloud Backup section above).
    open_path = "/cloud-backup" if cloud_backup_enabled else "/"
    subprocess.Popen(
        [sys.executable, os.path.join(APP_DIR, "server.py")],
        stdout=server_log,
        stderr=server_log,
        start_new_session=True,
        env={**os.environ, "FOLDERW_OPEN_BROWSER": "1", "FOLDERW_OPEN_PATH": open_path},
    )
    root.destroy()

     
def show_terminal():
    log_text = tk.Text(root, height=10, width=50, bg='#2d2d2d', font=('Courier', 10))
    log_text.tag_configure("neon_green", foreground="#39FF14")
    log_text.config(state=tk.NORMAL)
    sys.stdout = Logger(log_text)
    # Gridded like every other widget in this window, not .place()'d at a
    # hardcoded pixel position -- x=180,y=480 was tuned for a shorter
    # form and started overlapping the Backup Method row (and covering
    # Snapshots to Keep/Dashboard Password entirely) once rows were added
    # above it (the logo banner, Backup Method itself). Gridding it right
    # after the last existing row means it always lands below the form's
    # actual current content, however tall that turns out to be.
    log_text.grid(row=len(labels) + 10, column=0, columnspan=3, padx=10, pady=4)
    log_text.config(state=tk.NORMAL)
    # Previously a hardcoded 700x660 -- stale as soon as the form grew
    # wider/taller than that (same root cause as the overlap above), which
    # visibly shrank the window back down on every Save. Clearing the
    # geometry center_window() pinned at startup and letting Tk recompute
    # the natural size for the window's actual current content fixes both
    # at once, and keeps working regardless of how the form changes later.
    root.geometry("")
    root.update_idletasks()
    root.geometry(f"{root.winfo_reqwidth()}x{root.winfo_reqheight()}")


# Function to set placeholders in Entry fields, if corresponding .env value is empty
def set_placeholder(entry, placeholder, env_key):
    """
    This function will set the placeholder text on the Entry widget only if the corresponding value 
    is not present in the .env file. It will also handle focus-in and focus-out events.
    """
    # Check if the value exists in the .env file
    env_value = os.getenv(env_key)

    # If the .env value is empty, set the placeholder
    if not env_value:
        def on_focus_in(event):
            """Clear placeholder text when the field is focused."""
            if entry.get() == placeholder:
                entry.delete(0, tk.END)
                entry.config(foreground='black')  # Reset the text color to black when user starts typing

        def on_focus_out(event):
            """Restore placeholder text if the field is empty."""
            if entry.get() == "":
                entry.insert(0, placeholder)
                entry.config(foreground='grey')  # Set the text color to grey for placeholder text

        # Insert placeholder text and set the text color to grey
        entry.insert(0, placeholder)
        entry.config(foreground='grey')  # Placeholder text should be grey

        # Bind the focus-in and focus-out events
        entry.bind("<FocusIn>", on_focus_in)
        entry.bind("<FocusOut>", on_focus_out)
    else:
        # If the .env value is present, insert it directly into the entry field
        entry.insert(0, env_value)
        entry.config(foreground='black')  # Set text color to black if there's a value

# Load existing .env values if available
load_dotenv()

# Create the Tkinter window with a dark theme
root = ThemedTk(theme="black")
root.title("Backup Configuration")
try:
    # PIL's loader (already a dependency, used below for the directory
    # entries' Browse icons too), not tkinter's own PhotoImage -- Tk's
    # built-in PNG support depends on the Tk version it was built
    # against, PIL's doesn't. Kept referenced by a module-level variable
    # (not just passed inline) so Python's GC doesn't collect it out from
    # under the window -- a PhotoImage with no live Python reference
    # silently blanks back to no icon.
    _window_icon = ImageTk.PhotoImage(Image.open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logo.png')))
    root.iconphoto(True, _window_icon)
except (OSError, tk.TclError) as e:
    print(f"Could not set window icon (non-fatal): {e}")

# Function to center the window
def center_window(window):
    window.update_idletasks()
    screen_width = window.winfo_screenwidth()
    screen_height = window.winfo_screenheight()
    size = tuple(int(_) for _ in window.geometry().split('+')[0].split('x'))
    x = screen_width // 2 - size[0] // 2
    y = screen_height // 2 - size[1] // 2
    window.geometry(f"{size[0]}x{size[1]}+{x}+{y}")

# Create and place widgets with placeholders
labels = [
    ("Source Directory:", "SRC_DIR"),
    ("Base Backup Directory:", "BASE_DIR"),
    ("Full Folder Name:", "FULL_NAME"),
    ("Database:", "DATABASE"),
    ("Server Port:", "SERVER_PORT"),  # Add server port label
]
entries = []
placeholders = {
    "Source Directory:": "Select the Source Folder to be Backed up",
    "Base Backup Directory:": "Destination directory to place the backup",
    "Database:": "Enter the database file name, Example: backup.db",
    "Full Folder Name:": "Example: Documents — contains Full Backup/ and Snapshots/ inside it",
    "Server Port:": "Enter the server port, Example: 8000"
}

# Create and place widgets
for i, (label, env_key) in enumerate(labels):
    tk.Label(root, text=label, foreground='#ffffff', background='#1a1a1a').grid(row=i, column=0, padx=10, pady=4, sticky='e')
    entry = ttk.Entry(root, width=50)
    entry.grid(row=i, column=1, padx=10, pady=4)
    entries.append(entry)
    
    # Set placeholder text
    placeholder = placeholders.get(label, "")
    set_placeholder(entry, placeholder, env_key)  # Pass the corresponding env key
    
    if "Directory" in label or "Backup" in label:
        button = ttk.Button(root, text="Browse...", command=lambda e=entry: browse_directory(e))
        button.grid(row=i, column=2, padx=10, pady=4)
    elif "File" in label:
        button = ttk.Button(root, text="Browse...", command=lambda e=entry: browse_file(e))
        button.grid(row=i, column=2, padx=10, pady=4)

src_dir_entry, base_dir_entry, full_folder_name_entry, database_entry, server_port_entry = entries  # Include server port entry

# Logo banner, in the same column as the Browse buttons above it, but
# spanning the three rows below them (Full Folder Name/Database/Server
# Port) that never get a Browse button and would otherwise sit empty --
# rather than a dedicated row of its own, which wasted a full row's
# height with two of its three columns blank. Kept as a module-level
# reference (_logo_banner_image), same reasoning as _window_icon further
# down -- a PhotoImage with no live Python reference gets silently
# garbage-collected and the label just goes blank.
_logo_banner_image = ImageTk.PhotoImage(Image.open(os.path.join(APP_DIR, 'logo.png')).resize((96, 96), Image.LANCZOS))
logo_banner = tk.Label(root, image=_logo_banner_image, background='#1a1a1a')
logo_banner.grid(row=2, column=2, rowspan=3, padx=10, pady=4)

# Watchdog checkbox setup
monitor_var = tk.IntVar(value=1)
monitor_checkbox = tk.Checkbutton(root, text="Watch source folder for automated backups on changes", variable=monitor_var, background='#1a1a1a', foreground='#ffffff', selectcolor='#2ecc71', command=toggle_backup_options)
monitor_checkbox.grid(row=len(labels), column=1, padx=10, pady=4, sticky='w')  # Aligned with other fields

# Short hint: watchdog and scheduled backups are mutually exclusive
monitor_hint = tk.Label(root, text="Backs up as soon as a file changes.", font=('TkDefaultFont', 8), foreground='#888888', bg='#1a1a1a')
monitor_hint.grid(row=len(labels), column=2, padx=10, pady=4, sticky='w')

# Create the label next to the checkbox
monitor_label = tk.Label(root, text="Watchdog Future:", foreground='#ffffff', bg='#1a1a1a', padx=0, pady=0)
monitor_label.grid(row=len(labels), column=0, padx=0, pady=4, sticky='e')  # Place it in the same row as the checkbox, to the left (column 0)

# Dynamic Backup Interval Options
interval_var = StringVar(value='hourly')
interval_label = tk.Label(root, text="Backup Interval:", foreground='#ffffff', bg='#1a1a1a')
interval_dropdown = ttk.Combobox(root, textvariable=interval_var, values=["hourly", "half-day", "daily", "weekly"])
interval_hint = tk.Label(root, text="Backs up on this fixed schedule instead.", font=('TkDefaultFont', 8), foreground='#888888', bg='#1a1a1a')
interval_widgets = [interval_label, interval_dropdown, interval_hint]

interval_label.grid(row=len(labels) + 1, column=0, padx=10, pady=4, sticky='e')
interval_dropdown.grid(row=len(labels) + 1, column=1, padx=10, pady=4)
interval_hint.grid(row=len(labels) + 1, column=2, padx=10, pady=4, sticky='w')

# Autostart on boot checkbox setup
autostart_var = tk.IntVar(value=0)
autostart_checkbox = tk.Checkbutton(root, text="Start FolderW automatically at system boot", variable=autostart_var, background='#1a1a1a', foreground='#ffffff', selectcolor='#2ecc71')
autostart_checkbox.grid(row=len(labels) + 2, column=1, padx=10, pady=4, sticky='w')

autostart_hint = tk.Label(root, text="Installs a systemd user service that runs the dashboard on login.", font=('TkDefaultFont', 8), foreground='#888888', bg='#1a1a1a')
autostart_hint.grid(row=len(labels) + 2, column=2, padx=10, pady=4, sticky='w')

autostart_label = tk.Label(root, text="Autostart:", foreground='#ffffff', bg='#1a1a1a', padx=0, pady=0)
autostart_label.grid(row=len(labels) + 2, column=0, padx=0, pady=4, sticky='e')

# Backup Method: incremental (--link-dest against the most recent
# snapshot, stays small indefinitely) vs differential (--link-dest always
# against the original full backup, simpler mental model but snapshots
# grow larger over time). See rsync_incremental.py/rsync_differential.py.
# Radiobuttons sharing one StringVar, not two independent checkboxes --
# selecting one always deselects the other, matching "either/or, never
# both" rather than something a Checkbutton pair would need extra code
# to enforce.
backup_method_var = StringVar(value='differential')
backup_method_label = tk.Label(root, text="Backup Method:", foreground='#ffffff', bg='#1a1a1a', padx=0, pady=0)
backup_method_frame = tk.Frame(root, bg='#1a1a1a')

differential_radio = tk.Radiobutton(backup_method_frame, text="Differential Backup", variable=backup_method_var, value='differential', background='#1a1a1a', foreground='#ffffff', selectcolor='#2ecc71', activebackground='#1a1a1a', activeforeground='#ffffff')
differential_radio.pack(side=tk.LEFT)
differential_info = ttk.Button(backup_method_frame, text="?", width=2, command=lambda: _show_backup_method_info('differential'))
differential_info.pack(side=tk.LEFT, padx=(0, 20))

incremental_radio = tk.Radiobutton(backup_method_frame, text="Incremental Backup", variable=backup_method_var, value='incremental', background='#1a1a1a', foreground='#ffffff', selectcolor='#2ecc71', activebackground='#1a1a1a', activeforeground='#ffffff')
incremental_radio.pack(side=tk.LEFT)
incremental_info = ttk.Button(backup_method_frame, text="?", width=2, command=lambda: _show_backup_method_info('incremental'))
incremental_info.pack(side=tk.LEFT)

backup_method_label.grid(row=len(labels) + 3, column=0, padx=10, pady=4, sticky='e')
backup_method_frame.grid(row=len(labels) + 3, column=1, columnspan=2, padx=10, pady=4, sticky='w')

# Snapshot retention: optional, blank means keep every snapshot forever
max_snapshots_label = tk.Label(root, text="Snapshots to Keep:", foreground='#ffffff', bg='#1a1a1a', padx=0, pady=0)
max_snapshots_label.grid(row=len(labels) + 4, column=0, padx=0, pady=4, sticky='e')

max_snapshots_entry = ttk.Entry(root, width=50)
max_snapshots_entry.grid(row=len(labels) + 4, column=1, padx=10, pady=4)
set_placeholder(max_snapshots_entry, "Leave blank to keep all snapshots", 'MAX_SNAPSHOTS')

max_snapshots_hint = tk.Label(root, text="Oldest incremental snapshots beyond this count are deleted automatically.", font=('TkDefaultFont', 8), foreground='#888888', bg='#1a1a1a')
max_snapshots_hint.grid(row=len(labels) + 4, column=2, padx=10, pady=4, sticky='w')

# Pre/post-backup hook scripts: both optional. Pre-script failing (non-
# zero exit) aborts the backup entirely -- see backup_hooks.py. Post-
# script always runs regardless of backup outcome (e.g. restarting a
# service the pre-script stopped shouldn't depend on the backup itself
# succeeding).
pre_backup_script_label = tk.Label(root, text="Pre-Backup Script:", foreground='#ffffff', bg='#1a1a1a', padx=0, pady=0)
pre_backup_script_label.grid(row=len(labels) + 5, column=0, padx=0, pady=4, sticky='e')

pre_backup_script_entry = ttk.Entry(root, width=50)
pre_backup_script_entry.grid(row=len(labels) + 5, column=1, padx=10, pady=4)
set_placeholder(pre_backup_script_entry, "e.g. /home/user/scripts/dump-db.sh", 'PRE_BACKUP_SCRIPT')

pre_backup_script_hint = tk.Label(root, text="Executable, run before every backup. Exits non-zero = backup skipped. Leave blank if not needed.", font=('TkDefaultFont', 8), foreground='#888888', bg='#1a1a1a')
pre_backup_script_hint.grid(row=len(labels) + 5, column=2, padx=10, pady=4, sticky='w')

post_backup_script_label = tk.Label(root, text="Post-Backup Script:", foreground='#ffffff', bg='#1a1a1a', padx=0, pady=0)
post_backup_script_label.grid(row=len(labels) + 6, column=0, padx=0, pady=4, sticky='e')

post_backup_script_entry = ttk.Entry(root, width=50)
post_backup_script_entry.grid(row=len(labels) + 6, column=1, padx=10, pady=4)
set_placeholder(post_backup_script_entry, "e.g. /home/user/scripts/restart-service.sh", 'POST_BACKUP_SCRIPT')

post_backup_script_hint = tk.Label(root, text="Executable, run after every backup (success or failure). Leave blank if not needed.", font=('TkDefaultFont', 8), foreground='#888888', bg='#1a1a1a')
post_backup_script_hint.grid(row=len(labels) + 6, column=2, padx=10, pady=4, sticky='w')

# Dashboard password: optional, never prefilled (only the hash is ever
# stored) — leave blank on a fresh install for no login, or on an existing
# one to keep whatever password is already set.
dashboard_password_label = tk.Label(root, text="Dashboard Password:", foreground='#ffffff', bg='#1a1a1a', padx=0, pady=0)
dashboard_password_label.grid(row=len(labels) + 7, column=0, padx=0, pady=4, sticky='e')

dashboard_password_entry = ttk.Entry(root, width=50, show='*')
dashboard_password_entry.grid(row=len(labels) + 7, column=1, padx=10, pady=4)

dashboard_password_hint = tk.Label(root, text="Leave blank for no login, or to keep the current password unchanged.", font=('TkDefaultFont', 8), foreground='#888888', bg='#1a1a1a')
dashboard_password_hint.grid(row=len(labels) + 7, column=2, padx=10, pady=4, sticky='w')

# Notification URL(s): optional, comma-separated Apprise URLs
notify_urls_label = tk.Label(root, text="Notification URL(s):", foreground='#ffffff', bg='#1a1a1a', padx=0, pady=0)
notify_urls_label.grid(row=len(labels) + 8, column=0, padx=0, pady=4, sticky='e')

notify_urls_entry = ttk.Entry(root, width=50)
notify_urls_entry.grid(row=len(labels) + 8, column=1, padx=10, pady=4)
set_placeholder(notify_urls_entry, "e.g. pover://user@token, ntfy://topic", 'NOTIFY_URLS')

notify_urls_hint = tk.Label(root, text="Comma-separated Apprise URLs, notified on backup failure/completion. Leave blank to disable.", font=('TkDefaultFont', 8), foreground='#888888', bg='#1a1a1a')
notify_urls_hint.grid(row=len(labels) + 8, column=2, padx=10, pady=4, sticky='w')

# Independent of the URL(s) above: a local desktop notification via
# notify-send, usable on its own (no external service needed) or
# alongside Apprise URLs.
notify_send_always_var = tk.IntVar(value=0)
notify_send_always_checkbox = tk.Checkbutton(root, text="Always send a desktop notification", variable=notify_send_always_var, background='#1a1a1a', foreground='#ffffff', selectcolor='#2ecc71')
notify_send_always_checkbox.grid(row=len(labels) + 9, column=1, padx=10, pady=4, sticky='w')

notify_send_always_hint = tk.Label(root, text="Uses notify-send to show a notification on this machine's own desktop.", font=('TkDefaultFont', 8), foreground='#888888', bg='#1a1a1a')
notify_send_always_hint.grid(row=len(labels) + 9, column=2, padx=10, pady=4, sticky='w')

# Cloud Backup: optional, mirrors the dashboard's own "Add a New Remote"
# form (cloud_backup.html) so setup can register the remote itself --
# create_remote() is a plain `rclone config create`, no web server
# needed. The actual OAuth "Log In via Browser" step is deliberately
# NOT reimplemented here: it needs a local HTTP listener catching the
# provider's redirect, tracked via server.py's own in-memory job state
# (see cloud_backup.py's start_remote_authorize()/_authorize_jobs),
# which doesn't exist until server.py is actually running -- which
# only happens at the very end of save_paths(), after this whole
# window closes. So this just gets the remote CREATED and enabled;
# save_paths() opens the first-launch browser straight to /cloud-backup
# instead of / when this was used, landing the user exactly where they
# need to finish logging in.
cloud_backup_enabled_var = tk.IntVar(value=0)
cloud_backup_checkbox = tk.Checkbutton(root, text="Enable Cloud Backup", variable=cloud_backup_enabled_var, background='#1a1a1a', foreground='#ffffff', selectcolor='#2ecc71', command=lambda: toggle_cloud_backup_options())
cloud_backup_checkbox.grid(row=len(labels) + 10, column=1, padx=10, pady=4, sticky='w')

cloud_backup_label = tk.Label(root, text="Cloud Backup:", foreground='#ffffff', bg='#1a1a1a', padx=0, pady=0)
cloud_backup_label.grid(row=len(labels) + 10, column=0, padx=0, pady=4, sticky='e')

cloud_backup_hint = tk.Label(root, text="Mirrors the source folder to a cloud remote after every backup.", font=('TkDefaultFont', 8), foreground='#888888', bg='#1a1a1a')
cloud_backup_hint.grid(row=len(labels) + 10, column=2, padx=10, pady=4, sticky='w')

cloud_remote_type_label = tk.Label(root, text="Cloud Provider:", foreground='#ffffff', bg='#1a1a1a', padx=0, pady=0)
cloud_remote_type_var = StringVar(value=KNOWN_REMOTE_TYPES[0][0])
cloud_remote_type_dropdown = ttk.Combobox(root, textvariable=cloud_remote_type_var, state='readonly',
                                           values=[label for _key, label in KNOWN_REMOTE_TYPES])
cloud_remote_type_dropdown.current(0)
cloud_remote_type_hint = tk.Label(root, text="Which service to back up to.", font=('TkDefaultFont', 8), foreground='#888888', bg='#1a1a1a')

cloud_remote_name_label = tk.Label(root, text="Remote Name:", foreground='#ffffff', bg='#1a1a1a', padx=0, pady=0)
cloud_remote_name_entry = ttk.Entry(root, width=50)
cloud_remote_name_hint = tk.Label(root, text="Just a label for this connection -- letters, numbers, - and _.", font=('TkDefaultFont', 8), foreground='#888888', bg='#1a1a1a')
set_placeholder(cloud_remote_name_entry, "e.g. Google-Drive", 'CLOUD_SYNC_REMOTE')

cloud_client_id_label = tk.Label(root, text="Client ID:", foreground='#ffffff', bg='#1a1a1a', padx=0, pady=0)
cloud_client_id_entry = ttk.Entry(root, width=50)
cloud_client_id_hint = tk.Label(root, text="Optional -- your own OAuth app instead of rclone's shared one. Leave both blank to use rclone's.", font=('TkDefaultFont', 8), foreground='#888888', bg='#1a1a1a')

cloud_client_secret_label = tk.Label(root, text="Client Secret:", foreground='#ffffff', bg='#1a1a1a', padx=0, pady=0)
cloud_client_secret_entry = ttk.Entry(root, width=50, show='*')
cloud_client_secret_hint = tk.Label(root, text="Optional, goes with the Client ID above.", font=('TkDefaultFont', 8), foreground='#888888', bg='#1a1a1a')

cloud_backup_option_widgets = [
    (cloud_remote_type_label, cloud_remote_type_dropdown, cloud_remote_type_hint, len(labels) + 11),
    (cloud_remote_name_label, cloud_remote_name_entry, cloud_remote_name_hint, len(labels) + 12),
    (cloud_client_id_label, cloud_client_id_entry, cloud_client_id_hint, len(labels) + 13),
    (cloud_client_secret_label, cloud_client_secret_entry, cloud_client_secret_hint, len(labels) + 14),
]
for lbl, widget, hint, row in cloud_backup_option_widgets:
    lbl.grid(row=row, column=0, padx=10, pady=4, sticky='e')
    widget.grid(row=row, column=1, padx=10, pady=4)
    hint.grid(row=row, column=2, padx=10, pady=4, sticky='w')

def toggle_cloud_backup_options():
    for lbl, widget, hint, _row in cloud_backup_option_widgets:
        if cloud_backup_enabled_var.get() == 1:
            lbl.grid()
            widget.grid()
            hint.grid()
        else:
            lbl.grid_remove()
            widget.grid_remove()
            hint.grid_remove()

save_button = ttk.Button(root, text="Save Configuration", command=save_paths)
save_button.grid(row=len(labels) + 15, column=1, pady=4)

result_label = ttk.Label(root, text="", background='#1a1a1a', foreground='#ffffff')
result_label.grid(row=len(labels) + 16, column=1, pady=4)

# Safely set the monitor variable
monitor_value = os.getenv('MONITOR')
if monitor_value is not None:
    monitor_var.set(int(monitor_value))
else:
    monitor_var.set(0)  # Default to 0 if MONITOR is not set

# Safely set the autostart variable
autostart_value = os.getenv('AUTOSTART')
autostart_var.set(int(autostart_value)) if autostart_value is not None else autostart_var.set(0)

# Safely set the backup method variable
backup_method_value = os.getenv('BACKUP_METHOD')
backup_method_var.set(backup_method_value if backup_method_value in ('incremental', 'differential') else 'differential')

# Safely set the "always send a desktop notification" variable
notify_send_always_value = os.getenv('NOTIFY_SEND_ALWAYS')
notify_send_always_var.set(int(notify_send_always_value)) if notify_send_always_value is not None else notify_send_always_var.set(0)

# Safely set the cloud backup enabled variable -- remote TYPE isn't
# pre-selected on reopen even if CLOUD_SYNC_REMOTE is already set: it
# isn't stored in .env at all (it lives in rclone's own config), and
# an existing install with cloud backup already configured would
# manage it from the dashboard's Cloud Backup page, not this wizard.
cloud_backup_enabled_value = os.getenv('CLOUD_SYNC_ENABLED')
cloud_backup_enabled_var.set(int(cloud_backup_enabled_value)) if cloud_backup_enabled_value == '1' else cloud_backup_enabled_var.set(0)

# Sync the interval widgets' visibility with the loaded monitor value
toggle_backup_options()
toggle_cloud_backup_options()

# Configure the root window background color
root.configure(bg='#1a1a1a')

# Center the window
center_window(root)


root.mainloop()