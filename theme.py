#!/usr/bin/env python3
"""Themes: design tokens + QSS generation. Does not import Qt.

Code asks for a token, not a color; when the theme changes only the dict
changes. Hand-drawn widgets (board, strip, charts) read the token WHILE
DRAWING -- one that copied it at setup would stay in the old color.
"""
import os

import i18n

TYPEFACE = "Segoe UI"   # one family for the whole app; no monospace (Consolas)

# bg: window · panel: text box/list · ink · faint: secondary text ·
# edge · button / button_hover · accent / accent_hover · accent_text ·
# sidebar / side_text: sidebar · board · grid · black / white: stones ·
# stone_edge · grain: wood grain · shadow: stone shadow + radial gradient
THEMES = {
    "classic": dict(
        name=("Klasik", "Classic"),
        bg="#f4f1ea", panel="#ffffff", ink="#1f1b16", faint="#7d766a",
        edge="#ddd5c7", button="#ffffff", button_hover="#f3eee5", accent="#2f6fd6",
        accent_hover="#255cb5", accent_text="#ffffff", sidebar="#e9e3d8", side_text="#1f1b16",
        board="#e8c88a", grid="#3a2f1c", black="#1a1a1a", white="#fdfdfd",
        stone_edge="#3a2f1c", grain=False, shadow=False),
    "wood": dict(
        name=("Ahşap", "Wood"),
        bg="#f3ece0", panel="#fffdf9", ink="#2b2116", faint="#8a7a62",
        edge="#dccdb3", button="#fffdf9", button_hover="#f5ebdb", accent="#a0561c",
        accent_hover="#86470f", accent_text="#ffffff", sidebar="#33251a", side_text="#f1e6d4",
        board="#dcb363", grid="#2b1f10", black="#141414", white="#f4f1ea",
        stone_edge="#2b1f10", grain=True, shadow=True),
    "dark": dict(
        name=("Karanlık", "Dark"),
        bg="#1c1d20", panel="#25272b", ink="#e6e6e6", faint="#9a9ca3",
        edge="#3a3d44", button="#2c2f34", button_hover="#353940", accent="#5b9cf0",
        accent_hover="#74acf3", accent_text="#0e1116", sidebar="#141518", side_text="#e6e6e6",
        board="#5e5241", grid="#c9bda6", black="#0b0b0b", white="#e6e2da",
        stone_edge="#0b0b0b", grain=False, shadow=True),
    "paper": dict(
        name=("Kağıt", "Paper"),
        bg="#ffffff", panel="#ffffff", ink="#111111", faint="#8a8a8a",
        edge="#e2e2e2", button="#ffffff", button_hover="#f4f4f4", accent="#111111",
        accent_hover="#333333", accent_text="#ffffff", sidebar="#f7f7f7", side_text="#111111",
        board="#ffffff", grid="#9a9a9a", black="#111111", white="#ffffff",
        stone_edge="#111111", grain=False, shadow=False),
    "contrast": dict(
        name=("Yüksek kontrast", "High contrast"),
        bg="#000000", panel="#000000", ink="#ffffff", faint="#ffe14d",
        edge="#ffffff", button="#000000", button_hover="#333333", accent="#ffe14d",
        accent_hover="#fff07a", accent_text="#000000", sidebar="#000000", side_text="#ffffff",
        board="#ffd21f", grid="#000000", black="#000000", white="#ffffff",
        stone_edge="#000000", grain=False, shadow=False),
}
DEFAULT = "wood"
_active = THEMES[DEFAULT]


def select(name):
    global _active
    _active = THEMES.get(name, THEMES[DEFAULT])
    return _active


def active():
    return _active


def its_name(name):
    return i18n.t(*THEMES[name]["name"])


def ok_svg(folder, color):
    """Combo box arrow. The QSS border-triangle trick draws a rectangle in Qt;
    a small SVG in the token color is written instead (if missing). Returns a
    '/'-separated path."""
    path_ = os.path.join(folder, f"ok_{color.lstrip('#')}.svg")
    if not os.path.exists(path_):
        os.makedirs(folder, exist_ok=True)
        with open(path_, "w", encoding="utf-8") as f:
            f.write('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="6" '
                    f'viewBox="0 0 10 6"><path d="M1 1 L5 5 L9 1" fill="none" '
                    f'stroke="{color}" stroke-width="1.6" stroke-linecap="round"/></svg>')
    return path_.replace(os.sep, "/")


