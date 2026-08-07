from db_operations import load_env_value, load_other_variables
import os
import psutil
import subprocess
import shutil
from loguru import logger

def get_disk_usage(base_dir):
    total, used, free = shutil.disk_usage(base_dir)
    return {
        "total": total,
        "used": used,
        "free": free
    }

def get_folder_size(base_dir):
    total_size = 0
    for dirpath, dirnames, filenames in os.walk(base_dir):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            if os.path.exists(fp):
                total_size += os.path.getsize(fp)
    return total_size

def get_available_space(path):
    """
    Get the available space in the specified path.
    """
    statvfs = os.statvfs(path)
    return statvfs.f_frsize * statvfs.f_bavail


def check_space(src_dir, base_dir):
    src_size = get_folder_size(src_dir)
    dest_available_space = get_available_space(base_dir)
    return dest_available_space >= 2 * src_size

def validate_all_conditions(src_dir, base_dir):
    logger.info("Validating all conditions")
    unmet_conditions = []

    if not os.path.exists(src_dir):
        unmet_conditions.append("Source directory does not exist: {}".format(src_dir))
        return False, unmet_conditions

    if not os.path.exists(base_dir):
        unmet_conditions.append("Base backup directory does not exist: {}".format(base_dir))
        return False, unmet_conditions

    return True, unmet_conditions




def get_device_for_path(path):
    """
    Get the device for the given path.
    """
    real_path = os.path.realpath(path)
    partitions = psutil.disk_partitions()

    for partition in partitions:
        if real_path.startswith(partition.mountpoint):
            return partition.device, partition.mountpoint
    
    return None, None



def check_env_variables():
    logger.info(f"Checking settings...")
    settings = {
        'DATABASE': load_env_value('DATABASE'),
        'SRC_DIR': load_env_value('SRC_DIR'),
        'BASE_DIR': load_env_value('BASE_DIR'),
        'FULL_NAME': load_env_value('FULL_NAME'),
        'SERVER_PORT': load_env_value('SERVER_PORT'),
        'MONITOR': load_env_value('MONITOR'),
        'BACKUP_INTERVAL': load_env_value('BACKUP_INTERVAL')
    }
            
    missing_vars = []
    for key, value in settings.items():
        if not value:
            missing_vars.append(key)
        
    for key, value in settings.items():
        logger.info(f"{key}: {value}")
        
    settings_sent = len(missing_vars) == 0
    return settings_sent, settings, missing_vars

def get_folder_size_bytes_du(path, exclude_from=None, apparent_size=True):
    """Like get_folder_size(), but shells out to `du` (much faster than a
    pure-Python os.walk on a large tree) and returns a raw byte count
    instead of a human-readable string.

    `du` exits non-zero whenever it can't read even a single subdirectory
    (permission-denied is common and expected on a large tree — e.g. Docker
    volume configs owned by a container's internal UID) but still prints a
    valid, if slightly undercounted, total to stdout. Only treat it as a
    real failure if stdout has nothing usable at all.

    exclude_from (optional): a path to an rsync-style exclude patterns file
    (GNU du's --exclude-from uses the same glob syntax). Without this, a
    huge excluded-from-the-backup item — e.g. a sparse virtual disk image
    with a multi-hundred-GB apparent size — inflates the total far beyond
    what will actually be transferred.

    apparent_size: True (default) sums each file's logical size — matches
    what rsync's own byte counters report, so the live progress-percentage
    math (which compares against those counters) stays internally
    consistent. Pass False for anything shown/compared as an absolute
    size a user might sanity-check against real disk/drive capacity (a
    "does this fit" question) — a sparse file can have a logical size wildly
    larger than what it (or its backup copy, now that rsync runs with
    --sparse) actually occupies on disk.
    """
    command = ['du', '-s']
    if apparent_size:
        command.append('-b')
    else:
        command.append('--block-size=1')
    if exclude_from:
        command.append(f'--exclude-from={exclude_from}')
    command.append(path)
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    output = result.stdout.decode('utf-8').strip()
    if output:
        try:
            return int(output.split()[0])
        except (ValueError, IndexError):
            pass
    logger.info(f"Error: {result.stderr.decode()}")
    return None

def get_folder_size_du(full_backup):
    size_in_bytes = get_folder_size_bytes_du(full_backup)
    return human_readable_size(size_in_bytes) if size_in_bytes is not None else None

def human_readable_size(size_in_bytes):
    """
    Convert size in bytes to a human-readable format (KB, MB, GB, etc.).
    """
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_in_bytes < 1024:
            return f"{size_in_bytes:.2f} {unit}"
        size_in_bytes /= 1024
    return f"{size_in_bytes:.2f} TB"
def get_device_size(mount_point):
    """
    Get the total and available size of the device mounted at the given path.
    """
    usage = psutil.disk_usage(mount_point)
    total_size = usage.total
    available_size = usage.free
    return total_size, available_size

def destination_space():
    subdirectory_path = load_env_value('BASE_DIR')
    try:
        usage = psutil.disk_usage(subdirectory_path)
        total = usage.total / (1024**3)  # Convert to GB
        used = usage.used / (1024**3)  # Convert to GB
        free = usage.free / (1024**3)  # Convert to GB
        return total, used, free
    except FileNotFoundError:
        return None, None, None



def evaluation_of_resources():
    # get_folder_size_bytes_du with exclude_from, not the plain get_folder_size
    # os.walk above: without it, this counts everything rsync will actually
    # skip too — most dramatically Docker Desktop's VM disk, a sparse file
    # with a ~1TB apparent size but ~1.4GB real content, which alone made
    # this look like a 1.25TB source when the real backup scope is ~200GB.
    # apparent_size=False: this number gets shown to a user as an absolute
    # size ("does my data fit?") and compared against real drive capacity —
    # sparse files should count for their real disk footprint here, not
    # their logical size (rsync now runs with --sparse, so a sparse file's
    # backup copy genuinely only occupies its real size at the destination
    # too, not its logical one).
    # `or 0`: unlike the os.walk version, du can return None on total
    # failure (e.g. SRC_DIR unreadable) — keep this always an int like
    # every downstream comparison/formatting call here already assumes.
    src_size = get_folder_size_bytes_du(load_env_value('SRC_DIR'), exclude_from=load_other_variables('exclude_file'), apparent_size=False) or 0
    total, used, free = destination_space()
    if total is None:
        return (
            human_readable_size(src_size),
            "N/A",
            False,
            ["Cannot determine free space for the destination directory — it may not exist or isn't mounted."],
        )
    unmet_conditions = []
    if free * (1024**3) < src_size:  # free is in GB, so we convert it back to bytes for comparison
        unmet_conditions.append(f"Not enough space in destination. Required: {human_readable_size(src_size)}, Available: {human_readable_size(free * (1024**3))}")
        return human_readable_size(src_size), human_readable_size(free * (1024**3)), False, unmet_conditions
    return human_readable_size(src_size), human_readable_size(free * (1024**3)), True, ["All conditions are met."]

