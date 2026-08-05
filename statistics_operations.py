from db_operations import load_env_value, load_other_variables
import os
import psutil
import subprocess
import shutil
from loguru import logger

database = load_env_value('DATABASE')
base_dir = load_env_value('BASE_DIR')
src_dir = load_env_value('SRC_DIR')
full_backup = load_other_variables('full_backup')
server_port = load_env_value('SERVER_PORT')
monitor = load_env_value('MONITOR')
backup_interval = load_env_value('BACKUP_INTERVAL')
full_name = load_env_value('FULL_NAME')

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
        return unmet_conditions

    if not os.path.exists(base_dir):
        unmet_conditions.append("Base backup directory does not exist: {}".format(base_dir))
        return unmet_conditions

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
        'DATABASE': database,
        'SRC_DIR': src_dir,
        'BASE_DIR': base_dir,
        'FULL_NAME': full_name,
        'SERVER_PORT': server_port,
        'MONITOR': monitor,
        'BACKUP_INTERVAL': backup_interval
    }
            
    missing_vars = []
    for key, value in settings.items():
        if not value:
            missing_vars.append(key)
        
    for key, value in settings.items():
        logger.info(f"{key}: {value}")
        
    settings_sent = len(missing_vars) == 0
    return settings_sent, settings, missing_vars

def get_folder_size_du(full_backup):
    result = subprocess.run(['du', '-sb', full_backup], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    
    if result.returncode == 0:
        size_in_bytes = int(result.stdout.decode('utf-8').split()[0])
        return human_readable_size(size_in_bytes)
    else:
        logger.info(f"Error: {result.stderr.decode()}")
        return None

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
    parent_mount_point = os.path.dirname(subdirectory_path.rstrip('/'))
    try:
        usage = psutil.disk_usage(parent_mount_point)
        total = usage.total / (1024**3)  # Convert to GB
        used = usage.used / (1024**3)  # Convert to GB
        free = usage.free / (1024**3)  # Convert to GB
        return total, used, free
    except FileNotFoundError:
        return None, None, None



def evaluation_of_resources():
    src_size = get_folder_size(src_dir)
    total, used, free = destination_space()
    if total is None:
        raise Exception("Cannot determine the device for the destination path.")
    unmet_conditions = []
    if free * (1024**3) < src_size:  # free is in GB, so we convert it back to bytes for comparison
        unmet_conditions.append(f"Not enough space in destination. Required: {human_readable_size(src_size)}, Available: {human_readable_size(free * (1024**3))}")
        return human_readable_size(src_size), human_readable_size(free * (1024**3)), False, unmet_conditions
    return human_readable_size(src_size), human_readable_size(free * (1024**3)), True, ["All conditions are met."]



total, used, free = destination_space()
logger.info(f"Destination space: {total}, {used}, {free}")
logger.info(f"Evaluation of resources: {evaluation_of_resources()}")