def stylesheet(t=None, ok=None):
    """QSS from the tokens. Applied app-wide (dialogs included).
    objectName instead of inline colors: #faint, #state, #job, #title, #section,
    #warning, #sidebar, #live, #choice; primary button: property primary=true."""
    t = t or _active
    return f"""
* {{ font-family:"{TYPEFACE}"; }}
QWidget {{ background:{t['bg']}; color:{t['ink']}; font-size:10pt; }}
QLabel {{ background:transparent; }}
QTextEdit, QTextBrowser, QListWidget {{ background:{t['panel']}; border:1px solid {t['edge']};
    border-radius:8px; padding:4px; selection-background-color:{t['accent']};
    selection-color:{t['accent_text']}; }}
QScrollArea {{ background:{t['panel']}; border:1px solid {t['edge']}; border-radius:8px; }}
QListWidget {{ outline:none; }}
QListWidget::item {{ padding:7px 6px; border-radius:6px; }}
QListWidget::item:hover {{ background:{t['button_hover']}; }}
QListWidget::item:selected {{ background:{t['accent']}; color:{t['accent_text']}; }}
QListWidget#gamelist {{ background:{t['bg']}; border:none; padding:0; }}


QPushButton {{ background:{t['button']}; color:{t['ink']}; border:1px solid {t['edge']};
    border-radius:8px; padding:7px 16px; font-weight:600; outline:none; }}
QPushButton:hover {{ background:{t['button_hover']}; border-color:{t['accent']}; }}
QPushButton:pressed {{ background:{t['edge']}; }}
QPushButton:checked, QPushButton[primary="true"] {{ background:{t['accent']};
    color:{t['accent_text']}; border-color:{t['accent']}; }}
QPushButton:checked:hover, QPushButton[primary="true"]:hover {{ background:{t['accent_hover']};
    border-color:{t['accent_hover']}; }}
QPushButton:disabled {{ background:{t['bg']}; color:{t['faint']}; border-color:{t['edge']}; }}

QComboBox {{ background:{t['button']}; color:{t['ink']}; border:1px solid {t['edge']};
    border-radius:8px; padding:6px 28px 6px 12px; font-weight:600; }}
QComboBox:hover {{ border-color:{t['accent']}; }}
QComboBox::drop-down {{ border:none; width:24px; }}
QComboBox::down-arrow {{ {f'image:url("{ok}");' if ok else ''} width:10px; height:6px;
    margin-right:10px; }}
QComboBox QAbstractItemView {{ background:{t['panel']}; color:{t['ink']};
    border:1px solid {t['edge']}; outline:none; padding:4px;
    selection-background-color:{t['accent']}; selection-color:{t['accent_text']}; }}

QTabBar::tab {{ background:transparent; color:{t['faint']}; padding:9px 18px; border:none;
    border-bottom:2px solid {t['edge']}; font-weight:600; }}
QTabBar::tab:selected {{ color:{t['accent']}; border-bottom:2px solid {t['accent']}; }}
QTabBar::tab:hover {{ color:{t['ink']}; }}

QScrollBar:vertical {{ background:transparent; width:10px; margin:2px; }}
QScrollBar:horizontal {{ background:transparent; height:10px; margin:2px; }}
QScrollBar::handle:vertical {{ background:{t['edge']}; border-radius:3px; min-height:30px; }}
QScrollBar::handle:horizontal {{ background:{t['edge']}; border-radius:3px; min-width:30px; }}
QScrollBar::handle:hover {{ background:{t['faint']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width:0; height:0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background:transparent; }}

QSlider::groove:horizontal {{ background:{t['edge']}; height:4px; border-radius:2px; }}
QSlider::sub-page:horizontal {{ background:{t['accent']}; border-radius:2px; }}
QSlider::handle:horizontal {{ background:{t['accent']}; width:14px; height:14px;
    margin:-5px 0; border-radius:7px; }}
QCheckBox {{ spacing:8px; }}
QCheckBox::indicator {{ width:16px; height:16px; border:1px solid {t['edge']};
    border-radius:4px; background:{t['panel']}; }}
QCheckBox::indicator:checked {{ background:{t['accent']}; border-color:{t['accent']}; }}

QLabel#faint, QLabel#state {{ color:{t['faint']}; }}
QLabel#job {{ color:{t['accent']}; font-weight:600; }}
QLabel#title {{ font-size:15pt; font-weight:700; }}
QLabel#section {{ font-size:11pt; font-weight:700; padding-top:12px; }}
QLabel#warning {{ background:#c0392b; color:#ffffff; padding:8px; border-radius:6px; }}
QWidget#choice {{ background:{t['panel']}; border:1px solid {t['accent']}; border-radius:8px; }}
QWidget#sidebar {{ background:{t['sidebar']}; }}
QWidget#sidebar QPushButton {{ background:transparent; border:none; border-radius:8px;
    text-align:left; padding:10px 14px; font-size:11pt; color:{t['side_text']}; }}
QWidget#sidebar QPushButton:hover {{ background:rgba(127, 127, 127, 0.20); }}
QWidget#sidebar QPushButton:checked {{ background:{t['accent']}; color:{t['accent_text']}; }}
QPushButton#live {{ background:{t['accent']}; color:{t['accent_text']}; border:none;
    border-radius:0; padding:8px; }}
"""


def demo():
    for name, t in THEMES.items():
        assert set(t) == set(THEMES["classic"]), f"{name} is missing tokens"
        assert t["accent"] in stylesheet(t) and "{t[" not in stylesheet(t)
    assert select("missing") is THEMES[DEFAULT] and select("dark")["name"][0] == "Karanlık"
    select(DEFAULT)
    return "tema ok"


if __name__ == "__main__":
    print(demo())
