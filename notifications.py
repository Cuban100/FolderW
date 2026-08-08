import os
import subprocess
import apprise
from db_operations import load_env_value
from loguru import logger

# notify-send's own urgency levels (freedesktop notification spec) --
# used as-is for the desktop channel, and mapped to Apprise's closest
# NotifyType for services that render/color-code by severity.
VALID_LEVELS = ('low', 'normal', 'critical')

_APPRISE_TYPE_BY_LEVEL = {
    'low': apprise.NotifyType.INFO,
    'normal': apprise.NotifyType.SUCCESS,
    'critical': apprise.NotifyType.FAILURE,
}


def notify(title, body, level='normal'):
    """Send a notification to every URL configured in NOTIFY_URLS (comma-
    separated Apprise URLs — e.g. pover://user@token, ntfy://topic), and
    additionally always to the desktop (via notify-send) if
    NOTIFY_SEND_ALWAYS is enabled — a local notification independent of
    whether any Apprise URL is configured, or in addition to one.

    level: 'low', 'normal' (default), or 'critical' -- passed to notify-
    send's --urgency as-is, and mapped to the closest Apprise NotifyType
    (INFO/SUCCESS/FAILURE) for services that color-code by severity.
    """
    if level not in VALID_LEVELS:
        logger.warning(f"Unknown notification level {level!r}, treating as 'normal'.")
        level = 'normal'
    apprise_sent = _notify_apprise(title, body, level)
    if load_env_value('NOTIFY_SEND_ALWAYS') == '1':
        _notify_desktop(title, body, level)
    return apprise_sent


def _notify_apprise(title, body, level):
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
        return apobj.notify(title=title, body=body, notify_type=_APPRISE_TYPE_BY_LEVEL[level])
    except Exception as e:
        logger.warning(f"Failed to send notification: {e}")
        return False


def _notify_desktop(title, body, level):
    # Same pattern as rsync_event_handler.py's notify_stop() -- DISPLAY=:0
    # is needed since this runs from a --user systemd service with no
    # inherited X session, not an interactive terminal. --urgency is
    # notify-send's own flag, accepting exactly these three values.
    try:
        result = subprocess.run(
            ['/usr/bin/notify-send', f'--urgency={level}', title, body],
            env=dict(os.environ, DISPLAY=":0"),
            capture_output=True, text=True,
        )
        # A non-zero exit (no notification daemon registered on the D-Bus
        # session -- e.g. it hasn't been activated yet, or crashed) used
        # to be silently swallowed: subprocess.run doesn't raise on a bad
        # return code unless check=True, so this failed completely
        # invisibly, with nothing in the logs to explain why a
        # notification never appeared.
        if result.returncode != 0:
            logger.warning(f"notify-send exited {result.returncode}: {result.stderr.strip()}")
    except (FileNotFoundError, OSError) as e:
        logger.debug(f"Desktop notification unavailable, skipping: {e}")
