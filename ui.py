#!/usr/bin/env python3
"""GoMentor — desktop UI (PySide6/Qt).

Sidebar, one board, one engine:
  Play        Free: quiet, STOPS only on a blunder (card). Ranked: NO feedback,
              scores at the end of the game. Two players: two people at one
              computer, the engine only counts at the end.
  Lesson      generated position -> a PLAN_LEN-move sequence, a card on every move
  Games       saved games; variations, blunder fixing, video, quiz, SGF
  Analysis    mistake review (Leitner) · habits (patterns) · progress
  Settings    theme (5), language, sound, rules, video LLM, developer

Moved here from tkinter. Reason: the window could not be resized, there were
no real tables, and tkinter looks bad on macOS -- where the build for friends
would run. The "zero dependencies" argument was gone anyway once the package
reached 500 MB.

The UI does not talk to the engine directly: it calls the goban/engine/
scoring/store layers. None of those layers changed in the move.

Usage: python ui.py [--rank 15k] [--bot 15k] [--next 9k] [--plan 3] [--size 9]
"""
import argparse, datetime, html, math, os, queue, random, sys, threading, time, traceback

# In a windowless (--windowed) frozen build sys.stdout/stderr are None: an
# import error or an uncaught exception kills the app WITHOUT a trace. That is
# the price of hiding the console; output is tied to a file first and the rest
# is imported afterwards, so a startup error also lands in the log.
if getattr(sys, "frozen", False) and sys.stderr is None:
    try:
        import bundle                    # depends only on os/sys, can load first
        _log = os.path.join(bundle.data_root(), "games", "log")
        os.makedirs(_log, exist_ok=True)
        sys.stdout = sys.stderr = open(os.path.join(_log, "app.log"),
                                       "a", encoding="utf-8", errors="replace",
                                       buffering=1)
    except Exception:
        pass                            # if no log can be kept, the app should still open

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import (QColor, QDesktopServices, QFont, QKeySequence, QPainter,
                           QPen, QPixmap, QPolygonF, QRadialGradient, QShortcut,
                           QTextCursor)
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox,
                               QFileDialog, QGridLayout,
                               QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow,
                               QPushButton, QScrollArea, QSizePolicy, QSlider, QStyle,
                               QStyledItemDelegate,
                               QSplitter, QStackedWidget, QTabBar, QTextBrowser, QTextEdit,
                               QVBoxLayout, QWidget)
# QtMultimedia is in pip's PySide6 and NOT in Anaconda's. Without it the
# video opens in the system player and stone sounds do not play; nothing else
# is affected.
try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QSoundEffect
    from PySide6.QtMultimediaWidgets import QVideoWidget
except ImportError:
    QMediaPlayer = QSoundEffect = None

import i18n, goban, charts, sgf_import, bundle, legacy, store, scoring, theme, tsumego, video
from goban import Board, from_gtp, to_gtp
from engine import Config, Engine
from scoring import REGIONS

C_MINE, C_NEXT, C_BEST, C_ZONE = "#1f6fd0", "#1f9d55", "#cc2b2b", "#cc2b2b"
SIZES = (9, 13, 19)
TIME_MINUTES = (5, 10, 15, 20, 30, 45, 60)     # time control options (minutes)
NL = chr(10)      # a constant, to avoid heredoc/escaping trouble
PROJECT_URL = "https://github.com/latifakgun/gomentor"

# Two warnings that come up in several places on the archive screen: keep the
# text in one place so the translations cannot drift apart. A function, not a
# constant -- the language is read at call time.
PICK_GAME = lambda: i18n.t("Önce listeden bir maç seç.", "Select a game from the list first.")
NO_SCORE = lambda: i18n.t("Bu maçta değerlendirilmiş hamle yok "
                         "(puanlama kaydı tutulmamış).",
                         "No graded moves in this game (no scoring was recorded).")
# Theme preview on the settings screen: a few stones, the last move, the best-move mark.
EXAMPLE = [["B", "C7"], ["W", "G3"], ["B", "C3"], ["W", "G7"], ["B", "E5"], ["W", "F5"],
         ["B", "F4"], ["W", "E6"], ["B", "D6"], ["W", "F6"], ["B", "E7"]]


class BoardView(QWidget):
    """Board drawing + clicks + hover ghost stone. Knows no modes, it only draws.

    The tkinter Canvas kept objects (create_oval returned an id); in Qt drawing
    is immediate: `update()` is called and `paintEvent` redraws everything.
    The screen state comes from a single source (self.board), which is cleaner.
    """

    def __init__(self, on_click, size=9):
        super().__init__()
        self.size = size
        self.on_click = on_click
        self.overlays = []
        self.zone = None
        self.board = Board(size)
        self.live = False
        self.plain = False          # watching: a past position, no overlays/regions
        self.white = False         # is the ghost stone white (White's turn in a two-player game)
        self._hover = None
        self._grain = (None, None)  # (key, QPixmap): wood texture cache
        self.setMinimumSize(380, 380)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    # ---------- geometry: scales with the window ----------
    def _geo(self):
        beside = min(self.width(), self.height())
        h = beside / (self.size + 1)                  # +1 for the margin
        return h, (self.width() - h * (self.size - 1)) / 2, \
            (self.height() - h * (self.size - 1)) / 2

    def xy(self, i):
        h, ox, oy = self._geo()
        r, c = divmod(i, self.size)
        return ox + c * h, oy + r * h

    def hit(self, px, py):
        h, ox, oy = self._geo()
        c, r = round((px - ox) / h), round((py - oy) / h)
        if not (0 <= r < self.size and 0 <= c < self.size):
            return None
        i = r * self.size + c
        x, y = self.xy(i)
        return i if abs(x - px) < h * .45 and abs(y - py) < h * .45 else None

    def draw(self, board, live=True, plain=False, white=False):
        # The size comes from the board. One place: the live game, lessons and the
        # archive all pass through here, and when a game of another size opens in the
        # archive the widget adapts itself.
        self.size = board.size
        self.board, self.live, self.plain, self.white = board, live, plain, white
        self.setCursor(Qt.PointingHandCursor if live else Qt.ArrowCursor)
        self.update()

    # ---------- events ----------
    def mousePressEvent(self, ev):
        i = self.hit(ev.position().x(), ev.position().y())
        if i is not None:
            self.on_click(i)

    def mouseMoveEvent(self, ev):
        i = self.hit(ev.position().x(), ev.position().y())
        if i != self._hover:
            self._hover = i
            self.update()

    def leaveEvent(self, ev):
        self._hover = None
        self.update()

    # ---------- drawing ----------
    def _wood(self, t):
        """Procedural wood texture. Fixed seed: the same board on every draw.
        From the cache as long as size/color do not change."""
        key_ = (self.width(), self.height(), t["board"])
        if self._grain[0] == key_:
            return self._grain[1]
        pm = QPixmap(max(1, self.width()), max(1, self.height()))
        pm.fill(QColor(t["board"]))
        q = QPainter(pm)
        q.setRenderHint(QPainter.Antialiasing)
        r = random.Random(7)
        dark = QColor(t["grid"])
        for _ in range(int(pm.height() / 2.2)):
            y0, gen, phase_ = r.uniform(0, pm.height()), r.uniform(1, 6), r.uniform(0, 6.3)
            wave_ = r.uniform(90, 260)
            dark.setAlpha(r.randint(6, 26))
            q.setPen(QPen(dark, r.uniform(.6, 2.2)))
            pt = [QPointF(x, y0 + gen * math.sin(x / wave_ + phase_))
                   for x in range(0, pm.width() + 24, 24)]
            q.drawPolyline(QPolygonF(pt))
        q.end()
        self._grain = (key_, pm)
        return pm

    def paintEvent(self, ev):
        t = theme.active()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if t["grain"]:
            p.drawPixmap(0, 0, self._wood(t))
        else:
            p.fillRect(self.rect(), QColor(t["board"]))
        h, ox, oy = self._geo()
        pen = QPen(QColor(t["grid"]), 2 if t is theme.THEMES["contrast"] else 1)
        end = ox + h * (self.size - 1)

        if self.zone is not None and not self.plain:
            zr, zc = divmod(self.zone, 3)
            t = self.size / 3.0
            pen_z = QPen(QColor(C_ZONE), 3, Qt.DashLine)
            p.setPen(pen_z)
            p.drawRect(ox + zc * t * h - h * .5, oy + zr * t * h - h * .5,
                       t * h, t * h)

        p.setPen(pen)
        for k in range(self.size):
            a = ox + k * h
            b = oy + k * h
            p.drawLine(ox, b, end, b)
            p.drawLine(a, oy, a, oy + h * (self.size - 1))

        p.setFont(QFont("Segoe UI", max(6, int(h * .22))))
        letter = goban.cols(self.size)
        for k in range(self.size):
            p.drawText(ox + k * h - h * .3, oy - h * .55, h * .6, h * .5,
                       Qt.AlignCenter, letter[k])
            p.drawText(ox - h * .95, oy + k * h - h * .25, h * .6, h * .5,
                       Qt.AlignCenter, str(self.size - k))

        p.setBrush(QColor(t["grid"]))
        yr = max(2.0, h * .07)                     # keep the points from getting tiny on 19x19
        for s in goban.star_points(self.size):
            x, y = self.xy(s)
            p.drawEllipse(x - yr, y - yr, yr * 2, yr * 2)

        r = h * .44
        for i, v in enumerate(self.board.g):
            if not v:
                continue
            x, y = self.xy(i)
            black = v == 1
            if t["shadow"]:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(0, 0, 0, 70))
                p.drawEllipse(QPointF(x + r * .08, y + r * .12), r, r)
                g = QRadialGradient(x - r * .35, y - r * .35, r * 1.3)
                g.setColorAt(0, QColor("#5c5c5c" if black else "#ffffff"))
                g.setColorAt(1, QColor(t["black"] if black else "#c9c5bd"))
                p.setBrush(g)
            else:
                p.setBrush(QColor(t["black"] if black else t["white"]))
                p.setPen(QPen(QColor(t["stone_edge"]), 1.5 if t is theme.THEMES["contrast"] else 1))
            p.drawEllipse(QPointF(x, y), r, r)
            if i == self.board.last:
                p.setBrush(Qt.NoBrush)
                p.setPen(QPen(QColor("#d03030"), 2))
                p.drawEllipse(x - 5, y - 5, 10, 10)

        for coord, color, shape, label in ([] if self.plain else self.overlays):
            try:
                x, y = self.xy(from_gtp(coord, self.size))
            except (ValueError, IndexError, TypeError):
                continue
            d = h * .3
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(color), 4 if shape == "x" else 3))
            if shape == "x":
                p.drawLine(x - d, y - d, x + d, y + d)
                p.drawLine(x - d, y + d, x + d, y - d)
            else:
                p.setPen(QPen(QColor(color), 3 if shape == "tri" else 2))
                p.drawPolygon(QPolygonF([QPointF(x, y - d), QPointF(x + d, y + d),
                                         QPointF(x - d, y + d)]))
            if label:
                p.setFont(QFont("Segoe UI", max(6, int(h * .17)), QFont.Bold))
                p.drawText(x - h * .5, y - h * .78, h, h * .4,
                           Qt.AlignCenter, label)

        if self._hover is not None and self.live and not self.board.g[self._hover]:
            x, y = self.xy(self._hover)
            p.setBrush(QColor(235, 235, 232, 110) if self.white else QColor(26, 26, 26, 90))
            p.setPen(QPen(QColor(t["grid"])))
            p.drawEllipse(x - r, y - r, r * 2, r * 2)


class MoveStrip(QWidget):
    """Move tree strip.

    Circle = move: filled with the mover's color, the move number inside. The
    line coming into a circle FROM THE LEFT is that move's category color
    (gray if uncategorized); the first move is joined to the root by a line
    too. The main line is the top row, each branch on its own row from the
    column where it splits off. Click: on_click(branch, i).
    """
    A, R = 34, 12

    def __init__(self, on_click):
        super().__init__()
        self.on_click = on_click
        self.nodes = []            # (x, y, color, no, category, (ax, ay), branch, i)
        self.selected = None
        self.area = QScrollArea()
        self.area.setWidget(self)
        # A long game's strip must not widen the panel and shrink the board: width scrolls.
        self.area.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

    def rebuild(self, game, categorized, selected):
        """selected: (branch, i). categorized=False: live game -- no categories shown."""
        A, start = self.A, goban.setup_count(game.moves)

        def spot(branch, i):
            branch = game.owner(branch, i)
            row_ = 0 if branch is None else branch + 1
            return A * (i - start + .5), A * (row_ + .5)

        self.nodes = [(A * .5, A * .5, None, 0, None, None, None, start)]      # root
        lines = [(None, start, game.moves[start:])] + \
                 [(j, d["root"], d["moves"]) for j, d in enumerate(game.branches)]
        for branch, root_, moves in lines:
            for k, (col, mv) in enumerate(moves):
                i = root_ + k + 1
                cat = scoring.category(game.record_of(i, branch)) if categorized else None
                self.nodes.append((*spot(branch, i), col, i - start, cat, spot(branch, i - 1), branch, i))
        self.selected = spot(*selected) if selected else None
        wide = max(x for x, *_ in self.nodes) + A
        total_h = max(y for _, y, *_ in self.nodes) + A * .5
        self.setFixedSize(int(wide), int(total_h))
        self.update()
        if self.selected:
            self.area.ensureVisible(int(self.selected[0]), int(self.selected[1]), A * 2, A)

    def mousePressEvent(self, ev):
        px, py = ev.position().x(), ev.position().y()
        for x, y, *_, branch, i in self.nodes:
            if (x - px) ** 2 + (y - py) ** 2 <= (self.R + 3) ** 2:
                self.on_click(branch, i)
                return

    def paintEvent(self, ev):
        t = theme.active()
        gray = t["edge"] if t is not theme.THEMES["contrast"] else "#888888"
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(t["panel"]))
        A, R = self.A, self.R
        # Lines first, in REVERSE order: branches from the same fork share the
        # vertical part of the elbow; the nearest row (and the main line last) stays on top.
        for x, y, col, _, cat, parent, *_ in reversed(self.nodes):
            if not parent:
                continue
            p.setPen(QPen(QColor(scoring.CATEGORY_COLOR.get(cat, gray)), 3))
            ax, ay = parent
            if ay == y:
                p.drawLine(QPointF(ax, ay), QPointF(x, y))
            else:                     # branch: elbow in the in-between column
                middle = ax + A * .5
                p.drawPolyline(QPolygonF([QPointF(ax, ay), QPointF(middle, ay),
                                          QPointF(middle, y), QPointF(x, y)]))
        p.setFont(QFont("Segoe UI", 7, QFont.Bold))
        for x, y, col, no, *_ in self.nodes:
            if col is None:           # root
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(gray))
                p.drawEllipse(QPointF(x, y), 4, 4)
                continue
            black = col == "B"
            p.setPen(QPen(QColor(t["stone_edge"] if t["stone_edge"] != t["panel"] else t["ink"]), 1))
            p.setBrush(QColor(t["black"] if black else t["white"]))
            p.drawEllipse(QPointF(x, y), R, R)
            p.setPen(QColor(t["white"] if black else t["black"]))
            p.drawText(int(x - R), int(y - R), 2 * R, 2 * R, Qt.AlignCenter, str(no))
        if self.selected:
            p.setPen(QPen(QColor(t["accent"]), 2))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QPointF(*self.selected), R + 3, R + 3)


def _event_lines(event):
    """apply_rank_result event -> lines. The end-of-game screen and the archive show the same text."""
    t = []
    if event["kind"] == "placement":
        t.append(i18n.t(f"Yerleştirme: {event['remaining']} maç kaldı.",
                       f"Placement: {event['remaining']} games to go."))
        t.append(i18n.t(f"Sıradaki rakip: {event['next_opponent']}",
                       f"Next opponent: {event['next_opponent']}"))
    elif event["kind"] == "placement_done":
        t.append(i18n.t(f"Yerleştirme bitti  →  ~{event['label']}",
                       f"Placement done  →  ~{event['label']}"))
        t.append(i18n.t(f"Aralık {event['spacing'][0]} – {event['spacing'][1]}"
                       f"  (±{event['uncertainty']} rank)",
                       f"Range {event['spacing'][0]} – {event['spacing'][1]}"
                       f"  (±{event['uncertainty']} ranks)"))
        if event.get("source") == "binary+quality":
            t.append(i18n.t(f"Maç sonuçları: {event['binary']} · "
                           f"hamle kalitesi: {event['quality']}",
                           f"Game results: {event['binary']} · "
                           f"move quality: {event['quality']}"))
        t.append(i18n.t(f"{NL}{scoring.value_rank(event['value'])} / 0 SP ile başlıyorsun.",
                       f"{NL}You start at {scoring.value_rank(event['value'])} / 0 SP."))
    else:
        t.append(f"{event['sp_change']:+.1f} SP"
                 + (i18n.t("   (provizyonel: yeni oyuncu, maçlar daha ağır)",
                          "   (provisional: new player, games count for more)")
                    if event.get("provisional") else ""))
        t.append(f"{event['previous'][0]} {event['previous'][1]:.0f} SP  →  "
                 f"{event['current'][0]} {event['current'][1]:.0f} SP")
        if event["rank_up"] > 0:
            t.append(i18n.t(f"{NL}★ Rank atladın: {event['current'][0]}",
                           f"{NL}★ Rank up: {event['current'][0]}"))
        elif event["rank_up"] < 0:
            t.append(i18n.t(f"{NL}Rank düştü: {event['current'][0]}",
                           f"{NL}Rank down: {event['current'][0]}"))
        if event.get("streak", 0) >= 2:
            t.append(i18n.t(f"{NL}{event['streak']} maçlık seri — bir sonraki rakip daha güçlü.",
                           f"{NL}{event['streak']}-game streak — the next opponent is stronger."))
    return t


def _apply_ranked(game, S, diff, opponent_value, sv, how):
    """Applies a ranked result to the profile and writes it to the game. Also
    called from the worker thread, so it does not touch the UI. The profile
    only keeps the LATEST state; per-game SP exists nowhere else, so it goes
    into the jsonl for the archive to show."""
    d, event = store.apply_rank_result(game.size, S, opponent_value,
                                    sv["value"] if sv else None)
    event["streak"] = d["streak"]
    store.add_record(game, {"game_end": {
        "result": how, "won": S, "score": diff,
        "opponent": scoring.value_rank(opponent_value), "sp": event}})
    return event


# Mode tint in the game list: blended into the panel color by 10%, "not too different".
KIND_TINT = {"ranked": "#c0662b", "game": "#3f8f5a", "lesson": "#3b72c4", "sgf": "#8a5bb0",
            "duo": "#2f8f8f"}


def _blend(a, b, ratio):
    """Blends two #rrggbb colors (ratio: b's share)."""
    ca, cb = QColor(a), QColor(b)
    return QColor(*(round(x + (y - x) * ratio) for x, y in
                    ((ca.red(), cb.red()), (ca.green(), cb.green()), (ca.blue(), cb.blue())))).name()


class GameRow(QStyledItemDelegate):
    """A card-like row in the game list: mode tint (UserRole), rounded corners,
    selected = accent. When ::item was styled in QSS, Qt ignored the item's
    background; the tint only shows when drawn by hand."""

    def paint(self, p, opt, idx):
        t = theme.active()
        r = QRectF(opt.rect).adjusted(2, 3, -2, -3)
        selected = bool(opt.state & QStyle.State_Selected)
        upper = bool(opt.state & QStyle.State_MouseOver)
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor(t["accent"] if selected or upper else t["edge"]), 1))
        p.setBrush(QColor(t["accent"] if selected else idx.data(Qt.UserRole) or t["panel"]))
        p.drawRoundedRect(r, 8, 8)
        p.setPen(QColor(t["accent_text"] if selected else t["ink"]))
        area = r.adjusted(14, 0, -10, 0)
        p.drawText(area, Qt.AlignVCenter | Qt.AlignLeft,
                   opt.fontMetrics.elidedText(idx.data(), Qt.ElideRight, int(area.width())))
        p.restore()

    def sizeHint(self, opt, idx):
        return QSize(10, 48)          # width: as wide as the view (no horizontal scrolling)


