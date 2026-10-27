"""UI language. PURE -- knows no files; the preference lives in store.setting("lang").

    t("Doğru hamle.", "Good move.")

t() returns the selected language AT CALL TIME. Text assigned to a constant at
module top level freezes in whatever language was active at import and never
changes again. Text is always produced where it is used.

Two languages, no key file: when both versions of a text sit side by side, a
missing or stale translation is visible while reading the code.
"""
import locale

LANGUAGES = {"tr": "Türkçe", "en": "English"}
_selected = "tr"


def set_lang(code):
    """Unknown codes fall back to Turkish -- the app's source language."""
    global _selected
    _selected = code if code in LANGUAGES else "tr"
    return _selected


def selected():
    return _selected


def t(tr, en):
    return en if _selected == "en" else tr


def system_lang():
    """When no preference is saved: tr if the operating system is Turkish, else en."""
    try:
        name = (locale.getlocale()[0] or "").lower()
    except (ValueError, TypeError):
        name = ""
    return "tr" if name.startswith(("tr", "turkish")) else "en"
