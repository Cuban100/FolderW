"""Single source of truth for FolderW's UI strings, in both Python
(Jinja templates, server.py/statistics_operations.py messages) and
JavaScript (index.html/settings.html's live-updating text, via the
i18n_json blob injected by server.py's _global_template_context()).

Deliberately flat dicts, not gettext/.po/.mo -- two languages, one
maintainer, no build step. See translations/en.py / translations/es.py
for the actual strings.
"""
from loguru import logger
from db_operations import load_env_value
from translations import en, es

LANGUAGES = {"en": en.TRANSLATIONS, "es": es.TRANSLATIONS}
DEFAULT_LANGUAGE = "en"
SUPPORTED_LANGUAGES = tuple(LANGUAGES.keys())

_MISSING = object()


def current_language():
    # Always re-reads .env (load_env_value() already reparses it on every
    # call, see db_operations.py) -- same "always fresh" convention as
    # every other setting (MONITOR, BACKUP_METHOD, etc.), nothing to
    # invalidate when the footer switcher saves a new value.
    lang = load_env_value('LANGUAGE')
    return lang if lang in LANGUAGES else DEFAULT_LANGUAGE


def t(key, lang=None, **kwargs):
    """t('settings_title') -> plain lookup.
    t('settings_error_overlap', container_dir=x, app_dir_real=y) -> .format(**kwargs).

    Fallback chain, matching FolderW's existing "fail loud" philosophy
    (e.g. load_env_value()'s own logger.error on a missing var):
      1. key in the active language -> that value.
      2. key missing in the active language, present in English -> English
         (a page translated for "en" but not yet for "es" reads in
         English rather than going blank).
      3. key missing everywhere -> the raw key itself, logged as an
         error, so a typo'd/forgotten key is visibly wrong on screen
         instead of silently disappearing.
    """
    lang = lang or current_language()
    value = LANGUAGES.get(lang, LANGUAGES[DEFAULT_LANGUAGE]).get(key, _MISSING)
    if value is _MISSING:
        value = LANGUAGES[DEFAULT_LANGUAGE].get(key, _MISSING)
    if value is _MISSING:
        logger.error(f"Translation key not found in any language: '{key}'")
        return key
    if not kwargs:
        return value
    try:
        return value.format(**kwargs)
    except (KeyError, IndexError) as e:
        # A renamed/removed kwarg shouldn't 500 the whole page -- show
        # the raw unformatted template (still visibly a translation,
        # just with literal {placeholders}) rather than crashing.
        logger.error(f"Translation key '{key}' ({lang}) .format() failed: {e}")
        return value


def t_or_raw(key, raw_value, lang=None):
    """Like t(), but for values that used to be stored as pre-formatted
    English text directly in the database (e.g. restore_runs.restore_type
    before this was switched to stable snake_case keys) -- a legacy row
    whose stored value doesn't match any known key shows its own
    original text verbatim instead of t()'s usual "log + show the raw
    key" fallback, which would otherwise leak the literal lookup key
    (e.g. "restore_type_Selected Files") onto the page.
    """
    lang = lang or current_language()
    value = LANGUAGES.get(lang, LANGUAGES[DEFAULT_LANGUAGE]).get(key, _MISSING)
    if value is _MISSING:
        value = LANGUAGES[DEFAULT_LANGUAGE].get(key, _MISSING)
    return raw_value if value is _MISSING else value
