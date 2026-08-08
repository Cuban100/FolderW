[← Back to README](../README.md)

## Login

The dashboard has no login page by default. Set a **Dashboard Password** (in `setup.py` or the web Settings page) to require one — only a salted, hashed version of the password is ever stored, never the plaintext. Once set, every page (including Settings) requires logging in first.

Sessions last **15 days** via a signed cookie, so you won't need to log in again on the same browser until it expires or you explicitly log out. Uncheck **Require Login** in Settings to disable the login page again.

<p align="center"><img src="../screenshots/login-page.png" alt="FolderW login page" width="400"></p>

## Notifications

FolderW notifies you when a backup fails, and when the initial full backup completes — through two independent channels, configured in `setup.py` or the web Settings page. **Neither is required**; use one, both, or neither.

### Desktop notifications (no setup needed beyond the checkbox)

Check **"Always send a desktop notification"** to show a notification on this machine's own screen via `notify-send` — no account, service, or internet connection required. This is the simplest option if you only care about being notified on the machine FolderW is running on. Leave it unchecked if you don't want this (e.g. a headless server with no desktop session, or you only want the URL-based notifications below).

### Notification URL(s) (optional — leave blank if you don't need an external platform)

If you want to be notified somewhere *other than* this machine's desktop — your phone, email, a chat app — set **Notification URL(s)**. Uses [Apprise](https://github.com/caronc/apprise), which supports 100+ services through simple URL strings — Pushover, ntfy, Pushbullet, Discord, Telegram, email, and more. Enter one or more, comma-separated:

```
pover://user@token
ntfy://topic
pbul://accesskey
```

See the [Apprise README](https://github.com/caronc/apprise#supported-notifications) for the full list of supported services and URL formats, including how to construct the URL for your specific service (most need an API token or similar, obtained from that service itself). **Leave this field blank if you don't want notifications sent anywhere beyond this machine's desktop** — it's entirely optional and independent of the desktop checkbox above.

Use the **Send Test Notification** button in Settings to verify your setup before saving — it tests whichever of the two channels above is currently filled in/checked on the form (even before you've saved), and reports each one's result separately.

### Urgency levels

Every notification carries one of three levels, following the desktop notification standard (`notify-send --urgency`) and mapped to the closest Apprise severity for URL-based services too:

- **Critical** — a backup failed.
- **Normal** — the initial full backup completed successfully.

(There's currently no built-in **Low**-level notification; the level system supports it for future use.)
