import psutil
import os
from db_operations import load_env_value

def destination_space():
    subdirectory_path = load_env_value('BASE_DIR')
    parent_mount_point = os.path.dirname(subdirectory_path.rstrip('/'))
    try:
        usage = psutil.disk_usage(parent_mount_point)
        print(f"Total space at {parent_mount_point}: {usage.total / (1024**3):.2f} GB")
        print(f"Used space: {usage.used / (1024**3):.2f} GB")
        print(f"Free space: {usage.free / (1024**3):.2f} GB")
    except FileNotFoundError:
        print(f"Mount point {parent_mount_point} not found.")


