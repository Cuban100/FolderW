import os
import hashlib
import hmac
import secrets
from db_operations import load_env_value, save_env_values

PBKDF2_ITERATIONS = 200_000


def hash_password(password):
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, PBKDF2_ITERATIONS)
    return f"{salt.hex()}:{dk.hex()}"


def verify_password(password, stored_hash):
    if not stored_hash or ':' not in stored_hash:
        return False
    salt_hex, hash_hex = stored_hash.split(':', 1)
    try:
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, PBKDF2_ITERATIONS)
    return hmac.compare_digest(dk.hex(), hash_hex)


def get_or_create_secret_key():
    # Persisted rather than regenerated on every process start — otherwise
    # every dashboard restart (e.g. from update.sh) would invalidate every
    # signed session cookie and silently log everyone out.
    key = load_env_value('SECRET_KEY')
    if not key:
        key = secrets.token_hex(32)
        save_env_values({'SECRET_KEY': key})
    return key
