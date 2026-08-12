// Client-side counterpart to translations/__init__.py's t() -- reads
// window.FOLDERW_I18N, a JSON dump of the CURRENT LANGUAGE's whole dict
// (see server.py's _global_template_context()'s i18n_json, emitted as
// an inline <script> immediately before this file loads on the pages
// that need it: index.html and settings.html, whose own inline JS
// builds dynamic text server-side rendering can't reach -- live
// watchdog countdown, SSE progress text, the pluralized update-count
// banner, the watchdog-delay slider's live unit label.
//
// Deliberately the WHOLE current-language dict, not a page-curated
// subset: one JSON blob shape everywhere avoids a second, easy-to-
// forget "which keys does this page's JS actually use" list to keep in
// sync as strings are added later.
function t(key, params) {
    const template = (window.FOLDERW_I18N && window.FOLDERW_I18N[key]) || key;
    if (!params) return template;
    return template.replace(/\{(\w+)\}/g, (match, name) => (name in params) ? params[name] : match);
}
