import apprise
from db_operations import load_env_value
from loguru import logger


def notify(title, body):
    """Send a notification to every URL configured in NOTIFY_URLS (comma-
    separated Apprise URLs — e.g. pover://user@token, ntfy://topic). A no-op
    if none are configured, so this is always safe to call unconditionally.
    """
    urls_raw = load_env_value('NOTIFY_URLS')
    if not urls_raw:
        return False
    urls = [u.strip() for u in urls_raw.split(',') if u.strip()]
    if not urls:
        return False

    apobj = apprise.Apprise()
    for url in urls:
        if not apobj.add(url):
            logger.warning(f"Invalid notification URL, skipped: {url}")

    try:
        return apobj.notify(title=title, body=body)
    except Exception as e:
        logger.warning(f"Failed to send notification: {e}")
        return False