def _silent_wav(path_):
    """A 1 s inaudible WAV (+-1 sample jitter: a stream of pure zeros is treated
    as idle and shut off by some drivers). Looped to keep headphones awake."""
    if not os.path.exists(path_):
        import struct, wave
        os.makedirs(os.path.dirname(path_), exist_ok=True)
        with wave.open(path_, "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(44100)
            w.writeframes(b"".join(struct.pack("<hh", 1 - 2 * (i % 2), 1 - 2 * (i % 2))
                                   for i in range(44100)))
    return path_


def _big(t):
    """First letter uppercase (Turkish i -> İ)."""
    return ("İ" if t[:1] == "i" else t[:1].upper()) + t[1:] if t else t


class Status(QLabel):
    """Status line: written in lowercase from all over the code, capitalized on screen."""

    def setText(self, t):
        super().setText(_big(t))


def _primary(b):
    """Primary action button (accent color). QSS: QPushButton[primary="true"]."""
    b.setProperty("primary", True)
    return b


def _txt():
    t = QTextBrowser()               # QTextEdit + link clicks (card details)
    t.setOpenLinks(False)
    t.document().setDocumentMargin(10)
    return t


def _html(text_, extra=""):
    """Plain text -> readable HTML (line breaks and spaces kept).
    No monospace text: places that need alignment pass an extra HTML table."""
    return (f'<p style="margin:0; white-space:pre-wrap; line-height:125%">'
            f'{html.escape(text_)}</p>' + extra)


def _min_sec(ms):
    seconds = max(0, ms) // 1000
    return f"{seconds // 60}:{seconds % 60:02}"


class VideoView(QWidget):
    """The analysis video inside the app. While it is being made the same window
    says 'generating', and App plays it on its own when done. One window,
    reused across games (self.name holds which game it belongs to)."""

    def __init__(self, restart):
        super().__init__()
        self.name = None
        self.state_ = QLabel(alignment=Qt.AlignCenter, wordWrap=True)
        self.state_.setStyleSheet("font-size:13pt;")
        self.screen_ = QVideoWidget()
        self.stack = QStackedWidget()
        self.stack.addWidget(self.state_)
        self.stack.addWidget(self.screen_)

        self.player_ = QMediaPlayer(self)
        self.sound = QAudioOutput(self)            # Qt 6: without this connection the video is silent
        self.player_.setAudioOutput(self.sound)
        self.player_.setVideoOutput(self.screen_)

        self.b_playback = _primary(QPushButton(i18n.t("Duraklat", "Pause")))
        self.b_playback.clicked.connect(self.play_pause)
        self.bar_ = QSlider(Qt.Horizontal)
        self.bar_.sliderMoved.connect(self.player_.setPosition)
        self.time_s = QLabel("0:00 / 0:00")
        self.b_restart = QPushButton(i18n.t("Yeniden üret", "Regenerate"))
        self.b_restart.clicked.connect(lambda: restart(self.name))

        self.player_.durationChanged.connect(lambda ms: self.bar_.setRange(0, ms))
        self.player_.positionChanged.connect(self._position)
        self.player_.playbackStateChanged.connect(self._button)
        self.player_.errorOccurred.connect(
            lambda _, text_: self.error(i18n.t(f"Video oynatılamadı: {text_}",
                                             f"Could not play video: {text_}")))

        bottom = QHBoxLayout()
        for w in (self.b_playback, self.bar_, self.time_s, self.b_restart):
            bottom.addWidget(w, 1 if w is self.bar_ else 0)
        spot = QVBoxLayout(self)
        spot.addWidget(self.stack, 1)
        spot.addLayout(bottom)

    def generating(self, text_):
        # On Windows ffmpeg cannot write over an open file: release it first.
        self.player_.stop()
        self.player_.setSource(QUrl())
        self.error(text_)
        self.b_restart.setEnabled(False)

    def error(self, text_):
        self.state_.setText(text_)
        self.stack.setCurrentWidget(self.state_)
        self.b_playback.setEnabled(False)
        self.bar_.setEnabled(False)
        self.b_restart.setEnabled(True)

    def playback(self, path_):
        self.player_.setSource(QUrl.fromLocalFile(path_))
        self.stack.setCurrentWidget(self.screen_)
        for w in (self.b_playback, self.bar_, self.b_restart):
            w.setEnabled(True)
        self.player_.play()

    def play_pause(self):
        if self.player_.playbackState() == QMediaPlayer.PlayingState:
            self.player_.pause()
        else:
            self.player_.play()

    def _position(self, ms):
        if not self.bar_.isSliderDown():
            self.bar_.setValue(ms)
        self.time_s.setText(f"{_min_sec(ms)} / {_min_sec(self.player_.duration())}")

    def _button(self, state_):
        self.b_playback.setText(i18n.t("Duraklat", "Pause") if state_ == QMediaPlayer.PlayingState
                             else i18n.t("Oynat", "Play"))

    def hideEvent(self, ev):
        # When the page closes (Back, screen change) the file is released here.
        self.player_.stop()
        self.player_.setSource(QUrl())
        super().hideEvent(ev)


class SelectionBar(QWidget):
    """A question inside the panel instead of a separate window (message box):
    text + options. The first option is the primary button. If an option's
    function is None it only closes."""

    def __init__(self):
        super().__init__(objectName="choice")
        self.setAttribute(Qt.WA_StyledBackground, True)
        d = QVBoxLayout(self)
        d.setContentsMargins(12, 10, 12, 10)
        self.text_ = QLabel(wordWrap=True)
        d.addWidget(self.text_)
        self.row_ = QHBoxLayout()
        d.addLayout(self.row_)
        self.hide()

    def ask(self, text_, options):
        while self.row_.count():
            w = self.row_.takeAt(0).widget()
            if w:
                w.deleteLater()
        self.text_.setText(text_)
        for i, (label, fn) in enumerate(options):
            b = QPushButton(label)
            if i == 0:
                b.setProperty("primary", True)
            b.clicked.connect(lambda _=False, f=fn: self._choose(f))
            self.row_.addWidget(b)
        self.row_.addStretch(1)
        self.show()

    def _choose(self, fn):
        self.hide()
        if fn:
            fn()


class Welcome(QWidget):
    """First-run page (not a window)."""

    def __init__(self, new, knower):
        super().__init__()
        d = QVBoxLayout(self)
        d.setContentsMargins(60, 60, 60, 60)
        d.addStretch(1)
        b = QLabel(i18n.t("GoMentor'a hoş geldin", "Welcome to GoMentor"), objectName="title")
        d.addWidget(b)
        m = QLabel(i18n.t("Go biliyor musun? Yeni başlıyorsan önce kısa bir kural turu, "
                         "sonra 9x9'da kolay bir bot. Biliyorsan 9x9 dereceli "
                         "yerleştirme maçıyla seviyeni buluruz.",
                         "Do you know Go? If you are new: a short rules tour, then an easy "
                         "bot on 9x9. If you know it: a 9x9 ranked placement game."),
                   wordWrap=True)
        m.setStyleSheet("font-size:12pt;")
        d.addWidget(m)
        row_ = QHBoxLayout()
        for label, fn, main_ in ((i18n.t("Yeni başlıyorum", "I'm new"), new, True),
                                (i18n.t("Go biliyorum", "I know Go"), knower, False)):
            k = QPushButton(label)
            k.setProperty("primary", main_)
            k.setMinimumHeight(44)
            k.clicked.connect(fn)
            row_.addWidget(k)
        row_.addStretch(1)
        d.addLayout(row_)
        d.addStretch(2)
        self.buttons_ = row_


class RulesTour(QWidget):
    """A 7-page rules tour for beginners. Each page: a small 9x9 diagram with
    setup stones + two or three sentences."""

    PAGES = [
        (("Taşlar", "Stones"),
         ("İki oyuncu sırayla çizgilerin kesiştiği noktalara taş koyar. Siyah başlar. "
          "Konan taş bir daha hareket etmez.",
          "Two players take turns placing stones on the intersections. Black starts. "
          "A stone never moves once placed."),
         [["AB", "C7"], ["AW", "G3"], ["AB", "C3"], ["AW", "G7"], ["AB", "E5"]], []),
        (("Nefes", "Liberties"),
         ("Bir taşın yanındaki (çapraz değil) boş noktalar onun nefesidir. "
          "Ortadaki taşın dört nefesi var.",
          "The empty points next to a stone (not diagonal) are its liberties. "
          "The stone in the middle has four."),
         [["AB", "E5"]], [("E6", "tri2"), ("E4", "tri2"), ("D5", "tri2"), ("F5", "tri2")]),
        (("Esir almak", "Capturing"),
         ("Beyaz taşın tek nefesi kaldı (atari). Siyah ✕'e oynarsa bütün nefesleri "
          "kapanır: taş esir alınır ve tahtadan kalkar.",
          "The white stone has one liberty left (atari). If black plays ✕, the stone "
          "has no liberties: it is captured and removed."),
         [["AB", "D5"], ["AW", "E5"], ["AB", "F5"], ["AB", "E6"]], [("E4", "x")]),
        (("Grup", "Groups"),
         ("Yan yana taşlar bir gruptur: nefesleri ortaktır, birlikte yaşar birlikte ölür.",
          "Connected stones form a group: they share liberties and live or die together."),
         [["AB", "D5"], ["AB", "E5"], ["AB", "E4"], ["AW", "D6"], ["AW", "F5"]], []),
        (("İki göz", "Two eyes"),
         ("İçinde iki ayrı boş nokta (göz) olan grup ölmez: rakip iki göze aynı anda "
          "oynayamaz. Yaşamak = iki göz.",
          "A group with two separate empty points inside (eyes) cannot die: the "
          "opponent cannot fill both at once. Living = two eyes."),
         [["AB", c] for c in ("A2", "B2", "C2", "D2", "D1", "B1")],
         [("A1", "tri2"), ("C1", "tri2")]),
        (("Ko", "Ko"),
         ("Tek taş alındıktan hemen sonra aynı taşı geri almak yasak (ko). Önce başka "
          "bir yere oyna; bir sonraki turda alabilirsin.",
          "Right after a single stone is captured you may not immediately take it "
          "back (ko). Play elsewhere first."),
         [["AB", "D5"], ["AB", "E6"], ["AB", "E4"], ["AW", "F6"], ["AW", "F4"],
          ["AW", "G5"], ["AW", "E5"]], [("F5", "x")]),
        (("Sonuç", "Winning"),
         ("Amaç tahtanın daha büyük kısmını çevirmek. Sayım: taşların + çevirdiğin boş "
          "noktalar. Beyaz sonra başladığı için 7.5 puan (komi) alır. Oynayacak kârlı "
          "hamle kalmayınca 'pas'; iki pas oyunu bitirir ve GoMentor sayar. Hazırsın!",
          "The goal is to surround more of the board. Score: your stones + the empty "
          "points you surround. White gets 7.5 points (komi) for moving second. When "
          "nothing useful is left, 'pass'; two passes end the game. You're ready!"),
         [["AB", c] for c in ("D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8", "D9")]
         + [["AW", c] for c in ("F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9")], []),
    ]

    def __init__(self, done_):
        super().__init__()
        self.done_ = done_
        self.i = 0
        self.board_ = BoardView(lambda _: None, 9)
        self.board_.setFixedSize(560, 560)          # square: keep the texture from spilling outside the grid
        self.title = QLabel(objectName="title")
        self.text_ = QLabel(wordWrap=True)
        self.text_.setStyleSheet("font-size:12pt;")
        self.counter = QLabel(objectName="faint")
        self.b_back = QPushButton(i18n.t("Geri", "Back"))
        self.b_forward = _primary(QPushButton())
        self.b_back.clicked.connect(lambda: self.go(-1))
        self.b_forward.clicked.connect(lambda: self.go(1))
        d = QVBoxLayout(self)
        d.setContentsMargins(24, 12, 24, 16)
        d.addWidget(self.title)
        d.addWidget(self.board_, 0, Qt.AlignHCenter)
        d.addWidget(self.text_)
        bottom = QHBoxLayout()
        bottom.addWidget(self.counter)
        bottom.addStretch(1)
        bottom.addWidget(self.b_back)
        bottom.addWidget(self.b_forward)
        d.addLayout(bottom)
        self.go(0)

    def go(self, d):
        if self.i + d >= len(self.PAGES):
            self.done_()
            return
        self.i = max(0, self.i + d)
        title, text_, stones_, mark = self.PAGES[self.i]
        self.title.setText(i18n.t(*title))
        self.text_.setText(i18n.t(*text_))
        self.counter.setText(f"{self.i + 1} / {len(self.PAGES)}")
        self.b_back.setEnabled(self.i > 0)
        self.b_forward.setText(i18n.t("Bitir", "Finish") if self.i == len(self.PAGES) - 1
                             else i18n.t("İleri", "Next"))
        self.board_.overlays = [(k, C_BEST if s == "x" else C_NEXT, s, "") for k, s in mark]
        self.board_.draw(goban.replay(stones_, 9), False)


class App(QMainWindow):
    """Sidebar + board + right panel. Five screens: Play, Lesson, Games,
    Analysis, Settings. One board, one engine.

    The logic came over from the tkinter version: phase machine, queue+pump,
    generation counter, button states. The SCREEN (self.mode) and the GAME
    (self.game.mode) are separate: a live game does not die when you switch
    screens, it carries on when you come back.
    """

    RANKED_SETTINGS = dict(temp=0.70, tail=0.01, pass_=True)
    QUIZ_THRESHOLD, QUIZ_COUNT = 3.0, 5
    SCREENS = (("play", "⚫", ("Oyna", "Play")), ("lesson", "📖", ("Ders", "Lesson")),
                ("problems", "🧩", ("Problemler", "Problems")),
                ("games", "🗂", ("Maçlar", "Games")), ("analysis", "📊", ("Analiz", "Analysis")))

    def __init__(self, my_rank, bot_rank, next_rank, plan_len):
        super().__init__()
        self.q = queue.Queue()
        self._muted = False   # keep a programmatic selection from firing the signal back
        self.my_rank, self.bot_rank, self.next_rank = my_rank, bot_rank, next_rank
        self.opponent_value = scoring.rank_value(bot_rank)
        self.plan_len = plan_len
        self.resume = None      # --resume: name of the game to resume once the engine is up
        self.cfg = Config.env()
        self.eng = None
        self.phase, self.gen = "boot", 0
        self._today = datetime.date.today          # can be changed for tests
        # Language and theme BEFORE the UI is built: t() and the tokens are read at
        # call time, so widgets are born in the right language and colors.
        i18n.set_lang(store.setting("lang") or i18n.system_lang())
        self._apply_theme(store.setting("theme") or theme.DEFAULT)
        self.setWindowTitle("GoMentor")
        self.resize(1280, 800)

        legacy.migrate(store.ROOT)   # data saved under the old name
        store.arrange()          # move the old flat layout into subfolders

        self.screen_ = "play"
        self.watch = None              # a past position viewed during a live game (move count)
        self.game = store.Game(name=store.new_name("game"), size=self.cfg.size, komi=self.cfg.komi)
        self.board = Board(self.cfg.size)
        self.task, self.facts = None, None
        self.step, self.attempts, self.scores = 0, 0, []
        self.archive, self.archive_i, self.archive_branch = None, 0, None
        self.questions, self.question_i = [], 0
        self.saved = []
        self._video_view, self._video_game = None, None   # player; the game being generated
        self._sgf_name = None                          # the import being graded
        self._branch_job = None                          # the fix attempt being graded
        self._intro, self._details = "", False       # game intro text; card details
        self._game_title = ""
        self.review = None                           # (card, game, move_no, answer)
        self.review_board = None                     # board of the card being answered
        self.prob = None                             # Problems: (card, game, move_no, answer)
        self.prob_board = None
        self.prob_set = None                         # selected set; None = the root list
        self.prob_source = None                      # "mistakes" | "set"
        self.prob_queue = []                        # cards left in the "mistakes" source
        self.prob_rows = []                      # list row -> (kind, value)
        self._setup_sound()

        self._build_ui()

        for keycode, fn in ((Qt.Key_Left, lambda: self.navigate(-1)),
                        (Qt.Key_Right, lambda: self.navigate(1)),
                        (Qt.Key_Home, lambda: self.navigate(-10 ** 6)),
                        (Qt.Key_End, lambda: self.navigate(10 ** 6)),
                        (Qt.Key_Escape, self.back_to_live)):
            QShortcut(QKeySequence(keycode), self, activated=fn)

        # No SEPARATE QTimer for the clock: the pump already runs every 80 ms.
        self._clock_mark = self._clock_last = None
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.pump)
        self.timer.start(80)

        bad = bundle.non_ansi()
        if bad:
            # A strip above the panel, not a message box: no separate windows.
            # The engine may still start (KataGo found elsewhere): the strip only shows on
            # a startup error, so a working install gets no false alarm.
            self.warning.setText(i18n.t(
                "Uygulamanın yolunda Windows'un ANSI kod sayfasına "
                "çevrilemeyen karakterler var: " + bad + NL
                + "KataGo bu yolu açamayabilir. Motor başlamazsa "
                "uygulamayı ASCII karakterli bir klasöre taşı.",
                "The app's path contains characters that Windows' ANSI code page "
                "cannot represent: " + bad + NL
                + "KataGo may fail to open this path. If the engine does not start, "
                "move the app to a folder with ASCII-only characters."))
        self.run(lambda: Engine(self.cfg), self._booted)

    def _build_ui(self):
        """Builds every widget. Called again when the language changes: t() is read
        at call time, so labels already built stay in the old language -- the
        only honest refresh is to rebuild the widgets. The game state lives in
        self.game/self.board/self.phase, not in widgets; without that split,
        changing the language would kill a live game."""
        self.canvas = BoardView(self.click, self.cfg.size)
        # Over the board while looking at a past move in a live game: clicks do not
        # play, and this strip or Esc returns to the live position.
        self.b_live = QPushButton(objectName="live")
        self.b_live.clicked.connect(self.back_to_live)
        self.b_live.hide()
        self.left = QWidget()
        sd = QVBoxLayout(self.left)
        sd.setContentsMargins(0, 0, 0, 0)
        sd.setSpacing(0)
        sd.addWidget(self.b_live)
        sd.addWidget(self.canvas, 1)

        plenty = QSplitter(Qt.Horizontal)
        plenty.addWidget(self.left)
        plenty.addWidget(self._right_panel())
        plenty.setStretchFactor(0, 3)
        plenty.setStretchFactor(1, 2)
        plenty.setSizes([720, 480])
        # Features do not open in separate windows: a page with "← Back" replaces board+panel.
        self.content = QStackedWidget()
        self.content.addWidget(plenty)
        page = QWidget()
        sd = QVBoxLayout(page)
        sd.setContentsMargins(16, 12, 16, 12)
        upper = QHBoxLayout()
        b = QPushButton(i18n.t("← Geri", "← Back"))
        b.clicked.connect(self.close_page)
        upper.addWidget(b)
        self.page_title = QLabel(objectName="title")
        upper.addWidget(self.page_title, 1)
        sd.addLayout(upper)
        self._page_layout = sd
        self.content.addWidget(page)
        self._open_page = None
        main_ = QWidget()
        horizontal = QHBoxLayout(main_)
        horizontal.setContentsMargins(0, 0, 0, 0)
        horizontal.setSpacing(0)
        horizontal.addWidget(self._sidebar())
        horizontal.addWidget(self.content, 1)
        self.setCentralWidget(main_)

        self.panel()
        self.buttons()
        self.refresh()

    # ---------- theme / sound ----------
    def _apply_theme(self, name):
        """QSS goes app-wide (dialogs included); hand-drawn widgets read the token
        while drawing, so update() is enough."""
        t = theme.select(name)
        app = QApplication.instance()
        if app:
            app.setFont(QFont(theme.TYPEFACE, 10))
            ok = theme.ok_svg(os.path.join(store.ROOT, "theme"), t["ink"])   # derived
            app.setStyleSheet(theme.stylesheet(t, ok))
        charts.PAPER, charts.GRID, charts.TEXT, charts.FAINT = (
            t["panel"], t["edge"], t["ink"], t["faint"])
        for w in (getattr(self, "canvas", None), getattr(self, "live_strip", None),
                  getattr(self, "archive_strip", None)):
            if w:
                w.update()

    def theme_select(self, name):
        self._apply_theme(name)
        if self.saved:                      # list tints depend on the theme color
            row_ = self.game_list.currentRow()
            self.archive_fill()
            self._muted = True
            self.game_list.setCurrentRow(row_)
            self._muted = False
        store.set_setting("theme", name)
        for b in getattr(self, "theme_buttons", {}).values():
            b.setChecked(b.property("theme") == name)

    def _setup_sound(self):
        """Stone sound: real recordings (sounds/stone1-5.wav, KaTrain, MIT; recorded by
        Jordan Seaward -- sounds/LICENSE.txt). A different one on every move.
        Silent without QtMultimedia.

        Headphones awake: bluetooth headphones put the stream to sleep during
        silence and swallow the first ~150 ms of a new sound -- which is the
        whole stone sound. An inaudible stream is kept open for 5 min after
        the last sound.
        ponytail: an open audio stream delays Windows' automatic sleep; it closes after 5 min."""
        self._sound_on = store.setting("sound", True)
        self._sfx_all, self._last_sfx = [], None
        self._keep_awake = None
        if QSoundEffect is None:
            return
        folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sounds")
        level_ = store.setting("volume", 80) / 100
        for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
            if name.startswith("stone") and name.endswith(".wav"):
                e = QSoundEffect(self)
                e.setSource(QUrl.fromLocalFile(os.path.join(folder, name)))
                e.setVolume(level_)
                self._sfx_all.append(e)
        try:
            self._keep_awake = QSoundEffect(self)
            self._keep_awake.setSource(QUrl.fromLocalFile(_silent_wav(store.sub("sound", "silent.wav"))))
            self._keep_awake.setLoopCount(QSoundEffect.Loop.Infinite.value)   # PySide6 wants an int
            self._keep_awake_timer = QTimer(self, singleShot=True, interval=5 * 60 * 1000)
            self._keep_awake_timer.timeout.connect(self._keep_awake.stop)
        except Exception:
            self._keep_awake = None

    @property
    def _sfx(self):
        """Is there sound (for the settings screen and tests)."""
        return self._sfx_all[0] if self._sfx_all else None

    def wake(self):
        """Open/extend the headphone stream (on entering the play screen and on every sound)."""
        if self._keep_awake and self._sound_on:
            if not self._keep_awake.isPlaying():
                self._keep_awake.play()
            self._keep_awake_timer.start()

    def tick(self):
        if not (self._sfx_all and self._sound_on):
            return
        self.wake()
        option = [e for e in self._sfx_all if e is not self._last_sfx] or self._sfx_all
        self._last_sfx = random.choice(option)
        self._last_sfx.play()

    # ---------- panel setup ----------
    def _sidebar(self):
        beside = QWidget(objectName="sidebar")
        beside.setFixedWidth(150)
        d = QVBoxLayout(beside)
        d.setContentsMargins(8, 14, 8, 14)
        d.setSpacing(4)
        self.screen_buttons = {}
        group_ = QButtonGroup(self)
        for name, icon, label in self.SCREENS + (("settings", "⚙", ("Ayarlar", "Settings")),):
            if name == "settings":
                d.addStretch(1)
            b = QPushButton(f"{icon}  {i18n.t(*label)}", checkable=True)
            b.setFocusPolicy(Qt.NoFocus)
            b.clicked.connect(lambda _=False, a=name: self.screen_select(a))
            group_.addButton(b)
            self.screen_buttons[name] = b
            d.addWidget(b)
        self.screen_buttons["play"].setChecked(True)
        return beside

    def _right_panel(self):
        container = QWidget()
        d = QVBoxLayout(container)
        d.setContentsMargins(12, 10, 12, 8)
        self.warning = QLabel(objectName="warning", wordWrap=True)
        self.warning.hide()
        d.addWidget(self.warning)
        self.head = QLabel("", objectName="title")
        d.addWidget(self.head)
        self.stack = QStackedWidget()
        self.page = {}
        for name, setup_ in (("play", self._play_panel), ("problems", self._problem_panel),
                        ("games", self._archive_panel),
                        ("analysis", self._analysis_panel), ("settings", self._settings_panel)):
            self.page[name] = self.stack.addWidget(setup_())
        d.addWidget(self.stack, 1)
        self.status = Status(i18n.t("motor yükleniyor…", "loading engine…"), objectName="state")
        d.addWidget(self.status)
        return container

    def _play_panel(self):
        self.play_box = QWidget()
        d = QVBoxLayout(self.play_box)
        d.setContentsMargins(0, 0, 0, 0)

        self.setup_box = QWidget()
        setup_row = QHBoxLayout(self.setup_box)
        setup_row.setContentsMargins(0, 0, 0, 0)
        self.kind_group = QButtonGroup(self)
        self.b_free = QPushButton(i18n.t("Serbest", "Casual"), checkable=True, checked=True)
        self.b_ranked = QPushButton(i18n.t("Dereceli", "Ranked"), checkable=True)
        self.b_two = QPushButton(i18n.t("Karşılıklı", "Two players"), checkable=True)
        self.b_two.setToolTip(i18n.t("Aynı bilgisayarda iki kişi: siyahı da beyazı da siz oynarsınız.",
                                    "Two people at one computer: you play both black and white."))
        for b, kind_ in ((self.b_free, "game"), (self.b_ranked, "ranked"),
                       (self.b_two, "duo")):
            self.kind_group.addButton(b)
            b.clicked.connect(lambda _=False, t=kind_: self.kind_select(t))
            setup_row.addWidget(b)
        setup_row.addSpacing(10)
        self.size_box = QComboBox()
        self.size_box.addItems([f"{n}x{n}" for n in SIZES])
        self.size_box.setCurrentText(f"{self.cfg.size}x{self.cfg.size}")
        self.size_box.currentTextChanged.connect(self._size_selected)
        setup_row.addWidget(self.size_box)
        self.opponent_line = QWidget()
        rs = QHBoxLayout(self.opponent_line)
        rs.setContentsMargins(6, 0, 0, 0)
        rs.addWidget(QLabel(i18n.t("Rakip", "Opponent")))
        self.bot_box = QComboBox()
        self.bot_box.addItems(scoring.LADDER)
        self.bot_box.setCurrentText(self.bot_rank)
        self.bot_box.currentTextChanged.connect(self._bot_selected)
        rs.addWidget(self.bot_box)
        setup_row.addWidget(self.opponent_line)
        setup_row.addStretch(1)
        d.addWidget(self.setup_box)

        upper = QHBoxLayout()
        self.turn = QLabel()
        self.turn.setFont(QFont("Segoe UI", 10, QFont.Bold))
        upper.addWidget(self.turn)
        upper.addStretch(1)
        self.clock = QLabel()
        self.clock.setFont(QFont("Segoe UI", 10, QFont.Bold))
        upper.addWidget(self.clock)
        d.addLayout(upper)
        self.live_strip = MoveStrip(self.live_select)
        self.live_strip.area.setFixedHeight(MoveStrip.A + 22)   # one row + scrollbar
        d.addWidget(self.live_strip.area)

        # Text + card in one box: plain text (intro, end-of-game tables) or an HTML
        # card (category, reason, details link).
        self.out = _txt()
        self.out.anchorClicked.connect(self._card_link)
        d.addWidget(self.out, 1)
        self.ip = QLabel(i18n.t("▲ senin seviyen   △ üst seviye   ✕ en iyi",
                               "▲ your level   △ next level   ✕ best"), objectName="faint")
        d.addWidget(self.ip)

        self.play_choice = SelectionBar()
        d.addWidget(self.play_choice)
        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.b_pass = QPushButton(i18n.t("Pas", "Pass"))
        self.b_resign = QPushButton(i18n.t("Pes", "Resign"))
        self.b_hint = QPushButton(i18n.t("İpucu", "Hint"))
        self.b_retry = QPushButton(i18n.t("Geri al", "Undo"))
        self.b_go = QPushButton(i18n.t("Devam", "Continue"))
        self.b_next = QPushButton(i18n.t("Yeni maç", "New game"))
        for b, fn in ((self.b_pass, self.do_pass), (self.b_resign, self.resign_game),
                      (self.b_hint, self.hint), (self.b_retry, self.retry),
                      (self.b_go, self.advance), (self.b_next, self.next_task)):
            b.clicked.connect(fn)
            bar.addWidget(b)
        d.addLayout(bar)
        return self.play_box

    def _archive_panel(self):
        self.archive_box = QWidget()
        d = QVBoxLayout(self.archive_box)
        d.setContentsMargins(0, 0, 0, 0)
        self.game_list = QListWidget(objectName="gamelist")
        self.game_list.setItemDelegate(GameRow(self.game_list))
        self.game_list.setMouseTracking(True)
        self.game_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.game_list.currentRowChanged.connect(lambda _: self.game_select())
        d.addWidget(self.game_list, 1)
        d.addWidget(QLabel(i18n.t("Hamleler · tahtaya tıklayarak varyasyon oynayabilirsin",
                                 "Moves · click the board to play a variation"),
                           objectName="faint"))
        self.archive_strip = MoveStrip(self.strip_select)
        self.archive_strip.area.setMinimumHeight(MoveStrip.A + 22)
        self.archive_strip.area.setMaximumHeight(MoveStrip.A * 3 + 22)
        d.addWidget(self.archive_strip.area)

        nav = QHBoxLayout()
        for label, step in (("«", -10**6), ("‹", -1), ("›", 1), ("»", 10**6)):
            b = QPushButton(label)
            b.setFixedWidth(44)
            b.setStyleSheet("font-size:13pt; padding:3px 0;")
            b.clicked.connect(lambda _=False, a=step: self.step_moves(a))
            nav.addWidget(b)
        nav.addStretch(1)
        b = QPushButton(i18n.t("Sonraki blunder", "Next blunder"))
        b.clicked.connect(self.blunder_git)
        nav.addWidget(b)
        d.addLayout(nav)

        nav2 = QHBoxLayout()
        for label, fn in ((i18n.t("Analiz videosu", "Analysis video"), self.make_video),
                           (i18n.t("Grafikler", "Charts"), self.show_charts),
                           (i18n.t("Sına", "Quiz"), self.quiz)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            nav2.addWidget(b)
        self.b_quiz = b
        nav2.addStretch(1)
        b = QPushButton(i18n.t("SGF içe aktar", "Import SGF"))
        b.setToolTip(i18n.t("OGS, KGS, Fox… maçını arşive al ve hamlelerini puanla. "
                           "Seçili içe aktarılan maç yarım kaldıysa onu sürdürür.",
                           "Bring an OGS/KGS/Fox game into the archive and grade the "
                           "moves. Resumes the selected import if it was interrupted."))
        b.clicked.connect(self.import_sgf)
        nav2.addWidget(b)
        d.addLayout(nav2)

        # Generation takes minutes and runs in the background: its own row, so it does
        # not overwrite archive_out even if the game changes.
        self.archive_choice = SelectionBar()
        d.addWidget(self.archive_choice)
        self.video_state, self.sgf_status = QLabel(objectName="job"), QLabel(objectName="job")
        for label in (self.video_state, self.sgf_status):
            label.setWordWrap(True)
            label.hide()
            d.addWidget(label)

        self.archive_out = _txt()
        d.addWidget(self.archive_out, 2)
        return self.archive_box

    def _analysis_panel(self):
        container = QWidget()
        d = QVBoxLayout(container)
        d.setContentsMargins(0, 0, 0, 0)
        self.analysis_tabs = QTabBar()
        self.analysis_tabs.setFocusPolicy(Qt.NoFocus)          # no dashed focus frame
        for label in (("Hata tekrarı", "Mistake review"), ("Alışkanlıklar", "Habits"),
                       ("Gelişim", "Progress")):
            self.analysis_tabs.addTab(i18n.t(*label))
        self.analysis_tabs.currentChanged.connect(lambda _: self.analysis_select())
        d.addWidget(self.analysis_tabs)
        self.analysis_stack = QStackedWidget()

        tk = QWidget()
        td = QVBoxLayout(tk)
        td.setContentsMargins(0, 6, 0, 0)
        self.review_out = _txt()
        td.addWidget(self.review_out, 1)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        self.b_review = _primary(QPushButton(i18n.t("Sonraki kart", "Next card")))
        self.b_review.clicked.connect(self.load_review)
        bottom.addWidget(self.b_review)
        td.addLayout(bottom)
        self.analysis_stack.addWidget(tk)

        self.pattern_out = _txt()
        self.analysis_stack.addWidget(self.pattern_out)

        self.progress = QWidget()
        QVBoxLayout(self.progress).setContentsMargins(0, 6, 0, 0)
        self.analysis_stack.addWidget(self.progress)
        d.addWidget(self.analysis_stack, 1)
        return container

    def _settings_panel(self):
        container = QWidget()
        d = QVBoxLayout(container)
        d.setContentsMargins(0, 0, 0, 0)

        def section(text_):
            d.addWidget(QLabel(text_, objectName="section"))

        section(i18n.t("Görünüm", "Appearance"))
        grid = QGridLayout()
        self.theme_buttons = {}
        selected = store.setting("theme") or theme.DEFAULT
        group_ = QButtonGroup(self)
        for i, name in enumerate(theme.THEMES):
            b = QPushButton(theme.its_name(name), checkable=True, checked=name == selected)
            b.setProperty("theme", name)
            b.clicked.connect(lambda _=False, a=name: self.theme_select(a))
            group_.addButton(b)
            self.theme_buttons[name] = b
            grid.addWidget(b, i // 3, i % 3)
        d.addLayout(grid)

        section(i18n.t("Dil", "Language"))
        self.lang_box = QComboBox()
        for code, name in i18n.LANGUAGES.items():
            self.lang_box.addItem(name, code)
        self.lang_box.setCurrentIndex(list(i18n.LANGUAGES).index(i18n.selected()))
        self.lang_box.currentIndexChanged.connect(self._lang_selected)
        d.addWidget(self.lang_box)

        section(i18n.t("Ses", "Sound"))
        row_ = QHBoxLayout()
        # A button that says on/off instead of a checkbox: the checkbox state was hard to read.
        self.sound_box = QPushButton(checkable=True, checked=self._sound_on)
        self.sound_box.toggled.connect(self._sound_setting)
        row_.addWidget(self.sound_box)
        self.volume_bar = QSlider(Qt.Horizontal)
        self.volume_bar.setRange(0, 100)
        self.volume_bar.setValue(store.setting("volume", 80))
        self.volume_bar.sliderReleased.connect(self._sound_setting)
        row_.addWidget(self.volume_bar, 1)
        b = QPushButton(i18n.t("Dene", "Test"))
        b.clicked.connect(self.tick)
        row_.addWidget(b)
        self._sound_label()
        if self._sfx is None:
            for w in (self.sound_box, self.volume_bar, b):
                w.setEnabled(False)
            self.sound_box.setText(i18n.t("Ses yok (QtMultimedia eksik)",
                                        "No sound (QtMultimedia missing)"))
        d.addLayout(row_)

        section(i18n.t("Süre", "Time"))
        row_ = QHBoxLayout()
        self.time_box = QPushButton(checkable=True, checked=bool(store.setting("clock_on")))
        self.time_box.toggled.connect(self._clock_setting)
        row_.addWidget(self.time_box)
        self.time_min_box = QComboBox()
        self.time_min_box.addItems([str(n) for n in TIME_MINUTES])
        self.time_min_box.setCurrentText(str(store.setting("clock_min", 20)))
        self.time_min_box.setEnabled(self.time_box.isChecked())
        self.time_min_box.currentTextChanged.connect(self._clock_setting)
        row_.addWidget(self.time_min_box)
        row_.addWidget(QLabel(i18n.t("dakika", "minutes")))
        row_.addStretch(1)
        d.addLayout(row_)
        self._clock_label()
        d.addWidget(QLabel(i18n.t(
            "Yeni maçta geçerli olur. Saat ilk taş konunca başlar ve yalnız sıra "
            "sendeyken işler: bot düşünürken, analiz beklenirken ve başka "
            "ekrandayken durur. "
            "Süre biterse maç kaybedilir.",
            "Applies to the next game. The clock starts after the first stone and only "
            "runs on your turn: it stops while "
            "the bot thinks, while analysis runs and on other screens. Running out "
            "loses the game."), objectName="faint", wordWrap=True))

        section(i18n.t("Kurallar", "Rules"))
        row_ = QHBoxLayout()
        b = QPushButton(i18n.t("Kural turu", "Rules tour"))
        b.clicked.connect(lambda: self.rules_tour())
        row_.addWidget(b)
        b = QPushButton(i18n.t("Skor kuralları", "Scoring rules"))
        b.clicked.connect(lambda: self._show_page(i18n.t("Skor kuralları", "Scoring rules"),
                                                scoring.rules(self.cfg.komi),
                                                markdown=True))
        row_.addWidget(b)
        b = QPushButton(i18n.t("Lisanslar", "Licenses"))
        b.clicked.connect(self.licenses)
        row_.addWidget(b)
        row_.addStretch(1)
        d.addLayout(row_)

        section(i18n.t("Analiz videosu", "Analysis video"))
        # The template writes the video text; if agy is installed it can be polished by an LLM.
        self.llm_box = QCheckBox(i18n.t("Video metnini LLM ile cilala (agy)",
                                        "Polish the video script with an LLM (agy)"))
        self.llm_box.setChecked(bool(store.setting("video_llm")))
        if not video.has_llm():
            self.llm_box.setEnabled(False)
            self.llm_box.setToolTip(i18n.t(
                "agy (Antigravity CLI) bulunamadı. Video yine "
                "üretilir; metni uygulama kendisi yazar.",
                "agy (Antigravity CLI) not found. The video is still produced; "
                "the app writes the script itself."))
        self.llm_box.toggled.connect(self._llm_setting)
        d.addWidget(self.llm_box)

        section(i18n.t("Geliştirici", "Developer"))
        row_ = QHBoxLayout()
        b = QPushButton(i18n.t("Seçili maçın brief'i", "Brief of selected game"))
        b.clicked.connect(self.write_brief)
        row_.addWidget(b)
        row_.addStretch(1)
        d.addLayout(row_)
        self.settings_out = QLabel(objectName="faint", wordWrap=True)
        d.addWidget(self.settings_out)
        d.addStretch(1)
        return container

    def _sound_label(self):
        self.sound_box.setText(i18n.t("Taş sesi: Açık", "Stone sound: On") if self._sound_on
                              else i18n.t("Taş sesi: Kapalı", "Stone sound: Off"))

    def _clock_label(self):
        self.time_box.setText(i18n.t("Süre: Açık", "Time limit: On")
                               if self.time_box.isChecked() else
                               i18n.t("Süre: Kapalı", "Time limit: Off"))

    def _clock_setting(self, *_):
        """The setting IS SAVED but does not touch the running game: the clock does not change mid-game."""
        store.set_setting("clock_on", self.time_box.isChecked())
        store.set_setting("clock_min", int(self.time_min_box.currentText()))
        self.time_min_box.setEnabled(self.time_box.isChecked())
        self._clock_label()

    def _sound_setting(self, *_):
        self._sound_on = self.sound_box.isChecked()
        self._sound_label()
        store.set_setting("sound", self._sound_on)
        store.set_setting("volume", self.volume_bar.value())
        for e in self._sfx_all:
            e.setVolume(self.volume_bar.value() / 100)
        if not self._sound_on and self._keep_awake:
            self._keep_awake.stop()
        self.tick()

    def _bot_selected(self, name):
        """The opponent bot's level. In a ranked game the opponent comes from the
        ladder; the value chosen here only affects free games."""
        self.bot_rank = name
        self.status.setText(i18n.t(f"rakip bot: {name} — sonraki hamleden itibaren",
                                  f"opponent bot: {name} — from the next move"))

    # ---------- thread plumbing ----------
    def run(self, fn, cb):
        self.phase = "thinking"
        g = self.gen
        self.buttons()
        self.refresh()

        def work():
            try:
                res = fn()
            except Exception as e:
                res = e
            self.q.put((g, cb, res))
        threading.Thread(target=work, daemon=True).start()

    def pump(self):
        """The pump must NEVER die: if a callback raises, the UI stays on
        'thinking' forever. QTimer keeps firing; try/except is required.

        The `root.after(80, pump)` pattern from tkinter was kept as is -- it
        could be written with Signals/Slots in Qt, but changing something that
        works has a cost and no gain."""
        try:
            while True:
                try:
                    g, cb, res = self.q.get_nowait()
                except queue.Empty:
                    break
                if g is None:       # video generation: arrives even if the tab/game changes,
                    cb(res)         # handles its own error
                    continue
                if g != self.gen:
                    continue
                if isinstance(res, Exception):
                    self._error(res)
                else:
                    cb(res)
        except Exception as e:
            self._error(e, traceback.format_exc())
        self._engine_status()
        try:
            self._clock_tick()
        except Exception as e:
            self._error(e, traceback.format_exc())

    def _engine_status(self):
        """Say what the engine is doing during a long job.

        On first start KataGo tunes the GPU and query() blocks for minutes;
        the screen only said "analyzing". The user thinks the app froze.
        """
        self._tick = getattr(self, "_tick", 0) + 1
        if self._tick % 12 or self.phase != "thinking" or not self.eng:
            return                       # about once a second is enough, not every 80 ms
        d = self.eng.status_line()
        if d:
            self.status.setText(d)

    def _error(self, e, trace=None):
        try:
            with open(store.sub("log", self.game.name + "_error.log"), "a",
                      encoding="utf-8") as f:
                f.write((trace or f"{type(e).__name__}: {e}") + NL)
        except OSError:
            pass
        self.phase = "your_turn"
        if self.eng is None and self.warning.text():
            self.warning.show()
        self.say(i18n.t(f"Hata: {type(e).__name__}: {e}{NL}{NL}Oyun sürüyor, devam edebilirsin.",
                       f"Error: {type(e).__name__}: {e}{NL}{NL}The game continues, carry on."))
        self.status.setText(i18n.t("hata — ayrıntı maclar/log/*_error.log",
                                  "error — details in maclar/log/*_error.log"))
        self.buttons()
        self.refresh()

    def _booted(self, eng):
        self.eng = eng
        if self.resume:
            self.resume_game(self.resume)
        elif self.is_playing:
            self.next_task()
        else:                    # the user is on another screen: set up the game when they come back
            self.phase = "done"
            self.status.setText(i18n.t("motor hazır", "engine ready"))
            self.buttons()

    def resume_game(self, name):
        """Resumes an unfinished game where it stopped (--resume). A normal new game
        is set up first (title, opponent, intro text), then the game and board
        are replaced with the record on disk. Does not open a finished game:
        its result would have to be removed by hand.
        ponytail: CLI only; if a 'resume' button is wanted in the UI, it hooks in here."""
        self.resume = None
        m = store.load_game(name)
        if m.result:
            self.say(i18n.t(f"{name} bitmiş bir maç, sürdürülemez.",
                           f"{name} is a finished game, it can't be resumed."))
            return
        self.cfg.size = m.size
        self.mode_select(m.mode)                  # eng ready: next_task sets it up
        self.game, self.board = m, m.board_at()
        self.phase = "your_turn"
        self.say(i18n.t(f"Maç kaldığı yerden sürüyor ({len(m.moves)} hamle). Sıra sende.",
                       f"Game resumed ({len(m.moves)} moves). Your turn."))
        self.buttons()
        self.refresh()

    def closeEvent(self, ev):
        """Close KataGo too when the window closes: it is a separate process and is
        not guaranteed to die when Python exits -- orphaned, it holds GPU
        memory and the next start launches a second engine."""
        try:
            if self.eng:
                self.eng.close()
        except Exception:
            pass
        ev.accept()

    # ---------- panel ----------
    def say(self, t):
        self.out.setHtml(_html(t))

    def add(self, t):
        self.out.moveCursor(QTextCursor.End)
        self.out.insertPlainText(t)

    @staticmethod
    def _write(box, t):
        box.setHtml(_html(t))

    def say_archive(self, t, extra=""):
        self.archive_out.setHtml(_html(t, extra))

    @property
    def mode(self):
        """The SCREEN's mode: lesson | game | ranked | archive | analysis | settings."""
        if self.screen_ == "play":
            return ("ranked" if self.b_ranked.isChecked() else
                    "duo" if self.b_two.isChecked() else "game")
        return {"games": "archive"}.get(self.screen_, self.screen_)

    @property
    def ranked(self):
        """The GAME's mode (not the screen's): in a ranked game undo would be
        cheating; hints and the live score would leak the engine's evaluation."""
        return self.game.mode == "ranked"

    @property
    def is_playing(self):
        return self.mode in ("lesson", "game", "ranked", "duo")

    def mode_select(self, mode):
        """For tests and the welcome page: mode -> screen (+ the kind on Play)."""
        if mode in ("game", "ranked", "duo"):
            self.b_ranked.setChecked(mode == "ranked")
            self.b_free.setChecked(mode == "game")
            self.b_two.setChecked(mode == "duo")
        self.screen_select({"game": "play", "ranked": "play", "duo": "play",
                        "archive": "games"}.get(mode, mode))

    def panel(self):
        m = self.mode
        self.stack.setCurrentIndex(self.page[{"lesson": "play", "game": "play",
                                               "ranked": "play", "duo": "play"}.get(m, self.screen_)])
        self.setup_box.setVisible(m in ("game", "ranked", "duo"))
        self.opponent_line.setVisible(m == "game")
        self.ip.setVisible(m == "lesson")
        # Board: Habits and Progress want wide text/charts.
        self.left.setVisible(not (m == "analysis" and self.analysis_tabs.currentIndex() > 0))

    def refresh(self):
        m = self.mode
        self.b_live.setVisible(self.is_playing and self.watch is not None)
        if m == "archive":
            if self.archive:
                self.canvas.draw(self.archive.board_at(self.archive_i, self.archive_branch), True)
                self.archive_strip.rebuild(self.archive, True, (self.archive_branch, self.archive_i))
            else:
                self.canvas.draw(Board(self.cfg.size), False)
            return
        if m == "problems":
            t = self.prob
            self.canvas.draw(self.prob_board if self.prob_board is not None else
                            (t[1].board_at(t[2] - 1) if t else Board(self.cfg.size)),
                            bool(t) and t[3] is None)
            return
        if m == "analysis":
            t = self.review
            self.canvas.draw(self.review_board if self.review_board is not None else
                            (t[1].board_at(t[2] - 1) if t else Board(self.cfg.size)),
                            bool(t) and t[3] is None)
            return
        if m == "settings":
            self.canvas.draw(goban.replay(EXAMPLE, 9), False)
            return
        watch = self.watch is not None
        self.canvas.draw(self.game.board_at(self.watch) if watch else self.board,
                        self.phase == "your_turn" and not watch, plain=watch,
                        white=self.game.mode == "duo" and goban.turn(self.game.moves) == "W")
        # Live game: category and score are NOT SHOWN in the strip.
        self.live_strip.rebuild(self.game, False,
                             (None, self.watch if watch else len(self.game.moves)))
        if watch:
            self.b_live.setText(i18n.t(
                f"{self.watch}. hamleye bakıyorsun  ·  Canlıya dön (Esc)",
                f"Viewing move {self.watch}  ·  Back to live (Esc)"))
        self.turn.setText(self._turn_text())

    def _turn_text(self):
        if self.phase in ("done", "boot"):
            return i18n.t("Oyun bitti", "Game over") if self.phase == "done" else ""
        s = goban.turn(self.game.moves)
        if self.game.mode == "duo":
            who = i18n.t("oyuncu 1", "player 1") if s == "B" else i18n.t("oyuncu 2", "player 2")
        else:
            who = (i18n.t("sen", "you") if s == "B" else
                   i18n.t("rakip", "opponent") if self.game.mode == "lesson" else "bot")
        stone_ = i18n.t("● Siyah", "● Black") if s == "B" else i18n.t("○ Beyaz", "○ White")
        n = sum(c in ("B", "W") for c, _ in self.game.moves)
        return i18n.t(f"Sıra: {stone_} — {who}   ·   {n} hamle",
                     f"To play: {stone_} — {who}   ·   {n} moves")

    # ---------- move strip / navigation ----------
    def live_select(self, branch, i):
        n = len(self.game.moves)
        self.watch = None if i >= n else i
        self.buttons()
        self.refresh()

    def back_to_live(self):
        if self.is_playing and self.watch is not None:
            self.live_select(None, len(self.game.moves))

    def navigate(self, d):
        """Keyboard: <- -> Home End. Along the line in the archive, watching in a live game."""
        if self.mode == "archive":
            self.step_moves(d)
        elif self.is_playing:
            n = len(self.game.moves)
            now = n if self.watch is None else self.watch
            self.live_select(None, max(0, min(n, now + d)))

    def buttons(self):
        p = self.phase
        game_ = self.is_playing
        # Hint/undo/continue only in single-player teaching modes: in ranked it would
        # be cheating, and in a two-player game it is unclear on whose behalf a hint is given.
        tutor = game_ and self.game.mode in ("lesson", "game")
        self.b_pass.setEnabled(p == "your_turn" and game_ and self.watch is None)
        self.b_resign.setEnabled(p in ("your_turn", "graded")
                              and self.game.mode in ("game", "ranked", "duo"))
        self.b_hint.setEnabled(bool(p == "graded" and self.facts and tutor))
        self.b_retry.setEnabled(p == "graded" and tutor)
        self.b_go.setEnabled(p == "graded" and tutor)
        # In a ranked game 'new' is enabled ONLY once the game is over: abandoning it
        # halfway and starting over would erase the loss -- the same hole as undo.
        self.b_next.setEnabled(bool(self.eng) and game_ and (
            p == "done" if self.ranked else p in ("graded", "done", "your_turn")))

    def _ranked_locked(self):
        """Leaving a started ranked game (changing size or kind) would be a back
        door to 'new' -- the same hole as undo."""
        if self.ranked and self.game.moves and self.phase != "done":
            self.status.setText(i18n.t("dereceli maç bitmeden değişmez — bitir ya da pes et",
                                      "not until the ranked game ends — finish or resign"))
            return True
        return False

    def _size_selected(self, t):
        if self._muted:
            return
        n = int(t.split("x")[0])
        if n == self.cfg.size:
            return
        if self._ranked_locked():
            self._muted = True
            self.size_box.setCurrentText(f"{self.cfg.size}x{self.cfg.size}")
            self._muted = False
            return
        self.cfg.size = n            # Engine reads the size from cfg on every query
        if self.eng:
            self.next_task()

    # ---------- screens ----------
    def kind_select(self, kind_):
        """Play: Free / Ranked / Two players."""
        if (kind_ == "ranked") != self.ranked and self._ranked_locked():
            self.b_ranked.setChecked(True)
            return
        self.screen_select("play")

    def screen_select(self, screen_):
        self.close_page()
        previous = self.screen_
        self.screen_ = screen_
        for name, b in self.screen_buttons.items():
            b.setChecked(name == screen_)
        self.watch = None
        self.panel()
        m = self.mode
        if self.is_playing:
            self.wake()                  # keep the first move's sound from being swallowed by the headphones
            # If a live game of the same kind is running, carry on where it stopped: going
            # to settings and back must not kill the game.
            lasts = self.game.mode == m and self.game.moves and self.phase not in ("done", "boot")
            if lasts:
                self.head.setText(self._game_title)
            elif self.eng:
                self.next_task()
                return
        elif m == "archive":
            self.head.setText(i18n.t("Maçlar", "Games"))
            if previous != screen_:
                self.archive_fill()
        elif m == "problems":
            self.head.setText(i18n.t("Problemler", "Problems"))
            if previous != screen_:
                self.prob_drop()
            self.prob_fill()
        elif m == "analysis":
            self.head.setText(i18n.t("Analiz", "Analysis"))
            self.analysis_select()
        elif m == "settings":
            self.head.setText(i18n.t("Ayarlar", "Settings"))
            self.canvas.overlays, self.canvas.zone = [("E3", C_BEST, "x", "")], None
        self.buttons()
        self.refresh()

    def next_task(self):
        self.wake()                      # new game: have the headphone stream ready
        self.gen += 1
        self.watch = None
        self.step, self.attempts, self.scores = 0, 0, []
        self.facts = None
        self.canvas.overlays, self.canvas.zone = [], None
        self.game = store.Game(name=store.new_name(self.mode), size=self.cfg.size,
                             komi=self.cfg.komi, mode=self.mode)
        self._setup_clock()
        self._details = False
        if self.mode == "lesson":
            self._heading(i18n.t("Ders", "Lesson"))
            self.say(i18n.t("pozisyon hazırlanıyor…", "preparing a position…"))
            self.status.setText(i18n.t("self-play + analiz…", "self-play + analysis…"))
            self.board = Board(self.cfg.size)
            self.run(lambda: scoring.teaching_position(self.eng, f"rank_{self.my_rank}"),
                     self._task_ready)
        elif self.mode == "duo":
            self._heading(i18n.t("Karşılıklı maç", "Two-player game"))
            self.task = None
            self.board = Board(self.cfg.size)
            self.phase = "your_turn"
            self._intro = i18n.t(
                f"Aynı bilgisayarda iki kişi. Siyah başlar; sırayla tahtaya "
                f"tıklayın.{NL}{NL}Hamle değerlendirmesi YOK — hangi oyuncu adına "
                f"puanlanacağı belli değil. Kurallar, esir alma ve maç sonu sayımı "
                f"aynı.{NL}{NL}İki pas maçı bitirir. 'Pes' sırası gelen tarafı "
                f"kaybettirir.{NL}{NL}Maç: {store.display_name(self.game.name)}",
                f"Two people at one computer. Black starts; take turns clicking the "
                f"board.{NL}{NL}NO move review — there is no telling which player it "
                f"would be for. Rules, captures and the end-of-game count are the "
                f"same.{NL}{NL}Two passes end the game. 'Resign' loses for the side "
                f"to play.{NL}{NL}Game: {store.display_name(self.game.name)}")
            self.say(self._intro)
            self.status.setText(i18n.t("sıra siyahta", "black to play"))
            self.buttons()
            self.refresh()
        elif self.ranked:
            d = store.rank_state(self.cfg.size)
            self.opponent_value = store.next_opponent(self.cfg.size)
            opponent_name = scoring.value_rank(self.opponent_value)   # do NOT override bot_rank
            self.task = None
            self.board = Board(self.cfg.size)
            self.phase = "your_turn"
            remaining = scoring.PLACEMENT_GAMES - len(d["placement"])
            if remaining > 0:
                self._heading(i18n.t(f"Dereceli · yerleştirme ({remaining} maç kaldı)",
                                   f"Ranked · placement ({remaining} to go)"))
                intro = i18n.t(f"YERLEŞTİRME MAÇI {scoring.PLACEMENT_GAMES - remaining + 1}"
                              f"/{scoring.PLACEMENT_GAMES}{NL}{NL}Rakip: {opponent_name}{NL}{NL}"
                              f"3 maç sonunda yaklaşık bir rank verilecek.",
                              f"PLACEMENT GAME {scoring.PLACEMENT_GAMES - remaining + 1}"
                              f"/{scoring.PLACEMENT_GAMES}{NL}{NL}Opponent: {opponent_name}{NL}{NL}"
                              f"After 3 games you get an approximate rank.")
            else:
                self._heading(f"{i18n.t('Dereceli', 'Ranked')} · {scoring.value_rank(d['value'])}"
                             f" · {d['sp']:.0f} SP")
                run_ = i18n.t(f"  ·  {d['streak']} maçlık seri",
                             f"  ·  {d['streak']}-game streak") if d["streak"] >= 2 else ""
                intro = i18n.t(f"Rankın: {scoring.value_rank(d['value'])}  "
                              f"({d['sp']:.0f}/{scoring.SP_RANK} SP){run_}{NL}{NL}"
                              f"Rakip: {opponent_name}",
                              f"Your rank: {scoring.value_rank(d['value'])}  "
                              f"({d['sp']:.0f}/{scoring.SP_RANK} SP){run_}{NL}{NL}"
                              f"Opponent: {opponent_name}")
                if self.opponent_value > d["value"]:
                    intro += i18n.t(f"  — senden {self.opponent_value - d['value']} basamak "
                                   f"güçlü (seri sayesinde). Yenersen daha çok SP.",
                                   f"  — {self.opponent_value - d['value']} steps stronger than "
                                   f"you (thanks to the streak). Beat them for more SP.")
            self._intro = (intro + i18n.t(
                f"{NL}{NL}Dereceli maçta geri alma, ipucu ve canlı puan "
                f"YOK.{NL}Puanlar kaydediliyor, maç sonunda gösterilecek."
                f"{NL}{NL}Maç: {store.display_name(self.game.name)}",
                f"{NL}{NL}NO undo, hints or live score in a ranked game."
                f"{NL}Scores are recorded and shown when the game ends."
                f"{NL}{NL}Game: {store.display_name(self.game.name)}")
                + scoring.calibration_note(self.cfg.size))
            self.say(self._intro)
            self.status.setText(i18n.t("sıra sende", "your turn"))
            self.buttons()
            self.refresh()
        else:
            self._heading(i18n.t("Serbest maç", "Casual game"))
            self.task = None
            self.board = Board(self.cfg.size)
            self.phase = "your_turn"
            self._intro = i18n.t(
                f"Sen siyahsın. Bir noktaya tıkla.{NL}{NL}"
                f"Normal hamlelerde sessiz kalırım. Blunder yaparsan maç durur; "
                f"nedenini ve yerine ne oynanırdı konuşuruz.{NL}{NL}"
                f"Oynayacak bir şeyin kalmadıysa 'pas'. İki pas oyunu bitirir."
                f"{NL}{NL}Rakip bot: {self.bot_rank}{NL}Maç: {store.display_name(self.game.name)}",
                f"You are black. Click an intersection.{NL}{NL}"
                f"I stay quiet on normal moves. If you blunder the game stops and "
                f"we look at why and what else to play.{NL}{NL}"
                f"Nothing left to play? 'pass'. Two passes end the game."
                f"{NL}{NL}Opponent bot: {self.bot_rank}{NL}Game: {store.display_name(self.game.name)}"
            ) + scoring.calibration_note(self.cfg.size)
            self.say(self._intro)
            self.status.setText(i18n.t("sıra sende", "your turn"))
            self.buttons()
            self.refresh()

    def _heading(self, text_):
        self._game_title = text_
        self.head.setText(text_)

    def _task_ready(self, res):
        moves, root, mi = res
        self.task = (moves, root, mi)
        self.game.moves = list(moves)
        self.board = self.game.board_at()
        self.phase = "your_turn"
        gap = mi[0]["scoreLead"] - mi[1]["scoreLead"] if len(mi) > 1 else 0
        self.say(i18n.t(
            f"{len(moves)}. hamleden sonra. Siyah oynayacak.{NL}{NL}"
            f"{self.plan_len} hamlelik bir plan kur. Her hamleden sonra rakip "
            f"en iyi cevabı verecek — planın doğruysa ayakta kalır.{NL}{NL}"
            f"En iyi ile ikinci arasında {gap:.1f} puan fark var.",
            f"After move {len(moves)}. Black to play.{NL}{NL}"
            f"Build a {self.plan_len}-move plan. After each move the opponent "
            f"answers best — if your plan is right it survives.{NL}{NL}"
            f"The gap between best and second best is {gap:.1f} points.")
            + scoring.calibration_note(self.cfg.size))
        self.status.setText(i18n.t(f"hamle 1 / {self.plan_len}",
                                  f"move 1 / {self.plan_len}"))
        self.buttons()
        self.refresh()

    # ---------- playing ----------
    # ---------- time control ----------
    def _setup_clock(self):
        """Puts a clock on a new game (when the setting is on, in real game modes).

        Both clocks live in the same dict: in a two-player game the side to
        move has its clock running; in single-player, during the bot's turn
        phase is NOT 'your_turn', so Black's clock is already stopped -- the
        same code carries both cases.

        ponytail: one total time, no byo-yomi/Fischer. It teaches a 15k nothing
        and needs constant care. If someone wants it, add a field to the dict;
        the clock loop stays the same.
        """
        self._clock_mark = self._clock_last = None
        if not (store.setting("clock_on") and self.mode in ("game", "ranked", "duo")):
            return
        seconds = float(store.setting("clock_min", 20)) * 60
        self.game.meta["clock"] = {"total": seconds, "remaining": {"B": seconds, "W": seconds}}

    def _clock_tick(self):
        """Called from the 80 ms pump. Monotonic, not the wall clock: the time does
        not drift even if the user moves the system clock forward."""
        s = self.game.meta.get("clock") if (self.is_playing and self.game) else None
        if not s:
            # The anchor is reset even WITHOUT a clock: otherwise 5 minutes spent in
            # settings would be deducted in one go on returning to the play screen.
            self._clock_mark = None
            if self._clock_last is not None:
                self.clock.setText("")
                self._clock_last = None
            return
        # The clock does not run until the first stone is placed (handicap stones are not moves).
        if self.phase == "your_turn" and self.screen_ == "play" and self.game.moves:
            now = time.monotonic()
            color = goban.turn(self.game.moves) if self.game.mode == "duo" else "B"
            if self._clock_mark is not None:
                s["remaining"][color] = max(0.0, s["remaining"][color] - (now - self._clock_mark))
            self._clock_mark = now
            if s["remaining"][color] <= 0:
                self._clock_show(s)
                self.resign_game(True, "clock")
                return
        else:
            self._clock_mark = None
        self._clock_show(s)

    def _clock_show(self, s):
        """Do not touch the QLabel unless the second changed: a repaint every 80 ms is pointless."""
        k = s["remaining"]
        if self.game.mode == "duo":
            text_ = (i18n.t("● Siyah ", "● Black ") + _min_sec(int(k["B"] * 1000))
                     + "    " + i18n.t("○ Beyaz ", "○ White ")
                     + _min_sec(int(k["W"] * 1000)))
            few = min(k["B"], k["W"]) <= 10
        else:
            text_ = i18n.t("Süren: ", "Your time: ") + _min_sec(int(k["B"] * 1000))
            few = k["B"] <= 10
        if text_ == self._clock_last:
            return
        self.clock.setText(text_)
        # Last 10 seconds in red: affects only this label, not the theme stylesheet.
        # A clock nobody notices is half a feature.
        self.clock.setStyleSheet("color:#c0392b" if few else "")
        self._clock_last = text_

    def resign_game(self, confirm=False, how="resign"):
        """Resigning. In Go it is the normal way to end a lost game. In a ranked game
        it counts as a LOSS; not an escape hatch, a shortcut. Confirmation is
        asked inside the panel, not in a message box.

        Running out of time goes through HERE too (how='clock'): the losing
        path -- record, SP, ranked event, screen -- is the same, only the label
        differs. A separate ending path would mean fixing the same bug twice."""
        if self.phase not in ("your_turn", "graded") or not self.is_playing:
            return
        n = len(self.game.valid_records)
        if self.game.mode == "duo":
            self._two_resign(confirm, how)
            return
        if not confirm:
            question = i18n.t("Pes etmek istiyor musun?", "Do you want to resign?")
            if self.ranked:
                question += i18n.t(
                    f" Dereceli maç: mağlubiyet olarak işlenecek ve SP kaybedeceksin "
                    f"(rakip {scoring.value_rank(self.opponent_value)}).",
                    f" Ranked game: this counts as a loss and you lose SP "
                    f"(opponent {scoring.value_rank(self.opponent_value)}).")
            self.play_choice.ask(question, [(i18n.t("Pes et", "Resign"), lambda: self.resign_game(True)),
                                       (i18n.t("Vazgeç", "Cancel"), None)])
            return
        self.game.meta["result"] = how
        store.save(self.game)
        self.phase = "done"
        state_ = (i18n.t("pes edildi", "resigned") if how == "resign" else
                 i18n.t("süre doldu", "out of time"))
        if self.ranked:
            # Resign = S 0, regardless of the score. The quality signal still counts.
            records = self.game.valid_records
            sv = scoring.game_level(records)
            event = _apply_ranked(self.game, 0.0, None, self.opponent_value, sv, how)
            self._write_ending(NL.join([scoring.result_sentence(0.0, how, None)]
                                    + self._result_summary(self.game.name, records, sv,
                                                        event, self.opponent_value)), how)
            self.status.setText(state_)
            self.buttons()
            self.refresh()
            return
        store.add_record(self.game, {"game_end": {"result": how}})
        start = (i18n.t(f"Pes ettin. ({n} hamle oynandı)", f"You resigned. ({n} moves played)")
               if how == "resign" else
               i18n.t(f"Süren doldu — maçı kaybettin. ({n} hamle oynandı)",
                     f"You ran out of time — you lost. ({n} moves played)"))
        self._write_ending(i18n.t(
            f"{start}{NL}{NL}Maç: {store.display_name(self.game.name)}{NL}"
            f"Maçlar ekranından inceleyebilirsin.",
            f"{start}{NL}{NL}Game: {store.display_name(self.game.name)}{NL}"
            f"You can review it from the Games screen."), how)
        self.status.setText(state_)
        self.buttons()
        self.refresh()

    def _write_ending(self, text_, how):
        """Resignation/timeout text. On timeout the winner does not change (whoever
        ran out loses), but the board's current count is added below it: "who
        would be ahead had time not run out". The count is an engine query, in
        the background."""
        self.say(text_)
        if how != "clock":
            return
        game, eng = self.game, self.eng
        sides = scoring.side_names(game.mode)

        def arrived(s):
            S, source, diff = scoring.game_result(s)
            sy, bz = sides or (i18n.t("Siyah (sen)", "Black (you)"),
                                  i18n.t("Beyaz (bot)", "White (bot)"))
            what = (i18n.t("eşit skor", "level score") if S == 0.5 else
                  i18n.t(f"{sy if diff > 0 else bz} {abs(diff):.1f} puan önde",
                        f"{sy if diff > 0 else bz} ahead by {abs(diff):.1f} points"))
            self.phase = "done"
            self.say(NL.join([text_, "", i18n.t(f"Süre bitmeseydi puan durumu: {what}.",
                                               f"Score if time hadn't run out: {what}.")]
                             + scoring.count_lines(s, None, sides)))
            self.buttons()
            self.refresh()

        self.say(text_ + i18n.t(f"{NL}{NL}Süre bitmeseydi puan durumu: sayılıyor…",
                               f"{NL}{NL}Score if time hadn't run out: counting…"))
        self.run(lambda: scoring.final_count(eng, list(game.moves), game.size, game.komi),
                 arrived)

    def do_pass(self):
        if self.phase != "your_turn" or not self.eng:
            return
        color = goban.turn(self.game.moves) if self.game.mode == "duo" else "B"
        self.board.play(None, goban.color(color))
        self.game.moves.append([color, "pass"])
        store.save(self.game)
        if goban.game_over(self.game.moves):
            self._finish()
            return
        if self.game.mode == "duo":
            self._two_turn()
            return
        self._submit()

    def click(self, idx):
        if self.mode == "problems":
            self.prob_answer(idx)
            return
        if self.mode == "analysis":
            self.review_answer(idx)
            return
        if self.mode == "settings":
            return
        if self.mode == "archive":
            if self.questions:                  # a quiz is running: click = answer
                self._quiz_answer(idx)
            elif self.archive:
                self.branch_play(idx)
            return
        if self.watch is not None:
            self.status.setText(i18n.t("geçmiş hamleye bakıyorsun — önce canlıya dön (Esc)",
                                      "viewing a past move — back to live first (Esc)"))
            return
        if self.phase != "your_turn" or not self.eng:
            return
        color = goban.turn(self.game.moves) if self.game.mode == "duo" else "B"
        if not self.board.play(idx, goban.color(color)):
            self.status.setText(i18n.t("yasa dışı hamle", "illegal move"))
            return
        self.game.moves.append([color, to_gtp(idx, self.cfg.size)])
        self.tick()
        if self.game.mode == "duo":
            store.save(self.game)
            self._two_turn()
            return
        self._submit()

    # ---------- two-player game (one board, two people) ----------
    def _two_turn(self):
        """A move was played: the engine is not asked, the turn passes to the other side."""
        if goban.game_over(self.game.moves):
            self._finish()
            return
        black = goban.turn(self.game.moves) == "B"
        self.status.setText(i18n.t("sıra siyahta", "black to play") if black else
                            i18n.t("sıra beyazda", "white to play"))
        self.buttons()
        self.refresh()

    def _two_resign(self, confirm, how="resign"):
        """Whoever resigns -- or runs out of time -- is the side to move: they are the loser."""
        black = goban.turn(self.game.moves) == "B"
        side = i18n.t("Siyah", "Black") if black else i18n.t("Beyaz", "White")
        if not confirm:
            self.play_choice.ask(
                i18n.t(f"Sıra {side}'da. Pes mi ediyor?", f"{side} to play. Resign?"),
                [(i18n.t("Pes et", "Resign"), lambda: self.resign_game(True)),
                 (i18n.t("Vazgeç", "Cancel"), None)])
            return
        self.game.meta["result"] = how
        store.save(self.game)
        self.phase = "done"
        self._write_ending(scoring.result_sentence(0.0 if black else 1.0, how, None,
                                              scoring.side_names("duo"))
                        + i18n.t(f"{NL}{NL}Maç: {store.display_name(self.game.name)}",
                                f"{NL}{NL}Game: {store.display_name(self.game.name)}"), how)
        self.status.setText(i18n.t("pes edildi", "resigned") if how == "resign"
                            else i18n.t("süre doldu", "out of time"))
        self.buttons()
        self.refresh()

    def _submit(self):
        self.canvas.overlays, self.canvas.zone = [], None
        self.status.setText(i18n.t("analiz ediliyor…", "analysing…"))
        mv = list(self.game.moves)
        self.run(lambda: scoring.grade(self.eng, mv, "B", self.my_rank, self.next_rank),
                 self._graded)

    def _graded(self, f):
        self.attempts += 1
        self.facts = f
        store.add_record(self.game, {"move_no": len(self.game.moves), **f})
        store.save(self.game)
        self.phase = "graded"
        good = f["points_lost"] < 1.0
        sv = f.get("level_estimate")
        sv_txt = i18n.t(f"  ~{sv['label']} seviyesi", f"  ~{sv['label']} level") if sv else ""

        # RANKED: nothing leaks -- no score, no overlay, no findings.
        if self.ranked:
            self.canvas.overlays, self.canvas.zone = [], None
            self.say(i18n.t(
                f"{len(self.game.valid_records)}. hamlen {f['move']} oynandı ve "
                f"kaydedildi.{NL}{NL}Değerlendirme maç sonunda.",
                f"Your move {len(self.game.valid_records)}, {f['move']}, is played and "
                f"recorded.{NL}{NL}The review comes when the game ends."))
            self.status.setText(i18n.t("bot düşünüyor…", "bot thinking…"))
            self.buttons()
            self.refresh()
            self._bot_move()
            return

        # FREE: quiet on a normal move; only a blunder stops the game.
        stop = self.game.mode == "game" and scoring.category(f) == "blunder"
        if self.game.mode == "game" and not stop:
            self.canvas.overlays, self.canvas.zone = [], None
            self.buttons()
            self.refresh()
            self._bot_move()
            return

        self.canvas.overlays = [(m, C_MINE, "tri", f"{p:.0%}")
                                for m, p in f["your_level_plays"][:3]]
        self.canvas.overlays += [(m, C_NEXT, "tri2", "")
                                 for m, _ in f["next_level_plays"][:3]]
        if stop or (self.attempts >= 2 and f["points_lost"] >= 1.0):
            self.canvas.overlays.append((f["best_move"], C_BEST, "x", ""))
        self._card(f, stop)
        self.status.setText(i18n.t("maç durdu — blunder", "game stopped — blunder") if stop
                            else i18n.t(f"hamle {self.step + 1} / {self.plan_len}",
                                       f"move {self.step + 1} / {self.plan_len}"))
        self.buttons()
        self.refresh()

    def _card(self, f, stop):
        """Move card (HTML): category label, reason, alternatives if needed; the
        numbers sit behind the 'more' link."""
        e = html.escape
        cat = scoring.category(f)
        h = [f'<div style="font-family:Segoe UI; font-size:10pt">',
             f'<p style="font-size:14pt"><span style="background-color:'
             f'{scoring.CATEGORY_COLOR[cat]}; color:#ffffff">&nbsp;{e(scoring.category_name(cat))}'
             f'&nbsp;</span>&nbsp; <b>{e(f["move"])}</b> &nbsp;−{f["points_lost"]}p</p>']
        if stop:
            h.append("<p><b>" + e(i18n.t("Maç durdu: blunder.", "Game stopped: blunder.")) + "</b></p>")
        for b in f.get("findings", []):
            h.append(f"<p>• {e(scoring.finding_text(b))}</p>")
        if f["points_lost"] >= 1.0 and abs(f.get("region_change_points", 0)) >= 1.5:
            h.append("<p>" + e(_big(i18n.t(
                f"{scoring.region_name(f['most_changed_region'])} "
                f"tarafında {f['region_change_points']:+.1f} puan el değiştirdi.",
                f"{f['region_change_points']:+.1f} points changed hands "
                f"on the {scoring.region_name(f['most_changed_region'])} side."))) + "</p>")
        if stop:
            h.append("<p><b>" + e(i18n.t("Bunun yerine:", "Instead:")) + "</b><br>")
            for a in f.get("alternatives", []):
                diff = i18n.t("en iyi", "best") if a["diff"] <= .05 else f"−{a['diff']}p"
                h.append(f"&nbsp;&nbsp;<b>{e(a['move'])}</b> {e(diff)} → "
                         f"{e(' '.join(a['continuation']))}<br>")
            h.append("</p>")
        elif self.attempts >= 2 and f["points_lost"] >= 1.0:
            h.append("<p>" + e(i18n.t(f"En iyi: {f['best_move']}  devamı: "
                                     f"{' '.join(f['katago_line'])}",
                                     f"Best: {f['best_move']}  follow-up: "
                                     f"{' '.join(f['katago_line'])}")) + "</p>")
        link = f'<a style="color:{theme.active()["accent"]}" href="ayrinti">'
        if self._details:
            sv = f.get("level_estimate")
            split = [i18n.t(f"Skor: {f['prev_score_lead']:+.1f} → {f['next_score_lead']:+.1f}",
                         f"Score: {f['prev_score_lead']:+.1f} → {f['next_score_lead']:+.1f}")]
            split += [x for x in self._level_line(f).split(NL) if x]
            if sv:
                split.append(i18n.t(f"Seviye tahmini: ~{sv['label']}",
                                 f"Level estimate: ~{sv['label']}"))
            h.append("<p>" + link + "▾ " + e(i18n.t("ayrıntı", "details")) + "</a><br>"
                     + "<br>".join(e(x) for x in split) + "</p>")
        else:
            h.append("<p>" + link + "▸ " + e(i18n.t("ayrıntı", "details")) + "</a></p>")
        h.append('<p style="color:gray">' + e(
            i18n.t("→ 'geri al' düzeltir, 'devam' maça döner.", "→ 'undo' fixes it, "
                  "'continue' resumes.") if stop else
            i18n.t("→ 'devam' rakibin cevabını oynatır.", "→ 'continue' plays the reply."))
            + "</p></div>")
        self.out.setHtml("".join(h))

    def _card_link(self, url):
        if url.toString() == "more" and self.facts:
            self._details = not self._details
            self._card(self.facts, self.game.mode == "game")

    def _level_line(self, f):
        bk = f.get("your_level_expected_loss")
        s = (f"{NL}▲ {self.my_rank}: "
             + " ".join(m for m, _ in f["your_level_plays"][:3])
             + f"{NL}△ {self.next_rank}: "
             + " ".join(m for m, _ in f["next_level_plays"][:3]))
        if bk is not None:
            s += i18n.t(f"{NL}{NL}Bir {self.my_rank} burada ortalama {bk}p kaybeder, "
                       f"sen {f['points_lost']}p kaybettin.",
                       f"{NL}{NL}A {self.my_rank} loses {bk}p here on average; "
                       f"you lost {f['points_lost']}p.")
        return s

    def _bot_move(self):
        # PASSING IS NOT LEFT TO THE HUMAN NET. KataGo's own documentation warns that
        # the human SL model does not reliably know when to pass. The information is
        # already in the `opponent_reply` field, for FREE.
        if self.facts and self.facts.get("opponent_reply") == "pass":
            self.status.setText(i18n.t("bot pas geçiyor…", "bot passes…"))
            self._bot_played(["pass"])
            return
        self.status.setText(i18n.t("bot düşünüyor…", "bot thinking…"))
        mv = list(self.game.moves)
        if self.ranked:
            value, setting = self.opponent_value, self.RANKED_SETTINGS
        else:
            value, setting = scoring.rank_value(self.bot_rank), {}
        # below 20k: the 20k profile + the measured temperature (scoring.bot_setting)
        profile_, setting = scoring.bot_setting(value, self.cfg.size, setting)
        self.run(lambda: self.eng.human_sample(mv, profile_, **setting), self._bot_played)

    def advance(self):
        if self.phase != "graded" or not self.facts:
            return
        if self.game.mode == "game":
            self.canvas.overlays, self.canvas.zone = [], None
            self.say(self._intro)
            self._bot_move()
            return
        self.scores.append(self.facts)
        self.step += 1
        mv = self.facts["opponent_reply"]
        try:
            ok = mv not in (None, "pass") and self.board.play(from_gtp(mv, self.cfg.size), -1)
        except ValueError:
            ok = False
        if ok:
            self.game.moves.append(["W", mv])
            self.tick()
        else:
            mv = "pass"
            self.board.play(None, -1)
            self.game.moves.append(["W", "pass"])
        store.save(self.game)
        self.attempts, self.facts = 0, None
        self.canvas.overlays, self.canvas.zone = [], None
        if self.step >= self.plan_len:
            self._summary()
        else:
            self.phase = "your_turn"
            self.status.setText(i18n.t(
                f"rakip {mv} — hamle {self.step + 1} / {self.plan_len}",
                f"opponent {mv} — move {self.step + 1} / {self.plan_len}"))
            self.buttons()
            self.refresh()

    def _summary(self):
        self.phase = "done"
        cat = [scoring.category(f) for f in self.scores]
        saw = sum(k in ("excellent", "great", "good") for k in cat)
        sv = scoring.game_level(self.game.valid_records)
        self.say(i18n.t(f"Ders bitti.{NL}{NL}Hamlelerin: ", f"Lesson over.{NL}{NL}Your moves: ")
                 + " · ".join(scoring.category_name(k) for k in cat) + f"{NL}{NL}"
                 + i18n.t(f"{saw}/{len(self.scores)} hamlede planı gördün.{NL}",
                         f"You saw the plan on {saw}/{len(self.scores)} moves.{NL}")
                 + (i18n.t(f"{NL}Bu derste ~{sv['label']} oynadın.{NL}",
                          f"You played at ~{sv['label']} in this lesson.{NL}") if sv else "")
                 + i18n.t(f"{NL}Başka bir pozisyon için Yeni maç'a bas.",
                         f"{NL}Press New game for another position."))
        self.status.setText(i18n.t("ders bitti", "lesson over"))
        self.buttons()
        self.refresh()

    def _bot_played(self, cands):
        for mv in cands:
            if mv != "pass" and self.board.play(from_gtp(mv, self.cfg.size), -1):
                self.game.moves.append(["W", mv])
                self.tick()
                break
        else:
            self.board.play(None, -1)
            self.game.moves.append(["W", "pass"])
        store.save(self.game)
        if goban.game_over(self.game.moves):
            self._finish()
            return
        self.phase = "your_turn"
        self.status.setText(i18n.t(f"bot: {self.game.moves[-1][1]} — sıra sende",
                                  f"bot: {self.game.moves[-1][1]} — your turn"))
        if self.game.moves[-1][1] == "pass":
            lead = self.facts["next_score_lead"] if self.facts else 0.0
            what = (i18n.t("Kazanıyorsun — pas doğru hamle.",
                        "You are ahead — passing is the right move.") if lead > 0
                  else i18n.t("Geridesin — pas geçersen kaybedersin.",
                             "You are behind — if you pass you lose."))
            self.add(i18n.t(
                f"{NL}{NL}— Bot PAS geçti: oynayacak kârlı hamlesi kalmadı.{NL}"
                f"Sen de 'pas' dersen oyun biter, skor {lead:+.1f}.{NL}",
                f"{NL}{NL}— The bot PASSED: nothing profitable left to play.{NL}"
                f"If you pass too the game ends, score {lead:+.1f}.{NL}") + what)
        self.buttons()
        self.refresh()

    def _finish(self):
        """Two passes: the game is over. The COUNT decides the winner, not KataGo's estimate.

        The count is an engine query (ownership map), done in the background
        so the UI does not block. The ranked result is also applied and saved
        THERE: if the tab changes and the callback is dropped (gen), only the
        explanation is lost, not the SP.
        """
        self.phase = "done"
        # grade() was never called in a two-player game: 0.0 would read as a "draw
        # estimate", so the count stays the only source.
        lead = (self.facts["next_score_lead"] if self.facts else
                None if self.game.mode == "duo" else 0.0)
        sides = scoring.side_names(self.game.mode)
        game, eng, ranked = self.game, self.eng, self.ranked
        opponent = self.opponent_value if ranked else None
        records = game.valid_records
        sv = scoring.game_level(records)

        def count_and_apply():
            s = scoring.final_count(eng, list(game.moves), game.size, game.komi)
            S, source, diff = scoring.game_result(s, lead)
            event = (_apply_ranked(game, S, diff, opponent, sv, "two_passes")
                    if ranked else None)
            return s, S, source, diff, event

        def arrived(r):
            s, S, source, diff, event = r
            t = [i18n.t("Oyun bitti — iki taraf da pas geçti.",
                       "Game over — both sides passed."),
                 scoring.result_sentence(S, source, diff, sides), ""]
            t += scoring.count_lines(s, lead, sides)
            t += self._result_summary(game.name, records, sv, event, opponent)
            self.phase = "done"
            self.say(NL.join(t))
            self.status.setText(i18n.t("dereceli maç bitti", "ranked game over") if ranked
                                else i18n.t("oyun bitti", "game over"))
            self.buttons()
            self.refresh()

        self.say(i18n.t(f"Oyun bitti — iki taraf da pas geçti.{NL}{NL}sayılıyor…",
                       f"Game over — both sides passed.{NL}{NL}counting…"))
        self.run(count_and_apply, arrived)

    def _result_summary(self, name, records, sv, event, opponent):
        """The part of the end-of-game text after the count. In ranked (with an event)
        the scores hidden during the game are revealed here."""
        t = []
        if event is None:
            if sv:
                t.append(i18n.t(f"{NL}Bu maçta ~{sv['label']} oynadın "
                               f"({sv['move_count']} hamle).",
                               f"{NL}You played at ~{sv['label']} in this game "
                               f"({sv['move_count']} moves)."))
        else:
            t += ["", i18n.t(f"Rakip: {scoring.value_rank(opponent)}",
                            f"Opponent: {scoring.value_rank(opponent)}")] + _event_lines(event)
            if records:
                say = scoring.category_count(records)
                t += ["", i18n.t("Maç boyunca gizlenen değerlendirme",
                                "The review hidden during the game"),
                      i18n.t(f"Toplam kayıp {sum(r['points_lost'] for r in records):.1f}p · ",
                            f"Total loss {sum(r['points_lost'] for r in records):.1f}p · ")
                      + " · ".join(f"{scoring.category_name(k)} {say[k]}"
                                   for k in scoring.CATEGORIES if say[k])]
                if sv:
                    t.append(i18n.t(f"Hamle kalitesi: ~{sv['label']} ({sv['move_count']} hamle)",
                                   f"Move quality: ~{sv['label']} ({sv['move_count']} moves)"))
                t.append(i18n.t(f"{NL}En pahalı 3 hamle:", f"{NL}The 3 most expensive moves:"))
                for r in sorted(records, key=lambda r: -r["points_lost"])[:3]:
                    t.append(f"  {r['move_no']:3}. {r['move']:4} -{r['points_lost']:.1f}p"
                             f"  → {r['best_move']}")
        t.append(i18n.t(f"{NL}Maç: {store.display_name(name)}{NL}"
                       f"Maçlar ekranından hamle hamle inceleyebilirsin.",
                       f"{NL}Game: {store.display_name(name)}{NL}"
                       f"You can review it move by move from the Games screen."))
        return t

    # ---------- buttons ----------
    def hint(self):
        f = self.facts
        if not f or self.phase != "graded":
            return
        if self.canvas.zone is None:
            self.canvas.zone = REGIONS.index(f["most_changed_region"])
            self.status.setText(i18n.t(
                f"İpucu: {scoring.region_name(f['most_changed_region'])} tarafına bak",
                f"Hint: look at the {scoring.region_name(f['most_changed_region'])} side"))
        else:
            self.canvas.overlays.append((f["best_move"], C_BEST, "x", ""))
            self.status.setText(i18n.t(f"En iyi hamle: {f['best_move']}",
                                      f"Best move: {f['best_move']}"))
        self.buttons()
        self.refresh()

    def retry(self):
        if self.phase != "graded":
            return
        if self.facts:
            store.add_record(self.game, {"move_no": len(self.game.moves),
                                        "move": self.facts["move"],
                                        "retracted": True})
        if self.game.mode == "lesson" and self.task:
            self.game.moves = self.game.moves[:len(self.task[0]) + self.step * 2]
        else:
            while self.game.moves and self.game.moves[-1][0] == "W":
                self.game.moves.pop()
            if self.game.moves:
                self.game.moves.pop()
        self.board = self.game.board_at()
        store.save(self.game)
        self.canvas.zone, self.canvas.overlays = None, []
        self.facts, self.watch = None, None
        self.phase = "your_turn"
        if self.game.mode == "game":
            self.say(self._intro)
        self.status.setText(i18n.t("geri alındı", "undone"))
        self.buttons()
        self.refresh()

    # ---------- archive ----------
    def archive_fill(self):
        self._muted = True
        self.game_list.clear()
        self.saved = store.list_all()
        t = theme.active()
        for name, n, _, size_ in self.saved:
            try:
                dz = len(store.load_game(name).unfixed)
            except (OSError, ValueError):
                dz = 0
            end = name.split("_")[-1]
            date = (f"{end[6:8]}.{end[4:6]} {end[9:11]}:{end[11:13]}"
                     if len(end) >= 15 and end[:8].isdigit() else "")
            it = QListWidgetItem("  ·  ".join(x for x in (store.display_name(name), date,
                                                          f"{size_}x{size_}",
                                                          i18n.t(f"{n} hamle", f"{n} moves")) if x)
                                 + self._blunder_badge(dz))
            it.setData(Qt.UserRole, _blend(t["panel"], KIND_TINT.get(store.mode_of(name),
                                                                     t["edge"]), 0.14))
            self.game_list.addItem(it)
        self._muted = False
        if not self.saved:
            self.say_archive(i18n.t(f"Henüz kayıtlı maç yok.{NL}Bir maç oyna, buraya düşecek.",
                                 f"No saved games yet.{NL}Play a game and it lands here."))

    def _all_games(self):
        """[(name, records)] -- newest first. The trend tab lines these up.

        The same scan as pattern_text(); that one also wants `moves`, this one
        does not. Kept separate because the trend runs on every open and should
        not pay for needless replays.
        """
        out = []
        for name, *_ in store.list_all():
            try:
                m = store.load_game(name)
            except OSError:
                continue
            if m.valid_records:
                out.append((name, m.valid_records))
        return out

    def show_charts(self):
        """Statistics charts of the selected game. The engine is NOT NEEDED -- all
        from data in the jsonl, it opens even without KataGo."""
        if not self.archive:
            self.say_archive(PICK_GAME())
            return
        if not self.archive.valid_records:
            self.say_archive(f"{store.display_name(self.archive.name)}{NL}{NL}" + NO_SCORE())
            return
        self._chart_view = charts.ChartView(
            None, self.archive, skip=self.skip_charts,
            all_games=self._all_games())
        self.open_page(i18n.t(f"Grafikler · {store.display_name(self.archive.name)}",
                            f"Charts · {store.display_name(self.archive.name)}"),
                      self._chart_view)

    def skip_charts(self, move_no):
        """A click coming from a chart: closes the page and takes the archive to that move."""
        if not self.archive:
            return
        self.close_page()
        self.archive_branch = None
        self.archive_i = max(0, min(len(self.archive.moves), int(move_no)))
        self.show_move()

    def write_brief(self):
        """Writes the selected game's brief to a file. No LLM is called: the hard part
        is not reaching a model but throwing the noise out of the jsonl -- and
        that is plain Python."""
        if not self.archive:
            self.say_archive(PICK_GAME())
            self.settings_out.setText(i18n.t("Önce Maçlar'dan bir maç seç.",
                                        "Select a game in Games first."))
            return
        k = self.archive.valid_records
        if not k:
            self.say_archive(f"{store.display_name(self.archive.name)}{NL}{NL}" + NO_SCORE())
            return
        text_ = scoring.brief(self.archive.name, self.archive.mode, self.archive.size,
                              k, scoring.game_level(k))
        path_ = store.sub("brief", f"{self.archive.name}_brief.md")
        try:
            with open(path_, "w", encoding="utf-8") as f:
                f.write(text_)
        except OSError as e:
            self.say_archive(i18n.t(f"Yazılamadı: {e}", f"Could not write: {e}"))
            return
        self._show_page(f"Brief · {store.display_name(self.archive.name)}", text_, markdown=True)
        self.say_archive(i18n.t(f"Brief yazıldı ({len(text_)} karakter):{NL}{NL}{path_}",
                             f"Brief written ({len(text_)} characters):{NL}{NL}{path_}"))
        self.settings_out.setText(i18n.t(f"Brief yazıldı: {path_}", f"Brief written: {path_}"))

    def licenses(self):
        """Third-party notices. The file ships with the package; from source it is in
        the repository root. Something the user MUST be able to see -- Qt,
        Qt's FFmpeg DLLs and edge-tts are in the package under the LGPL."""
        title = i18n.t("Lisanslar", "Licenses")
        for path_ in (bundle.beside("THIRD-PARTY-NOTICES.md"),
                    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "THIRD-PARTY-NOTICES.md")):
            try:
                with open(path_, encoding="utf-8") as f:
                    return self._show_page(title, f.read(), markdown=True)
            except OSError:
                continue
        # A missing file is not passed over in silence: if the notice fell out of the
        # package that is a compliance problem, and the user is told where to look.
        return self._show_page(title, i18n.t(
            "THIRD-PARTY-NOTICES.md bulunamadı. Lisans metinleri uygulamanın "
            "yanındaki `LICENSES` klasöründe ve projenin deposunda:"
            f"{NL}{NL}{PROJECT_URL}",
            "THIRD-PARTY-NOTICES.md not found. The license texts are in the "
            "`LICENSES` folder next to the application and in the project "
            f"repository:{NL}{NL}{PROJECT_URL}"), markdown=True)

    def _llm_setting(self, is_open):
        store.set_setting("video_llm", bool(is_open))

    def _lang_selected(self, i):
        """The preference is saved and the UI is reborn in the new language (no restart).

        The refresh is deferred with singleShot: this method runs inside
        lang_box's own signal, and deleting the box from inside its signal
        would crash.
        """
        store.set_setting("lang", i18n.set_lang(self.lang_box.itemData(i)))
        QTimer.singleShot(0, self._apply_lang)

    def _apply_lang(self):
        """Tears the widgets down, builds them in the new language, returns to the same screen.

        The MODE is kept, not the screen: Play's mode is read from the b_ranked
        button, and a rebuilt button fell back to free -- screen_select then
        took the live ranked game for a different game and killed it.
        """
        old = self.mode
        self._build_ui()      # setCentralWidget deletes the old one itself
        self.mode_select(old)

    def make_video(self):
        """'analysis video': play it if it exists, otherwise make it (it plays by itself when done)."""
        if not self.archive:
            self.say_archive(PICK_GAME())
            return
        if not self.archive.valid_records:
            self.say_archive(i18n.t(
                f"{store.display_name(self.archive.name)}{NL}{NL}Bu maçta değerlendirilmiş hamle yok.",
                f"{store.display_name(self.archive.name)}{NL}{NL}No graded moves in this game."))
            return
        name = self.archive.name
        if name == self._video_game:                 # already being made: show the progress
            d = self._open_video(name)
            if d:
                d.generating(self.video_state.text())
        elif os.path.exists(video.video_path(name)):
            self.watch_video(name)
        else:
            self.video_start(name)

    def _open_video(self, name):
        """The player window; None without QtMultimedia."""
        if QMediaPlayer is None:
            return None
        if not self._video_view:
            self._video_view = VideoView(self.video_start)
        d = self._video_view
        d.name = name
        self.open_page(i18n.t(f"Analiz videosu · {store.display_name(name)}",
                            f"Analysis video · {store.display_name(name)}"), d)
        return d

    def watch_video(self, name):
        path_ = video.video_path(name)
        d = self._open_video(name)
        if d:
            d.playback(path_)
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path_))
            self.say_archive(i18n.t(f"Video sistem oynatıcısında açıldı:{NL}{NL}{path_}",
                                 f"Video opened in the system player:{NL}{NL}{path_}"))

    def video_start(self, name):
        """Narrated analysis video with pictures, in the background. One at a time."""
        if self._video_game:
            self.video_state.setText(i18n.t(
                f"Önce bitsin: {store.display_name(self._video_game)} videosu üretiliyor.",
                f"Wait: the video for {store.display_name(self._video_game)} is being generated."))
            return
        self._video_game = name
        llm = self.llm_box.isChecked() and self.llm_box.isEnabled()
        self._open_video(name)
        self._video_status(i18n.t("metin yazılıyor" + (" (agy)" if llm else ""),
                              "writing the script" + (" (agy)" if llm else "")))

        self._background(lambda advance_: video.make_all(name, llm=llm, progress_=advance_),
                   self._video_progress, self._video_done)

    def _background(self, fn, progress_, done_):
        """Work that takes minutes, INDEPENDENT of the phase machine (video, import).

        run() is tied to gen: it drops the result when the tab/game changes and
        locks the game while waiting. These go into the queue with g=None and
        arrive no matter what. fn(advance) runs on the worker; progress((i, n))
        and done(result|exception) run on the UI.
        """
        def job():
            try:
                res = fn(lambda i, n: self.q.put((None, progress_, (i, n))))
            except Exception as e:
                res = e
            self.q.put((None, done_, res))
        threading.Thread(target=job, daemon=True).start()

    def _video_status(self, step):
        text_ = i18n.t(f"🎬 {store.display_name(self._video_game)}: video üretiliyor… {step}",
                      f"🎬 {store.display_name(self._video_game)}: generating video… {step}")
        self.video_state.setText(text_)
        self.video_state.show()
        d = self._video_view
        if d and d.name == self._video_game:
            d.generating(text_)

    def _video_progress(self, r):
        i, n = r
        self._video_status(i18n.t(f"seslendirme ve kareler {i + 1}/{n}",
                              f"voice and frames {i + 1}/{n}"))

    def _video_done(self, res):
        name, self._video_game = self._video_game, None
        d = self._video_view if self._video_view and self._video_view.name == name else None
        if isinstance(res, Exception):
            text_ = i18n.t(f"🎬 {store.display_name(name)}: video üretilemedi ({type(res).__name__}: {res})",
                          f"🎬 {store.display_name(name)}: video failed ({type(res).__name__}: {res})")
            self.video_state.setText(text_)
            if d:
                d.error(text_)
            return
        if d and self._open_page is d:
            self.video_state.hide()
            d.playback(res)
        else:
            self.video_state.setText(i18n.t(
                f"🎬 {store.display_name(name)}: video hazır. İzlemek için maçı seçip Analiz videosu'na bas.",
                f"🎬 {store.display_name(name)}: video ready. Select the game and press Analysis video."))

    def import_sgf(self):
        """Pick a file -> pick your color -> write to the archive -> grade in the background."""
        if not self.eng:
            self.sgf_status.setText(i18n.t("Motor hazır değil — başlaması bekleniyor.",
                                         "Engine not ready yet."))
            self.sgf_status.show()
            return
        if self.archive and self.archive.mode == "sgf" and sgf_import.missing(self.archive):
            self.sgf_start(self.archive)            # resume the unfinished one
            return
        path_, _ = QFileDialog.getOpenFileName(self, i18n.t("SGF dosyası seç", "Choose SGF file"),
                                             "", "SGF (*.sgf)")
        if not path_:
            return
        try:
            with open(path_, encoding="utf-8", errors="replace") as f:
                text_ = f.read()
            o = sgf_import.read(text_)
        except (OSError, ValueError) as e:
            self.say_archive(i18n.t(f"İçe aktarılamadı:{NL}{NL}{e}", f"Could not import:{NL}{NL}{e}"))
            return
        info_ = o["info"]
        hc = goban.setup_count(o["moves"])
        self.archive_choice.ask(
            f"{os.path.basename(path_)} · {o['size']}x{o['size']} · "
            f"{sum(c in ('B', 'W') for c, _ in o['moves'])} " + i18n.t("hamle", "moves")
            + (f" · {hc} " + i18n.t("handikap taşı", "handicap stones") if hc else "")
            + NL + i18n.t("Hangi renktin?", "Which color were you?"),
            [(i18n.t("Siyah", "Black") + f" — {info_.get('PB', '?')} {info_.get('BR', '')}".rstrip(),
              lambda: self.sgf_start(text_=text_, color="B")),
             (i18n.t("Beyaz", "White") + f" — {info_.get('PW', '?')} {info_.get('WR', '')}".rstrip(),
              lambda: self.sgf_start(text_=text_, color="W")),
             (i18n.t("Vazgeç", "Cancel"), None)])

    def sgf_start(self, game=None, text_=None, color="B"):
        """A new import (text + color) or resuming an unfinished one (game)."""
        if self._sgf_name:
            self.sgf_status.setText(i18n.t(f"Önce bitsin: {store.display_name(self._sgf_name)} puanlanıyor.",
                                         f"Wait: {store.display_name(self._sgf_name)} is being graded."))
            return
        try:
            m = game or sgf_import.create(text_, color)
        except (OSError, ValueError) as e:
            self.say_archive(i18n.t(f"İçe aktarılamadı:{NL}{NL}{e}", f"Could not import:{NL}{NL}{e}"))
            return
        self._sgf_name = m.name
        if self.mode == "archive":
            self.archive_fill()
        self._sgf_write(i18n.t("başlıyor", "starting"))
        start = time.monotonic()

        def progress_(r):
            # ~8 s per move (two 600-visit searches): a 19x19 game takes ~15 min. Without
            # a remaining-time line the app looks frozen.
            i, n = r
            remaining = (time.monotonic() - start) / i * (n - i) if i else None
            self._sgf_write(f"{i + 1}/{n}" + (i18n.t(f" · ~{remaining / 60:.0f} dk kaldı",
                                                  f" · ~{remaining / 60:.0f} min left")
                                            if remaining and remaining >= 60 else ""))
        self._background(lambda advance_: sgf_import.grade_moves(self.eng, m, self.my_rank,
                                                   self.next_rank, advance_),
                   progress_, self._sgf_done)

    def _sgf_write(self, step):
        self.sgf_status.setText(i18n.t(f"📥 {store.display_name(self._sgf_name)}: hamleler puanlanıyor… {step}",
                                     f"📥 {store.display_name(self._sgf_name)}: grading moves… {step}"))
        self.sgf_status.show()

    def _sgf_done(self, res):
        name, self._sgf_name = self._sgf_name, None
        if isinstance(res, Exception):
            self.sgf_status.setText(i18n.t(
                f"📥 {store.display_name(name)}: puanlama durdu ({type(res).__name__}: {res}){NL}"
                "Maçı seçip 'SGF içe aktar' kaldığı yerden sürdürür.",
                f"📥 {store.display_name(name)}: grading stopped ({type(res).__name__}: {res}){NL}"
                "Select the game and press 'import SGF' to resume."))
            return
        self.sgf_status.setText(i18n.t(
            f"📥 {store.display_name(name)}: içe aktarıldı, {len(res.valid_records)} hamle puanlandı.",
            f"📥 {store.display_name(name)}: imported, {len(res.valid_records)} moves graded."))
        if self.mode == "archive":
            self.archive_fill()
            row_ = next((i for i, t in enumerate(self.saved) if t[0] == name), None)
            if row_ is not None:
                self.game_list.setCurrentRow(row_)

    def _show_page(self, title, text_, markdown=False):
        """Long text: not a separate window but a page with "← Back"."""
        t = _txt()
        if markdown:
            t.setMarkdown(text_)
        else:
            t.setHtml(_html(text_))
        self.open_page(title, t)
        return t

    def open_page(self, title, w, width_=None):
        """A full page in place of board+panel. The sidebar stays.
        width: narrow content (tour, welcome) is centered, not stretched."""
        if self._open_page is not w:
            self.close_page()
            if width_:
                w.setFixedWidth(width_)
            self._page_layout.addWidget(w, 1, Qt.AlignHCenter if width_ else Qt.Alignment())
            self._open_page = w
        w.show()
        self.page_title.setText(title)
        self.content.setCurrentIndex(1)

    def close_page(self):
        w, self._open_page = self._open_page, None
        self.content.setCurrentIndex(0)
        if w is None:
            return
        w.hide()                          # video: hideEvent releases the file
        self._page_layout.removeWidget(w)
        if w is not self._video_view:        # the player is reused
            w.deleteLater()

    def pattern_text(self):
        """Makes the pattern report and writes it to a file. Returns: (text, path)."""
        games = []
        for name, *_ in store.list_all():
            m = store.load_game(name)
            if m.valid_records:
                games.append((name, m.size, m.moves, m.valid_records))
        text_ = scoring.pattern(games)
        path_ = store.sub("report", "_patterns.md")
        with open(path_, "w", encoding="utf-8") as f:
            f.write(text_)
        return text_, path_

    # ---------- analysis ----------
    def analysis_select(self):
        """Three subtabs: mistake review (with board), habits (patterns), progress."""
        k = self.analysis_tabs.currentIndex()
        self.analysis_stack.setCurrentIndex(k)
        self.panel()
        if k == 0:
            self.load_review()
        elif k == 1:
            try:
                text_, _ = self.pattern_text()
            except OSError as e:
                text_ = i18n.t(f"Yazılamadı: {e}", f"Could not write: {e}")
            self.pattern_out.setMarkdown(text_)
        else:
            self._build_progress()
        self.refresh()

    def _build_progress(self):
        d = self.progress.layout()
        while d.count():
            w = d.takeAt(0).widget()
            if w:
                w.deleteLater()
        row_ = []
        for size_ in SIZES:
            r = store.rank_state(size_)
            if r["game"] == 0:
                continue
            remaining = scoring.PLACEMENT_GAMES - len(r["placement"])
            row_.append(f"{size_}x{size_}:  " + (
                i18n.t(f"yerleştirme — {remaining} maç kaldı", f"placement — {remaining} games left")
                if remaining > 0 else
                f"{scoring.value_rank(r['value'])} · {r['sp']:.0f} SP · {r['game']} "
                + i18n.t("maç", "games")
                + (f" · {r['streak']} " + i18n.t("maçlık seri", "win streak")
                   if r["streak"] >= 2 else "")))
        e = QLabel(NL.join(row_) or i18n.t("Henüz dereceli maç yok.", "No ranked games yet."))
        e.setFont(QFont("Segoe UI", 11, QFont.Bold))
        d.addWidget(e)
        d.addWidget(charts.trend_panel(self._all_games()), 1)

    def load_review(self):
        """The first due card. Card = a fixed blunder (store.review_cards)."""
        today = self._today()
        cards = store.review_cards(today)
        term_ = [k for k, v in cards if v <= today.isoformat()]
        self.canvas.overlays, self.canvas.zone = [], None
        self.review, self.review_board = None, None
        if not term_:
            if cards:
                text_ = i18n.t(f"Bugün tekrar yok ✓{NL}{NL}Sıradaki kart: {cards[0][1]} "
                              f"({len(cards)} kart)",
                              f"Nothing to review today ✓{NL}{NL}Next card: {cards[0][1]} "
                              f"({len(cards)} cards)")
            else:
                text_ = i18n.t("Henüz kart yok.{NL}{NL}Maçlar'da blunder düzelttikçe kartlar "
                              "buraya düşer ve unutmak üzereyken tekrar sorulur "
                              "(1 → 3 → 7 → 14 → 30 gün).".replace("{NL}", NL),
                              "No cards yet.{NL}{NL}Fix blunders in Games; they come back "
                              "here right before you would forget them "
                              "(1 → 3 → 7 → 14 → 30 days).".replace("{NL}", NL))
            self._write(self.review_out, text_)
            self.b_review.setEnabled(False)
            self.refresh()
            return
        card = term_[0]
        state_ = store.review_state().get(card, {})
        box = i18n.t(f"Bugün {len(term_)} kart · kutu {state_.get('box', 0) + 1}/5",
                     f"{len(term_)} cards today · box {state_.get('box', 0) + 1}/5")
        name, no = card.rsplit("#", 1)
        m, no = store.load_game(name), int(no)
        shown_no = goban.display_no(m.moves, no)
        k = m.record_of(no)
        self.review = (card, m, no, None)
        color = i18n.t("siyah", "black") if m.color == "B" else i18n.t("beyaz", "white")
        self._write(self.review_out, i18n.t(
            f"{box}{NL}{NL}"
            f"{store.display_name(name)}, {shown_no}. hamle.{NL}Sıra sende ({color}). Tahtaya tıkla.{NL}{NL}"
            f"(maçta {k['move']} oynamıştın, {k['points_lost']}p kaybettirmişti)",
            f"{box}{NL}{NL}"
            f"{store.display_name(name)}, move {shown_no}.{NL}Your turn ({color}). Click the board.{NL}{NL}"
            f"(in the game you played {k['move']}, losing {k['points_lost']}p)"))
        self.b_review.setEnabled(False)
        self.refresh()

    def review_answer(self, idx):
        if not self.review or self.review[3] is not None:
            return
        card, m, no, _ = self.review
        if card.startswith(tsumego.PREFIX):
            return self._tsumego_cevap(idx)
        moves_ = m.moves[:no - 1]
        col = m.moves[no - 1][0]
        b = m.board_at(no - 1)
        if not b.play(idx, goban.color(col)):
            self.status.setText(i18n.t("yasa dışı hamle", "illegal move"))
            return
        if not self.eng:
            self.status.setText(i18n.t("motor hazır değil", "engine not ready"))
            return
        gtp = to_gtp(idx, m.size)
        self.review, self.review_board = (card, m, no, "waiting"), b
        self.canvas.overlays = []
        self.tick()
        self._write(self.review_out, i18n.t(f"{gtp} — puanlanıyor…", f"{gtp} — grading…"))
        self.refresh()

        def done_(res):
            if self.review is None or self.review[0] != card:
                return
            if isinstance(res, Exception):
                self.review = (card, m, no, None)
                self._write(self.review_out, i18n.t(f"puanlanamadı: {res}", f"grading failed: {res}"))
                return
            cat = scoring.category(res)
            correct = cat != "blunder"
            new = store.apply_review(card, correct, self._today())
            self.review = (card, m, no, res)
            old = m.record_of(no)
            self.canvas.overlays = [(old["best_move"], C_BEST, "x", "")]
            self._write(self.review_out, NL.join([
                (i18n.t("✓ Doğru", "✓ Correct") if correct else i18n.t("✗ Yine blunder", "✗ Blunder again"))
                + f" — {scoring.category_name(cat)} ({res['points_lost']}p)", "",
                i18n.t(f"En iyi: {old['best_move']} (✕)", f"Best: {old['best_move']} (✕)"),
                i18n.t(f"Sonraki tekrar: {new['next']}", f"Next review: {new['next']}")]))
            self.b_review.setEnabled(True)
            if self.mode == "analysis":
                self.refresh()
        self._background(lambda _step_progress: scoring.grade(
            self.eng, moves_ + [[col, gtp]], col, self.my_rank, self.next_rank,
            size=m.size, komi=m.komi, rule=goban.sgf_rule(m.info)), None, done_)

    # ---------- Problems ----------
    # Two sources, one solving flow: your own blunders (graded by the engine) and
    # tsumego sets (the answer key is the SGF's main line). The card id's prefix
    # tells them apart. Sets are solved IN ORDER -- the lock comes from
    # tsumego.set_problems.
    def _problem_panel(self):
        container = QWidget()
        d = QVBoxLayout(container)
        d.setContentsMargins(0, 0, 0, 0)
        upper = QHBoxLayout()
        b = QPushButton(i18n.t("＋ Set ekle", "＋ Add set"))
        b.clicked.connect(self.prob_add)
        upper.addWidget(b)
        b = QPushButton(i18n.t("Klasörden…", "From folder…"))
        b.clicked.connect(self.prob_add_folder)
        upper.addWidget(b)
        upper.addStretch(1)
        d.addLayout(upper)
        self.prob_list = QListWidget(objectName="gamelist")
        self.prob_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.prob_list.itemClicked.connect(
            lambda it: self.prob_select(self.prob_list.row(it)))
        d.addWidget(self.prob_list, 1)
        self.prob_out = _txt()
        d.addWidget(self.prob_out, 1)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        self.b_prob_next = _primary(QPushButton(i18n.t("Sonraki", "Next")))
        self.b_prob_next.clicked.connect(self.prob_next)
        bottom.addWidget(self.b_prob_next)
        d.addLayout(bottom)
        return container

    def prob_drop(self):
        """Leave the problem: clear the board and the queue. The selected set stays."""
        self.prob, self.prob_board = None, None
        self.prob_source, self.prob_queue = None, []
        self.canvas.overlays, self.canvas.zone = [], None
        self.b_prob_next.setEnabled(False)

    def _problem_row(self, kind, value, text_):
        """Adds a row to the list. If kind is None the row is not clickable (solved/locked)."""
        it = QListWidgetItem(text_)
        if kind is None:
            it.setFlags(it.flags() & ~Qt.ItemIsEnabled)
        self.prob_list.addItem(it)
        self.prob_rows.append((kind, value))

    def prob_fill(self):
        """At the root: your Mistakes + Reviews + sets; with a set selected, its problems."""
        self.prob_list.clear()
        self.prob_rows = []
        today = self._today()
        if self.prob_set is None:
            cards = store.review_cards(today)
            term_ = [k for k, v in cards if v <= today.isoformat()]
            self._problem_row("mistakes" if term_ else None, None, i18n.t(
                f"⚠  Hataların  ·  bugün {len(term_)} / toplam {len(cards)}",
                f"⚠  Your mistakes  ·  {len(term_)} due / {len(cards)} total"))
            single = tsumego.reviews(today)
            self._problem_row("review" if single else None, None, i18n.t(
                f"🔁  Tekrarlar  ·  bugün {len(single)} problem",
                f"🔁  Reviews  ·  {len(single)} problems due"))
            sets = tsumego.sets()
            for name, t, c in sets:
                self._problem_row("set", name,
                                 f"🧩  {name}  ·  {c}/{t}" + (" ✓" if c >= t else ""))
            if not sets:
                self._problem_row(None, None, i18n.t(
                    "Henüz problem seti yok — “Set ekle” ile .zip ya da .sgf ekle.",
                    "No problem sets yet — use “Add set” for a .zip or .sgf."))
        else:
            self._problem_row("back", None, i18n.t("←  Setler", "←  Sets"))
            for pid, state_ in tsumego.set_problems(self.prob_set):
                icon = {"solved": "✓", "next_up": "▶", "locked": "🔒"}[state_]
                self._problem_row("problem" if state_ == "next_up" else None, pid,
                                 f"{icon}  {pid.split('/', 1)[-1]}")

    def prob_select(self, row_):
        if not 0 <= row_ < len(self.prob_rows):
            return
        kind, value = self.prob_rows[row_]
        if kind == "back":
            self.prob_set = None
            self.prob_drop()
            self.prob_fill()
            self._write(self.prob_out, "")
            self.refresh()
        elif kind == "set":
            self.prob_set = value
            self.prob_drop()
            self.prob_source = "set"
            self.prob_fill()
            self.prob_next()
        elif kind in ("mistakes", "review"):
            today = self._today()
            self.prob_drop()
            self.prob_source = kind
            source = (store.review_cards(today) if kind == "mistakes"
                      else tsumego.reviews(today))
            self.prob_queue = [k for k, v in source if v <= today.isoformat()]
            self.prob_fill()
            self.prob_next()
        elif kind == "problem":
            self.prob_source = "set"
            self.prob_load(tsumego.PREFIX + value)

    def prob_next(self):
        """The next card in the source. In a set the order comes from tsumego.next_up --
        a wrongly answered problem stays in box 0 and comes up again."""
        self.prob, self.prob_board = None, None
        self.canvas.overlays = []
        self.b_prob_next.setEnabled(False)
        if self.prob_source == "set":
            pid = tsumego.next_up(self.prob_set) if self.prob_set else None
            if pid:
                return self.prob_load(tsumego.PREFIX + pid)
            self._write(self.prob_out, i18n.t(
                f"Set bitti ✓{NL}{NL}Bütün problemler çözüldü. Unutmadan önce "
                f"“Tekrarlar”da geri gelecekler (1 → 3 → 7 → 14 → 30 gün).",
                f"Set complete ✓{NL}{NL}All problems solved. They come back under "
                f"“Reviews” right before you would forget them (1 → 3 → 7 → 14 → 30 days)."))
        elif self.prob_queue:
            return self.prob_load(self.prob_queue.pop(0))
        elif self.prob_source:
            self._write(self.prob_out, i18n.t("Bugünlük bitti ✓", "Done for today ✓"))
        self.prob_fill()
        self.refresh()

    def prob_load(self, card):
        """Set the card up on the board. A `tsumego#`-prefixed card is an SGF problem, otherwise a blunder."""
        self.prob_board, self.canvas.overlays = None, []
        state_ = store.review_state().get(card, {})
        box = i18n.t(f"kutu {state_.get('box', 0) + 1}/5",
                     f"box {state_.get('box', 0) + 1}/5")
        if card.startswith(tsumego.PREFIX):
            pid = card[len(tsumego.PREFIX):]
            try:
                m, no, _ = tsumego.load_problem(pid)
            except (OSError, ValueError) as e:      # file deleted/broken: skip
                self.prob = None
                self._write(self.prob_out, i18n.t(f"Problem okunamadı ({pid}): {e}",
                                               f"Cannot read problem ({pid}): {e}"))
                self.b_prob_next.setEnabled(True)
                self.refresh()
                return
            col, title = m.color, f"🧩  {pid}"
            hint_ = i18n.t("Hayati noktayı bul.", "Find the vital point.")
        else:
            name, n = card.rsplit("#", 1)
            m, no = store.load_game(name), int(n)
            shown_no = goban.display_no(m.moves, no)
            k = m.record_of(no)
            col = m.moves[no - 1][0]
            title = f"⚠  {store.display_name(name)} · {shown_no}. " + i18n.t("move", "move")
            hint_ = i18n.t(f"Maçta {k['move']} oynamıştın, {k['points_lost']}p kaybettirmişti.",
                          f"In the game you played {k['move']}, losing {k['points_lost']}p.")
        color = i18n.t("siyah", "black") if col == "B" else i18n.t("beyaz", "white")
        self.prob = (card, m, no, None)
        self._write(self.prob_out, NL.join([
            title, box, "",
            i18n.t(f"Sıra sende ({color}). Tahtaya tıkla.",
                  f"Your turn ({color}). Click the board."), "", hint_]))
        self.b_prob_next.setEnabled(False)
        self.refresh()

    def prob_answer(self, idx):
        if not self.prob or self.prob[3] is not None:
            return
        card, m, no, _ = self.prob
        tsu = card.startswith(tsumego.PREFIX)
        col = m.color if tsu else m.moves[no - 1][0]
        b = m.board_at(no - 1)
        if not b.play(idx, goban.color(col)):
            self.status.setText(i18n.t("yasa dışı hamle", "illegal move"))
            return
        gtp = to_gtp(idx, m.size)
        if tsu:
            try:
                solution = tsumego.load_problem(card[len(tsumego.PREFIX):])[2]
            except (OSError, ValueError) as e:
                self.status.setText(i18n.t(f"problem okunamadı: {e}",
                                          f"cannot read problem: {e}"))
                return
            self._problem_result(card, m, no, b, gtp == solution, solution, None)
            return
        if not self.eng:
            self.status.setText(i18n.t("motor hazır değil", "engine not ready"))
            return
        self.prob, self.prob_board = (card, m, no, "waiting"), b
        self._write(self.prob_out, i18n.t(f"{gtp} — puanlanıyor…", f"{gtp} — grading…"))
        self.refresh()

        def done_(res):
            if self.prob is None or self.prob[0] != card:
                return
            if isinstance(res, Exception):
                self.prob = (card, m, no, None)
                self._write(self.prob_out, i18n.t(f"puanlanamadı: {res}",
                                               f"grading failed: {res}"))
                return
            cat = scoring.category(res)
            self._problem_result(card, m, no, b, cat != "blunder",
                             m.record_of(no)["best_move"],
                             f"{scoring.category_name(cat)} ({res['points_lost']}p)")
        self._background(lambda _step_progress: scoring.grade(
            self.eng, m.moves[:no - 1] + [[col, gtp]], col, self.my_rank,
            self.next_rank, size=m.size, komi=m.komi,
            rule=goban.sgf_rule(m.info)), None, done_)

    def _problem_result(self, card, m, no, b, correct, mark, extra):
        """Save the answer, show the board as a draft, refresh the locks."""
        new = store.apply_review(card, correct, self._today())
        self.prob, self.prob_board = (card, m, no, correct), b
        self.canvas.overlays = [(mark, C_BEST, "x", "")] if mark else []
        self.tick()
        start = i18n.t("✓ Doğru", "✓ Correct") if correct else i18n.t("✗ Yanlış", "✗ Wrong")
        row_ = [start + (f" — {extra}" if extra else "")]
        if mark:
            row_.append(i18n.t(f"Doğrusu: {mark} (✕)", f"Answer: {mark} (✕)")
                         if not correct else
                         i18n.t(f"En iyi: {mark} (✕)", f"Best: {mark} (✕)"))
        row_ += ["", i18n.t(f"Sonraki tekrar: {new['next']}",
                            f"Next review: {new['next']}")]
        self._write(self.prob_out, NL.join(row_))
        self.b_prob_next.setEnabled(True)
        self.prob_fill()
        self.refresh()

    def prob_add(self):
        """Add a set: a .zip or one or more .sgf files."""
        paths, _ = QFileDialog.getOpenFileNames(
            self, i18n.t("Problem seti: .zip ya da .sgf dosyaları",
                        "Problem set: .zip or .sgf files"), "",
            i18n.t("Problem seti (*.zip *.sgf)", "Problem set (*.zip *.sgf)"))
        if paths:
            # A single file makes its own name the set; several files are gathered under
            # the name of the folder they are in -- 900 .sgf files should not become 900 sets.
            self._import_problems(paths, None if len(paths) == 1 else
                             os.path.basename(os.path.dirname(paths[0])) or "set")

    def prob_add_folder(self):
        path_ = QFileDialog.getExistingDirectory(
            self, i18n.t("Problem klasörü seç", "Choose problem folder"))
        if path_:
            self._import_problems([path_], None)

    def _import_problems(self, sources, name):
        names, appended, skipped = [], 0, 0
        for k in sources:
            try:
                a, e, drop = tsumego.import_set(k, name)
            except (OSError, ValueError) as error:
                self._write(self.prob_out, i18n.t(f"Aktarılamadı: {error}",
                                               f"Import failed: {error}"))
                return
            names.append(a)
            appended, skipped = appended + e, skipped + drop
        self.prob_set = None
        self.prob_drop()
        self.prob_fill()
        cluster = ", ".join(sorted(set(names)))
        self._write(self.prob_out, i18n.t(
            f"“{cluster}” · {appended} problem eklendi"
            + (f", {skipped} dosya atlandı (problem olarak okunamadı)." if skipped else "."),
            f"“{cluster}” · {appended} problems added"
            + (f", {skipped} files skipped (not readable as problems)." if skipped else ".")))
        self.refresh()

    # ---------- welcome ----------
    def welcome(self):
        """First run: do you know Go? Beginners get the rules tour + a free 9x9 game,
        players get 9x9 ranked placement. A page, not a window."""
        store.set_setting("welcome", True)

        def new():
            self.size_box.setCurrentText("9x9")
            self.bot_box.setCurrentText("20k")
            self.rules_tour(later=lambda: self.mode_select("game"))

        def knower():
            self.size_box.setCurrentText("9x9")
            self.mode_select("ranked")
        self.open_page(i18n.t("Hoş geldin", "Welcome"), Welcome(new, knower), 820)

    def rules_tour(self, later=None):
        def done_():
            self.close_page()
            if later:
                later()
        self.open_page(i18n.t("Kural turu", "Rules tour"), RulesTour(done_), 760)

    # ---------- quiz: replay your own mistakes ----------
    # The engine is NOT NEEDED. The best move and the alternatives are in the
    # jsonl, so both question and answer come from the record -- instantly, even
    # without KataGo.
    def quiz(self):
        if not self.archive:
            self.say_archive(PICK_GAME())
            return
        if self.questions and self.question_i + 1 < len(self.questions):
            self.question_i += 1
            self._question()
            return
        candidate = [r for r in self.archive.valid_records
                if r.get("points_lost", 0) >= self.QUIZ_THRESHOLD and r.get("best_move")]
        candidate.sort(key=lambda r: -r["points_lost"])
        self.questions = sorted(candidate[:self.QUIZ_COUNT], key=lambda r: r["move_no"])
        self.question_i = 0
        if not self.questions:
            self.say_archive(i18n.t(
                f"{store.display_name(self.archive.name)}{NL}{NL}{self.QUIZ_THRESHOLD:.0f} puandan "
                f"pahalı hamle yok — bu maçta sınanacak bir şey bulamadım.",
                f"{store.display_name(self.archive.name)}{NL}{NL}No move costs more than "
                f"{self.QUIZ_THRESHOLD:.0f} points — nothing to quiz you on in this game."))
            return
        self._question()

    def _question(self):
        r = self.questions[self.question_i]
        shown_no = goban.display_no(self.archive.moves, r["move_no"])
        self.archive_i, self.archive_branch = r["move_no"] - 1, None   # the board before the move
        self.canvas.overlays, self.canvas.zone = [], None
        self.refresh()
        self.say_archive(i18n.t(
            f"Soru {self.question_i + 1}/{len(self.questions)}{NL}{NL}"
            f"{shown_no}. hamle. Sıra sende — tahtaya tıkla.{NL}"
            f"(bu pozisyonda {r['points_lost']:.1f} puan kaybetmiştin)",
            f"Question {self.question_i + 1}/{len(self.questions)}{NL}{NL}"
            f"Move {shown_no}. Your turn — click the board.{NL}"
            f"(you lost {r['points_lost']:.1f} points in this position)"))

    def _quiz_answer(self, idx):
        r = self.questions[self.question_i]
        gtp = to_gtp(idx, self.archive.size)
        bottom = {a["move"]: a["diff"] for a in r.get("alternatives", [])}
        if gtp == r["best_move"]:
            start = i18n.t(f"✔ {gtp} — motorun ilk tercihi.",
                        f"✔ {gtp} — the engine's first choice.")
        elif gtp in bottom:
            start = i18n.t(f"◐ {gtp} — kabul edilebilir, en iyisinden {bottom[gtp]:.1f} puan geride.",
                        f"◐ {gtp} — acceptable, {bottom[gtp]:.1f} points behind the best.")
        elif gtp == r["move"]:
            start = i18n.t(f"✘ {gtp} — maçta oynadığın hamle bu. "
                        f"{r['points_lost']:.1f} puana mal olmuştu.",
                        f"✘ {gtp} — this is the move you played. "
                        f"It cost you {r['points_lost']:.1f} points.")
        else:
            start = i18n.t(f"? {gtp} — kayıtta bu hamlenin değerlendirmesi yok.",
                        f"? {gtp} — the record has no review for this move.")
        self.canvas.overlays = [(r["best_move"], C_BEST, "x", "")]
        self.refresh()
        row_ = [start, "", i18n.t(f"Doğrusu: {r['best_move']}",
                                f"The right move: {r['best_move']}")]
        row_ += [scoring.finding_text(b)
                  for b in scoring.findings_full(self.archive.moves, self.archive.size, r)]
        row_ += ["", i18n.t("sonraki soru için 'sına'ya bas", "press 'quiz' for the next question")
                  if self.question_i + 1 < len(self.questions) else i18n.t("Sınav bitti.", "Quiz over.")]
        self.say_archive(NL.join(row_))

    # ---------- archive navigation ----------
    def game_select(self):
        if self._muted:
            return
        s = self.game_list.currentRow()
        if s < 0 or s >= len(self.saved):
            return
        try:
            self.questions, self.question_i = [], 0     # game changed, drop the quiz
            self.archive = store.load_game(self.saved[s][0])
        except OSError as e:
            self.say_archive(i18n.t(f"Açılamadı: {e}", f"Could not open: {e}"))
            return
        # If there is an unfixed blunder the game opens BEFORE it: your turn.
        dz = self.archive.unfixed
        self.archive_branch = None
        self.archive_i = dz[0] - 1 if dz else len(self.archive.moves)
        self.show_move()

    def strip_select(self, branch, i):
        if self.archive:
            self.archive_branch, self.archive_i = branch, i
            self.show_move()

    def step_moves(self, d):
        if not self.archive:
            return
        h = self.archive.line_moves(self.archive_branch)
        self.archive_i = max(0, min(len(h), self.archive_i + d))
        self.archive_branch = self.archive.owner(self.archive_branch, self.archive_i)
        self.show_move()

    def blunder_git(self):
        if not self.archive:
            self.say_archive(PICK_GAME())
            return
        dz = self.archive.unfixed
        if not dz:
            self.say_archive(i18n.t("Bu maçta düzeltilmemiş blunder yok ✓",
                                 "No unfixed blunders in this game ✓"))
            return
        next_ = next((n for n in dz if n - 1 > self.archive_i), dz[0])   # then wrap around to the start
        self.archive_branch, self.archive_i = None, next_ - 1
        self.show_move()

    @staticmethod
    def _blunder_badge(n):
        return i18n.t(f"  🔴 {n} blunder", f"  🔴 {n} blunder{'s' * (n > 1)}") if n else ""

    def branch_play(self, idx):
        """A click on the board in an old game: a variation move in the color to play.
        The user plays both sides -- imagining the opponent."""
        m, branch, i = self.archive, self.archive_branch, self.archive_i
        h = m.line_moves(branch)
        col = goban.turn(h[:i])
        if not goban.replay(h, m.size, i).play(idx, goban.color(col)):
            self.status.setText(i18n.t("yasa dışı hamle", "illegal move"))
            return
        self.archive_branch, self.archive_i, new = m.branch_play(branch, i, [col, to_gtp(idx, m.size)])
        self.tick()
        if new:
            store.save_branch(m)
        self.show_move()

    def _fix_attempt(self, m, branch, i):
        """Is this node the first move played in place of a blunder?"""
        if branch is None:
            return False
        d = m.branches[branch]
        return (d["parent"] is None and i == d["root"] + 1
                and scoring.category(m.record_of(i)) == "blunder"
                and m.moves[i - 1][0] == m.color)

    def _grade_branch(self, m, branch, i):
        """Grades a fix attempt in the background. Independent of the phase machine
        (_background): the result is written to the branch even if the tab/game
        changes. One at a time."""
        if self._branch_job or not self.eng:
            return
        self._branch_job = (m.name, branch)
        moves_ = m.line_moves(branch)[:i]

        def done_(res):
            self._branch_job = None
            if isinstance(res, Exception):
                self.status.setText(i18n.t(f"puanlanamadı: {res}", f"grading failed: {res}"))
                return
            m.branches[branch]["records"].append({"move_no": i, **res})
            store.save_branch(m)
            if scoring.category(res) != "blunder":      # fixed: it enters mistake review
                store.add_review(f"{m.name}#{i}", self._today())
            if self.mode == "archive" and self.archive is m:
                row_ = self.game_list.currentRow()
                it = self.game_list.item(row_)
                if it:
                    it.setText(it.text().split("  🔴")[0]
                               + self._blunder_badge(len(m.unfixed)))
                self.show_move()
        self._background(lambda _step_progress: scoring.grade(
            self.eng, moves_, moves_[-1][0], self.my_rank, self.next_rank,
            size=m.size, komi=m.komi, rule=goban.sgf_rule(m.info)), None, done_)

    def _blunder_lines(self, m, branch, i):
        """Text of the blunder-fixing flow: a question at the fork, the result on an attempt."""
        dz = m.unfixed
        if branch is None and (i + 1) in dz:
            k = m.record_of(i + 1)
            shown_no = goban.display_no(m.moves, i + 1)
            color = i18n.t("siyah", "black") if m.color == "B" else i18n.t("beyaz", "white")
            return [i18n.t(f"🔴 BLUNDER — {shown_no}. hamlede {k['move']} oynadın, "
                          f"{k['points_lost']}p kaybettirdi.",
                          f"🔴 BLUNDER — move {shown_no}: {k['move']} lost {k['points_lost']}p."),
                    i18n.t(f"Sıra sende ({color}). Blunder olmayan bir hamle bul.",
                          f"Your turn ({color}). Find a move that is not a blunder."),
                    i18n.t(f"(bu maçta {len(dz)} düzeltilmemiş blunder)",
                          f"({len(dz)} unfixed blunders in this game)"), ""]
        if not self._fix_attempt(m, branch, i):
            return []
        k = m.record_of(i, branch)
        if not k:
            self._grade_branch(m, branch, i)
            return [i18n.t("⏳ deneme puanlanıyor…" if self.eng else
                          "motor hazır olunca puanlanacak — hamleye tekrar tıkla",
                          "⏳ grading your try…" if self.eng else
                          "will be graded when the engine is ready — click the move again"), ""]
        cat = scoring.category(k)
        if cat == "blunder":
            return [i18n.t(f"✗ Bu da blunder ({k['points_lost']}p). ← ile geri dön, "
                          "başka bir hamle dene.",
                          f"✗ Still a blunder ({k['points_lost']}p). Go back (←) and try another."), ""]
        t = [i18n.t(f"✓ Düzeltildi — {scoring.category_name(cat)} ({k['points_lost']}p). "
                   "Varyasyonu buradan sürdürebilirsin.",
                   f"✓ Fixed — {scoring.category_name(cat)} ({k['points_lost']}p). "
                   "You can continue the variation from here.")]
        if dz:
            shown_no = goban.display_no(m.moves, dz[0])
            t.append(i18n.t(f"Sıradaki blunder: {shown_no}. hamle ('🔴 sonraki blunder').",
                           f"Next blunder: move {shown_no} ('🔴 next blunder')."))
        return t + [""]

    def _category_table(self, m):
        """OGS style: count and share per category (HTML table). For an imported
        game, the opponent's too."""
        you, opponent = (scoring.category_count(m.valid_records),
                      scoring.category_count(m.opponent_records))
        ns, nr = sum(you.values()), sum(opponent.values())
        if not ns:
            return ""
        th = '<th align="right" style="padding:3px 10px">'
        h = ['<table style="margin-top:8px" cellspacing="0"><tr><th></th>'
             f'{th}{i18n.t("Sen", "You")}</th>{th}%</th>'
             + (f'{th}{i18n.t("Rakip", "Opponent")}</th>{th}%</th>' if nr else "") + "</tr>"]
        td = '<td align="right" style="padding:3px 10px">'
        for k in scoring.CATEGORIES:
            h.append(f'<tr><td style="padding:3px 10px 3px 0">'
                     f'<span style="color:{scoring.CATEGORY_COLOR[k]}">●</span> '
                     f'{html.escape(scoring.category_name(k))}</td>'
                     f'{td}{you[k]}</td>{td}%{100 * you[k] / ns:.1f}</td>'
                     + (f'{td}{opponent[k]}</td>{td}%{100 * opponent[k] / nr:.1f}</td>' if nr else "")
                     + "</tr>")
        return "".join(h) + "</table>"

    def show_move(self):
        """(archive_branch, archive_i) = how many moves played on which line."""
        if not self.archive:
            return
        m, branch, i = self.archive, self.archive_branch, self.archive_i
        h = m.line_moves(branch)
        self.canvas.overlays, self.canvas.zone = [], None
        t = self._blunder_lines(m, branch, i)
        if i == 0:
            top = goban.move_count(m.moves)
            self.say_archive(NL.join(t + [i18n.t(
                f"{store.display_name(m.name)}{NL}{top} hamle{NL}{NL}Başlangıç.",
                f"{store.display_name(m.name)}{NL}{top} moves{NL}{NL}Start.")]),
                           self._category_table(m))
            self.refresh()
            return
        col, mv = h[i - 1]
        k = m.record_of(i, branch)
        color_name = {"B": i18n.t("Siyah", "Black"), "W": i18n.t("Beyaz", "White"),
                   "AB": i18n.t("Siyah", "Black"), "AW": i18n.t("Beyaz", "White")}.get(col, col)
        shown_no = goban.display_no(m.moves, i)
        t.append((i18n.t(f"Kurulum {i} · ", f"Setup {i} · ") if col in goban.SETUP
                  else i18n.t(f"{shown_no}. hamle · ", f"Move {shown_no} · "))
                 + f"{color_name} {i18n.t('pas', 'pass') if mv == 'pass' else mv}"
                 + (i18n.t("   (varyasyon)", "   (variation)")
                                       if branch is not None else ""))
        if k:
            sv = k.get("level_estimate")
            # The answer to an unfixed blunder is not given: find it yourself first.
            hidden = branch is None and i in m.unfixed
            t.append(f"{scoring.category_name(scoring.category(k))} · "
                     + i18n.t(f"{k['points_lost']}p kayıp", f"{k['points_lost']}p lost")
                     + ("" if hidden else i18n.t(f" · en iyi {k['best_move']}",
                                               f" · best {k['best_move']}"))
                     + (f"  ~{sv['label']}" if sv else ""))
            for b in scoring.findings_full(h, m.size, k)[:2]:
                text_ = scoring.finding_text(b)
                if not (hidden and k["best_move"] in text_):   # "big point" gives the answer away
                    t.append("• " + text_)
            if not hidden:
                self.canvas.overlays = [(k["best_move"], C_BEST, "x", "")]
        elif branch is not None:
            t.append(i18n.t("(Varyasyon hamlesi)", "(Variation move)"))
        elif col in goban.SETUP:
            t.append(i18n.t("(Handikap taşı, hamle değil)", "(Handicap stone, not a move)"))
        elif col != m.color:
            t.append(i18n.t("(Rakip hamlesi, puanlanmaz)", "(Opponent's move, not graded)"))
        elif mv == "pass" and i == len(m.moves):
            t.append(i18n.t("(Oyunu bitiren pas, puanlanmaz)",
                           "(The pass that ended the game, not graded)"))
        else:
            t.append(i18n.t("(Bu hamle için kayıt yok)", "(No record for this move)"))
        if branch is None and i == len(m.moves):
            t += self._result_lines(m)
            self.say_archive(NL.join(t), self._category_table(m))
        else:
            self.say_archive(NL.join(t))
        self.refresh()

    def _result_lines(self, m):
        """The last position in the archive: how the game ended, what the score is based on, SP if ranked."""
        ms = m.result
        t = ["", i18n.t("Maç sonu", "Game result")]
        if m.mode == "sgf":
            # An imported game's result comes from its own server (RE[]). Our count is
            # from Black's view and with Chinese rules; it would mislead in a Japanese-rules game.
            you = i18n.t("siyah", "black") if m.color == "B" else i18n.t("beyaz", "white")
            t.append(i18n.t(f"{scoring.result_label(ms['won'])} · SGF: {ms['re']} (sen {you})",
                           f"{scoring.result_label(ms['won'])} · SGF: {ms['re']} (you {you})")
                     if "won" in ms else i18n.t("SGF'te sonuç yok.", "No result in SGF."))
            n = len(sgf_import.missing(m))
            if n and m.name == self._sgf_name:
                t.append(i18n.t(f"puanlanıyor — {n} hamle kaldı", f"grading — {n} moves left"))
            elif n:
                t.append(i18n.t(f"{n} hamle puanlanmadı — 'SGF içe aktar' kaldığı yerden sürdürür.",
                               f"{n} moves not graded — 'import SGF' resumes."))
            return t
        if ms.get("result") == "resign":
            t.append(i18n.t("Pes edildi.", "Resigned."))
        elif goban.game_over(m.moves):
            k = m.valid_records
            lead = k[-1].get("next_score_lead") if k else None
            s = self._archive_count(m)
            if s:
                sides = scoring.side_names(m.mode)
                t.append(scoring.result_sentence(*scoring.game_result(s, lead), sides))
                t += scoring.count_lines(s, lead, sides)
            else:
                t.append(i18n.t("Sayım yapılıyor…", "Counting…"))
        else:
            t.append(i18n.t("İki pasla bitmemiş: yarım kaldı ya da pes edildi (kayıt yok).",
                           "Did not end on two passes: unfinished or resigned (no record)."))
        if m.mode == "ranked":
            t += ["", i18n.t("Dereceli", "Ranked")]
            if ms.get("sp"):
                t.append(f"{scoring.result_label(ms['won'])} · "
                         + i18n.t(f"Rakip {ms['opponent']}", f"Opponent {ms['opponent']}"))
                t += _event_lines(ms["sp"])
            else:
                t.append(i18n.t("SP kaydı yok: maç başına SP kaydı tutulmadan önce "
                               "oynanmış ya da yarım kalmış.",
                               "No SP record: played before per-game SP was tracked, "
                               "or left unfinished."))
        return t

    def _archive_count(self, m):
        """Count of a finished game: cache -> engine (in the background) -> approximate.

        Not written to a file: it can be regenerated from the board at any
        time, and old and new games go down the same path. None = query
        running, redrawn when it arrives.
        """
        if "count" in m.meta:
            return m.meta["count"]
        if not self.eng:
            return scoring.final_count(None, m.moves, m.size, m.komi)
        m.meta["count"] = None                  # keep back-and-forth navigation from starting a second query

        def arrived(res):
            # A live game may continue in the background: the path that does not touch the phase (_background).
            m.meta["count"] = (scoring.final_count(None, m.moves, m.size, m.komi)
                               if isinstance(res, Exception) else res)
            if self.mode == "archive" and self.archive is m and self.archive_i == len(m.moves):
                self.show_move()
        self._background(lambda _step_progress: scoring.final_count(self.eng, m.moves, m.size, m.komi),
                   None, arrived)
        return None


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--rank", default="15k")
    ap.add_argument("--bot", default="15k")
    ap.add_argument("--next", default="9k")
    ap.add_argument("--plan", type=int, default=3)
    ap.add_argument("--size", type=int, choices=SIZES)
    ap.add_argument("--resume", help="resume an unfinished game: its name under games/, e.g. ranked_20261009-200836")
    a = ap.parse_args()
    if a.size:
        os.environ["GOMENTOR_SIZE"] = str(a.size)    # Config.env() reads it from here
    app_ = QApplication(sys.argv)
    window = App(a.rank, a.bot, a.next, a.plan)
    window.resume = a.resume
    window.show()
    if not store.setting("welcome"):
        QTimer.singleShot(400, window.welcome)
    sys.exit(app_.exec())
