#!/usr/bin/env python3
"""Game statistics charts -- QPainter, zero new dependencies.

matplotlib brings ~70 MB and its PyInstaller hook is a separate headache;
QtCharts wants its own DLL and would mean touching the plugin list in the
.spec file. The board is already drawn with QPainter -- so are the charts.
Package size did not change, the .spec file was not touched.

The layering holds: the MATH is not here. scoring.distribution / runs /
heatmap / cumulative_loss / line_distribution / level_series are pure
functions that produce the numbers; this file only turns them into pixels.
So the charts' correctness is tested in --selftest without Qt ever starting.
"""
import math

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QScrollArea, QSizePolicy,
                               QSplitter, QTabWidget, QVBoxLayout, QWidget)

import i18n
import goban
import scoring
import theme

# The empty state of the list below comes up in several places: keep the text in one.
CLICK = lambda: i18n.t("Bir grafiğe tıkla, o hamleler burada listelensin.",
                      "Click a chart and those moves get listed here.")

PAPER, GRID, TEXT, FAINT = "#ffffff", "#e4e0d8", "#1a1a1a", "#8b8579"
GOOD_R, BAD_R, BLUE, PURPLE = "#1f9d55", "#cc2b2b", "#1f6fd0", "#7b4fa8"


def _bucket_color(i):
    """0-9 bucket -> red to green. Color carries meaning in the histogram:
    tall bars on the left are bad, tall bars on the right are good."""
    t = i / 9.0
    return QColor(int(204 - 173 * t), int(43 + 114 * t), int(43 + 42 * t))


def _step(spacing, goal=6):
    """Rough but good enough axis step: from the 1/2/5 x 10^n family."""
    if spacing <= 0:
        return 1.0
    raw_ = spacing / goal
    exponent = 10 ** math.floor(math.log10(raw_))
    for k in (1, 2, 5, 10):
        if k * exponent >= raw_:
            return k * exponent
    return 10 * exponent


def _x_ticks(a, b, n=6):
    if b <= a:
        return [(a, f"{a:g}")]
    d = _step(b - a, n)
    out, v = [], d * math.floor(a / d)
    while v <= b:
        if v >= a:
            out.append((v, f"{v:g}"))
        v += d
    return out


class Panel(QWidget):
    """Shared axis/grid/scale part. Line and Column share it -- both would have
    needed the same margin and scale math, and writing it twice was longer."""

    LEFT, RIGHT, TOP, BOTTOM = 54, 16, 8, 30

    def __init__(self, height=160, on_click=None):
        super().__init__()
        self.on_click = on_click
        self.xlim, self.ylim = (0.0, 1.0), (0.0, 1.0)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(height)

    def _area(self):
        return (float(self.LEFT), float(self.TOP),
                max(1.0, self.width() - self.LEFT - self.RIGHT),
                max(1.0, self.height() - self.TOP - self.BOTTOM))

    def _px(self, v):
        x0, _, w, _ = self._area()
        a, b = self.xlim
        return x0 + (v - a) / ((b - a) or 1) * w

    def _py(self, v):
        _, y0, _, h = self._area()
        a, b = self.ylim
        return y0 + h - (v - a) / ((b - a) or 1) * h

    def _y_ticks(self, unit=""):
        lo, hi = self.ylim
        d = _step(hi - lo, 4)
        out, v = [], d * math.floor(lo / d)
        while v <= hi:
            if v >= lo:
                out.append((v, f"{v:g}{unit}"))
            v += d
        return out

    def _frame(self, p, y_mark=(), x_mark=()):
        t = theme.active()
        x0, y0, w, h = self._area()
        p.setFont(QFont("Segoe UI", 7))
        for v, label in y_mark:
            y = self._py(v)
            p.setPen(QPen(QColor(t["edge"])))
            p.drawLine(x0, y, x0 + w, y)
            p.setPen(QPen(QColor(t["faint"])))
            p.drawText(0.0, y - 7, self.LEFT - 6.0, 14.0,
                       Qt.AlignRight | Qt.AlignVCenter, label)
        for v, label in x_mark:
            p.setPen(QPen(QColor(t["faint"])))
            p.drawText(self._px(v) - 22, y0 + h + 4, 44.0, 14.0,
                       Qt.AlignCenter, label)
        p.setPen(QPen(QColor(t["faint"])))
        p.drawLine(x0, y0 + h, x0 + w, y0 + h)
        p.drawLine(x0, y0, x0, y0 + h)

    def _legend(self, p, groups):
        t = theme.active()
        x0, y0, _, _ = self._area()
        p.setFont(QFont("Segoe UI", 7))
        lx = x0 + 6
        for g in groups:
            if not g.get("name"):
                continue
            p.setBrush(QColor(g.get("color") or BLUE))
            p.setPen(Qt.NoPen)
            p.drawRect(lx, y0 + 3, 8.0, 8.0)
            p.setPen(QPen(QColor(t["ink"])))
            gen = 7.0 * len(g["name"]) + 8
            p.drawText(lx + 12, y0 + 2, gen, 11.0,
                       Qt.AlignLeft | Qt.AlignVCenter, g["name"])
            lx += gen + 16

    def _empty(self, p):
        p.setPen(QPen(QColor(theme.active()["faint"])))
        p.drawText(self.rect(), Qt.AlignCenter, i18n.t("veri yok", "no data"))


class Line(Panel):
    """Multi-series line chart. Optional zero line + a strip along the bottom.

    The strip is not a separate widget: it shares the same x axis, so drawing
    it in the same paintEvent is less code and has no alignment trouble.
    """

    def __init__(self, runs, zero=False, strip=(), y_unit="",
                 height=160, on_click=None):
        super().__init__(height, on_click)
        self.runs = [s for s in runs if s.get("data")]
        self.zero, self.strip, self.y_unit = zero, list(strip), y_unit
        self._scale()

    def _scale(self):
        xs = [x for s in self.runs for x, _ in s["data"]]
        ys = [y for s in self.runs for _, y in s["data"]]
        if not xs:
            return
        self.xlim = (min(xs), max(max(xs), min(xs) + 1))
        lo, hi = min(ys), max(ys)
        if self.zero:
            m = max(abs(lo), abs(hi), 1.0)
            lo, hi = -m, m
        margin = (hi - lo) * 0.12 or 1.0
        self.ylim = (lo - margin, hi + margin)

    def _nearest(self, px):
        """The x value (move number) of the data point closest to the clicked x."""
        en, best_d = None, 1e18
        for s in self.runs:
            for x, _ in s["data"]:
                d = abs(self._px(x) - px)
                if d < best_d:
                    en, best_d = x, d
        return en if best_d < 40 else None

    def mousePressEvent(self, ev):
        if not self.on_click:
            return
        n = self._nearest(ev.position().x())
        if n is not None:
            self.on_click(int(round(n)))

    def paintEvent(self, ev):
        t = theme.active()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(t["panel"]))
        if not self.runs:
            self._empty(p)
            return
        x0, y0, w, h = self._area()
        self._frame(p, self._y_ticks(self.y_unit),
                      _x_ticks(*self.xlim))

        if self.zero:
            y = self._py(0.0)
            p.setPen(QPen(QColor(t["faint"]), 1, Qt.DashLine))
            p.drawLine(x0, y, x0 + w, y)

        # Strip: the ranges where the engine kept insisting on the same move, along the bottom edge.
        for start, end, color, label in self.strip:
            xa, xb = self._px(start), self._px(end)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(color))
            p.drawRect(xa, y0 + h - 7, max(2.0, xb - xa), 7.0)
            if label and xb - xa > 26:
                p.setPen(QPen(QColor("#ffffff")))
                p.setFont(QFont("Segoe UI", 6, QFont.Bold))
                p.drawText(xa, y0 + h - 7, xb - xa, 7.0, Qt.AlignCenter, label)

        for s in self.runs:
            data = s["data"]
            color = QColor(s.get("color") or BLUE)
            if s.get("fill"):
                # Above/below zero in different colors: "who is ahead" reads without reading.
                floor = self._py(0.0) if self.zero else y0 + h
                p.setPen(Qt.NoPen)
                for (xa, ya), (xb, yb) in zip(data, data[1:]):
                    c = QColor(GOOD_R if (ya + yb) / 2 >= 0 else BAD_R)
                    c.setAlpha(48)
                    p.setBrush(c)
                    p.drawPolygon(QPolygonF([
                        QPointF(self._px(xa), floor),
                        QPointF(self._px(xa), self._py(ya)),
                        QPointF(self._px(xb), self._py(yb)),
                        QPointF(self._px(xb), floor)]))
            p.setPen(QPen(color, 2))
            p.setBrush(Qt.NoBrush)
            for (xa, ya), (xb, yb) in zip(data, data[1:]):
                p.drawLine(self._px(xa), self._py(ya), self._px(xb), self._py(yb))
            if len(data) < 90:
                p.setBrush(color)
                p.setPen(Qt.NoPen)
                for x, y in data:
                    p.drawEllipse(QPointF(self._px(x), self._py(y)), 2.0, 2.0)

        self._legend(p, self.runs)


class Column(Panel):
    """Histogram and grouped bars. The clicked bar stays selected and is reported."""

    def __init__(self, labels, groups, height=170, on_click=None,
                 y_unit=""):
        super().__init__(height, on_click)
        self.labels = list(labels)
        self.groups = [g for g in groups if g.get("data")]
        self.selected = None
        self.y_unit = y_unit
        en = max([max(g["data"]) for g in self.groups] or [1])
        self.ylim = (0.0, (en or 1) * 1.18)
        self.xlim = (0.0, float(max(1, len(self.labels))))

    def _bucket(self, px):
        x0, _, w, _ = self._area()
        if not self.labels or not (x0 <= px <= x0 + w):
            return None
        i = int((px - x0) / w * len(self.labels))
        return min(len(self.labels) - 1, max(0, i))

    def mousePressEvent(self, ev):
        i = self._bucket(ev.position().x())
        if i is None:
            return
        self.selected = None if self.selected == i else i
        self.update()
        if self.on_click:
            self.on_click(self.selected)

    def paintEvent(self, ev):
        t = theme.active()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(t["panel"]))
        if not self.labels or not self.groups:
            self._empty(p)
            return
        x0, y0, w, h = self._area()
        self._frame(p, self._y_ticks(self.y_unit))

        n, g = len(self.labels), len(self.groups)
        bucket = w / n
        largest = max((len(e) for e in self.labels), default=1)
        skip = max(1, math.ceil(largest * 6.0 / bucket))
        p.setFont(QFont("Segoe UI", 7))
        for i, label in enumerate(self.labels):
            xa = x0 + i * bucket
            if self.selected == i:
                c = QColor(BLUE)
                c.setAlpha(26)
                p.fillRect(xa, y0, bucket, h, c)
            gen = bucket * 0.72 / g
            empty = bucket * 0.14
            for j, group_ in enumerate(self.groups):
                value = group_["data"][i] if i < len(group_["data"]) else 0
                yy = self._py(value)
                p.setPen(Qt.NoPen)
                colors = group_.get("colors")
                p.setBrush(QColor(colors[i] if colors else group_.get("color") or _bucket_color(i)))
                p.drawRect(xa + empty + j * gen, yy, gen, y0 + h - yy)
                if value and bucket >= 22:        # numbers are unreadable on dense bars, skip them
                    p.setPen(QPen(QColor(t["ink"])))
                    p.drawText(xa + empty + j * gen - 6, yy - 13, gen + 12, 12.0,
                               Qt.AlignCenter, f"{value:g}")
            # Once many games pile up the labels overlap: skip the ones that do not fit.
            if label and (i % skip == 0 or self.selected == i):
                p.setPen(QPen(QColor(t["ink"] if self.selected == i else t["faint"])))
                p.drawText(xa - 8, y0 + h + 4, bucket + 16, 14.0,
                           Qt.AlignCenter, label)

        self._legend(p, [g for g in self.groups if g.get("name")])


class Heat(QWidget):
    """9x9 loss heatmap. The GEOGRAPHY of loss -- where you give points away.

    BoardView is not reused: that widget is for live play (hover ghost stone,
    clicks, overlay list) and all this needs is one color per point. A separate
    45 lines is cheaper than a fourth mode in BoardView.
    """

    def __init__(self, map_, size=9, on_click=None):
        super().__init__()
        self.map_, self.size, self.on_click = map_, size, on_click
        self.en = max([v["lost"] for v in map_.values()] or [1.0]) or 1.0
        self.setMinimumSize(300, 300)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def _geo(self):
        beside = min(self.width(), self.height())
        h = beside / (self.size + 1)
        return h, (self.width() - h * (self.size - 1)) / 2, \
            (self.height() - h * (self.size - 1)) / 2

    def mousePressEvent(self, ev):
        if not self.on_click:
            return
        h, ox, oy = self._geo()
        c = round((ev.position().x() - ox) / h)
        r = round((ev.position().y() - oy) / h)
        if 0 <= r < self.size and 0 <= c < self.size:
            self.on_click(int(r) * self.size + int(c))

    def paintEvent(self, ev):
        tm = theme.active()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(tm["board"]))
        h, ox, oy = self._geo()
        end = ox + h * (self.size - 1)
        p.setPen(QPen(QColor(tm["grid"])))
        for k in range(self.size):
            p.drawLine(ox, oy + k * h, end, oy + k * h)
            p.drawLine(ox + k * h, oy, ox + k * h, end)
        p.setFont(QFont("Segoe UI", max(6, int(h * .22))))
        letter = goban.cols(self.size)
        p.setPen(QPen(QColor(tm["grid"])))
        for k in range(self.size):
            p.drawText(ox + k * h - h * .3, oy - h * .58, h * .6, h * .5,
                       Qt.AlignCenter, letter[k])
            p.drawText(ox - h * .95, oy + k * h - h * .25, h * .6, h * .5,
                       Qt.AlignCenter, str(self.size - k))
        r = h * .42
        for i, v in self.map_.items():
            if not 0 <= i < self.size * self.size:
                continue
            t = min(1.0, v["lost"] / self.en)
            x, y = ox + (i % self.size) * h, oy + (i // self.size) * h
            c = QColor(BAD_R)
            c.setAlpha(int(45 + 195 * t))
            p.setBrush(c)
            p.setPen(QPen(QColor(tm["grid"])))
            p.drawEllipse(QPointF(x, y), r, r)
            if v["lost"] >= self.en * .28:
                p.setPen(QPen(QColor("#ffffff")))
                p.setFont(QFont("Segoe UI", max(6, int(h * .2)), QFont.Bold))
                p.drawText(x - h * .5, y - h * .3, h, h * .6,
                           Qt.AlignCenter, f"{v['lost']:.0f}")


# ---------- window ----------
def _heading(text_, bottom=""):
    k = QWidget()
    d = QVBoxLayout(k)
    d.setContentsMargins(2, 12, 2, 3)
    d.setSpacing(1)
    b = QLabel(text_)
    b.setFont(QFont("Segoe UI", 10, QFont.Bold))
    d.addWidget(b)
    if bottom:
        a = QLabel(bottom, objectName="faint")
        a.setFont(QFont("Segoe UI", 8))
        a.setWordWrap(True)
        d.addWidget(a)
    return k


def _day(name):
    """'ranked_20260909-183402' -> '09-09'. Returned unchanged if the name pattern does not match."""
    end = name.split("_")[-1]
    date = end.split("-")[0]
    return f"{date[4:6]}-{date[6:8]}" if len(date) == 8 and date.isdigit() else end


def _scroll(inner):
    s = QScrollArea()
    s.setWidgetResizable(True)
    s.setWidget(inner)
    s.setFrameShape(QScrollArea.NoFrame)
    return s


class ChartView(QWidget):
    """Two tabs: this game / all games. Every chart is clickable; a click fills
    the list below, and clicking a row in the list takes the archive to that
    move. So a chart is not a picture, it is a way to navigate.
    """

    def __init__(self, parent_, game, skip=None, all_games=()):
        super().__init__(parent_)
        self.game, self.skip = game, skip
        self.records = game.valid_records
        self._buckets = scoring.distribution(self.records)
        self._map = {}

        plenty = QSplitter(Qt.Vertical)
        tab_ = QTabWidget()
        tab_.tabBar().setFocusPolicy(Qt.NoFocus)
        tab_.addTab(self._games_tab(), i18n.t("Bu maç", "This game"))
        tab_.addTab(self._trend_tab(all_games), i18n.t("Tüm maçlar", "All games"))
        plenty.addWidget(tab_)
        self.list_ = QListWidget()
        self.list_.itemClicked.connect(self._row)
        plenty.addWidget(self.list_)
        plenty.setStretchFactor(0, 1)
        plenty.setSizes([580, 180])

        d = QVBoxLayout(self)
        d.setContentsMargins(6, 6, 6, 6)
        d.addWidget(plenty)
        self._fill(CLICK(), [])

    # ---------- list below ----------
    def _fill(self, title, records):
        self.list_.clear()
        self.list_.addItem(title)
        for r in records:
            no = r.get("move_no", 0)
            shown_no = goban.display_no(self.game.moves, no)
            it = QListWidgetItem(
                f"  {shown_no:3}. {r.get('move', '?'):4} "
                f"{scoring.category_name(scoring.category(r)):11} "
                f"-{r.get('points_lost', 0):5.1f}p  "
                + i18n.t("en iyi ", "best ") + f"{r.get('best_move', '?'):4}  "
                + i18n.t("skor ", "score ") + f"{r.get('prev_score_lead', 0):+5.1f}"
                f" -> {r.get('next_score_lead', 0):+5.1f}")
            it.setData(Qt.UserRole, no)
            self.list_.addItem(it)

    def _row(self, it):
        no = it.data(Qt.UserRole)
        if no and self.skip:
            self.skip(int(no))

    def _to_move(self, no):
        r = next((k for k in self.records if k.get("move_no") == no), None)
        shown_no = goban.display_no(self.game.moves, no)
        self._fill(i18n.t(f"{shown_no}. hamle", f"Move {shown_no}") if r
                     else i18n.t(f"{shown_no}. hamle: kayıt yok", f"Move {shown_no}: no record"),
                     [r] if r else [])
        if self.skip:
            self.skip(no)

    def _bucket_selected(self, i):
        if i is None:
            self._fill(CLICK(), [])
        else:
            self._fill(f"{scoring.category_name(scoring.CATEGORIES[i])}: "
                         + i18n.t(f"{len(self._buckets[i])} hamle",
                                 f"{len(self._buckets[i])} moves"), self._buckets[i])

    def _point(self, idx):
        v = self._map.get(idx)
        name = goban.to_gtp(idx, self.game.size)
        self._fill(i18n.t(f"{name} — toplam -{v['lost']:.1f}p ({len(v['records'])} hamle)",
                           f"{name} — total -{v['lost']:.1f}p ({len(v['records'])} moves)")
                     if v else i18n.t(f"{name} — burada oynamamışsın",
                                     f"{name} — you never played here"),
                     v["records"] if v else [])

    # ---------- tab 1: this game ----------
    def _games_tab(self):
        inner = QWidget()
        d = QVBoxLayout(inner)
        d.setContentsMargins(10, 4, 10, 12)
        k = self.records
        if not k:
            d.addWidget(QLabel(i18n.t("Bu maçta değerlendirilmiş hamle yok "
                                     "(puanlama kaydı tutulmamış).",
                                     "No graded moves in this game "
                                     "(no scoring was recorded).")))
            d.addStretch(1)
            return _scroll(inner)
        ordered = sorted(k, key=lambda r: r.get("move_no", 0))

        # 1 + A + B: score curve, big-point strip -- same x axis
        score = []
        for r in ordered:
            no = r.get("move_no", 0)
            score.append((no - 1, r.get("prev_score_lead", 0.0)))
            score.append((no, r.get("next_score_lead", 0.0)))
        sr = scoring.runs(k)
        strip = [(s["start"], s["end"], BAD_R, f"{s['move']} x{s['n']}") for s in sr]

        d.addWidget(_heading(
            i18n.t("Anlık puan (senin gözünden)", "Live score (from your side)"),
            i18n.t("Sıfırın üstü sen öndesin. Alttaki kırmızı şerit: motorun aynı hamleyi "
                  "üst üste istediği ama senin oynamadığın aralıklar. Grafiğe tıkla, "
                  "tahta o hamleye gitsin.",
                  "Above zero you are ahead. The red band below marks the stretches where "
                  "the engine kept asking for the same move and you played elsewhere. "
                  "Click the chart and the board jumps to that move.")))
        d.addWidget(Line([{"name": i18n.t("Skor farkı", "Score gap"), "color": BLUE, "data": score,
                            "fill": True}],
                          zero=True, strip=strip, y_unit="p", height=195,
                          on_click=self._to_move))

        d.addWidget(_heading(
            i18n.t("Kümülatif kayıp", "Cumulative loss"),
            i18n.t("Anlık skor iki oyuncunun hatasını birden taşır; bu eğri yalnız seninkini. "
                  "Düz giden bölge iyi oyun, dikeldiği yer maçın öldüğü yer.",
                  "The live score carries both players' mistakes; this curve carries only "
                  "yours. A flat stretch is good play; where it rears up is where the game "
                  "died.")))
        d.addWidget(Line([{"name": i18n.t("Toplam kayıp", "Total loss"), "color": BAD_R,
                            "data": scoring.cumulative_loss(k)}],
                          y_unit="p", height=155, on_click=self._to_move))

        # 2: loss move by move, in category color (the 0-10 grade was removed)
        self._ordered = ordered
        d.addWidget(_heading(
            i18n.t("Hamle hamle kayıp", "Loss move by move"),
            i18n.t("Her çubuk bir hamlen: yüksekliği kaybettiği puan, rengi kategorisi "
                  "(mavi Mükemmel … kırmızı Blunder). Çubuğa tıkla, tahta o hamleye gitsin.",
                  "Each bar is one of your moves: its height is the points it lost, its "
                  "colour the category (blue Perfect … red Blunder). Click a bar and the "
                  "board jumps to that move.")))
        d.addWidget(Column([str(goban.display_no(self.game.moves, r.get("move_no", 1)))
                           for r in ordered],
                          [{"data": [r.get("points_lost", 0.0) for r in ordered],
                            "colors": [scoring.CATEGORY_COLOR.get(scoring.category(r), FAINT)
                                        for r in ordered]}],
                          height=175, y_unit="p",
                          on_click=lambda i: i is not None and self._to_move(
                              self._ordered[i].get("move_no", 0))))

        # E: level strip
        sv = [(no, dg) for no, _, dg in scoring.level_series(k) if dg is not None]
        if sv:
            d.addWidget(_heading(
                i18n.t("Hamle seviyesi", "Move level"),
                i18n.t(f"Hamle hamle seviye tahmini — {len(sv)}/{len(k)} hamlede üretilebildi; "
                      "gerisinde pozisyon seviyeleri ayırmıyor, o hamleler çizilmedi "
                      "(boşluğu doldurmak yalan olurdu). Yukarı = güçlü. Tepelerin "
                      "dağınıklığı ortalamandan daha çok şey söyler.",
                      f"A level estimate per move — produced on {len(sv)}/{len(k)} moves; "
                      "on the rest the position does not separate levels, so those moves "
                      "are not drawn (filling the gap would be a lie). Up = stronger. How "
                      "scattered the peaks are tells you more than your average.")))
            d.addWidget(Line([{"name": i18n.t("Seviye", "Level"), "color": PURPLE, "data": sv}],
                              height=145, on_click=self._to_move))

        # 3: distribution histogram -- clickable
        d.addWidget(_heading(
            i18n.t("Kategori dağılımı: hangi hamleler?",
                  "Category distribution: which moves?"),
            i18n.t("Her kategoride kaç hamle var. Bir çubuğa tıkla, o hamleler aşağıda "
                  "listelensin; listeden bir satıra tıkla, tahta oraya gitsin.",
                  "How many moves fall in each category. Click a bar to list those moves "
                  "below; click a row in the list and the board jumps there.")))
        d.addWidget(Column([scoring.category_name(k) for k in scoring.CATEGORIES],
                          [{"data": [len(x) for x in self._buckets],
                            "colors": [scoring.CATEGORY_COLOR[k] for k in scoring.CATEGORIES]}],
                          height=190, on_click=self._bucket_selected))

        # D: line (distance from edge) distribution
        me, engine_ = scoring.line_distribution(k, self.game.size)
        lines = sorted(set(me) | set(engine_))
        if lines:
            d.addWidget(_heading(
                i18n.t("Hat dağılımı — sen vs motor", "Line distribution — you vs engine"),
                i18n.t("Kenardan uzaklık. 1. hat orta oyunda pas ile eşdeğerdir; "
                      "motordan belirgin fazla 1. hat oynuyorsan orada puan bırakıyorsun.",
                      "Distance from the edge. In the middle game the 1st line is worth "
                      "about as much as a pass; if you play it far more than the engine "
                      "does, you are leaving points there.")))
            d.addWidget(Column([i18n.t(f"{h}. hat", f"line {h}") for h in lines],
                              [{"name": i18n.t("Sen", "You"), "color": BLUE,
                                "data": [me.get(h, 0) for h in lines]},
                               {"name": i18n.t("Motor", "Engine"), "color": FAINT,
                                "data": [engine_.get(h, 0) for h in lines]}],
                              height=180))

        # B: the strip in words -- not clickable, for reading
        if sr:
            d.addWidget(_heading(
                i18n.t("Görmezden gelinen büyük noktalar", "Big points you ignored"),
                i18n.t("Motorun üst üste 3+ kez istediği ama oynamadığın hamleler. "
                      "Boş nokta tahtadan kaçmıyor — sen o tarafa bakmıyorsun.",
                      "Moves the engine asked for 3+ times in a row and you never played. "
                      "An empty point does not run off the board — you are not looking "
                      "that way.")))
            for s in sorted(sr, key=lambda s: -s["lost"]):
                et = QLabel(f"&nbsp;&nbsp;<b>{s['move']}</b> · "
                            + i18n.t(f"{s['n']} hamle boyunca", f"over {s['n']} moves")
                            + f" (h{s['start']}–h{s['end']}) · <b>-{s['lost']:.0f}p</b>")
                et.setFont(QFont("Segoe UI", 9))
                d.addWidget(et)

        # C: heatmap
        self._map = scoring.heatmap(k, self.game.size)
        if self._map:
            d.addWidget(_heading(
                i18n.t("Kayıp ısı haritası", "Loss heat map"),
                i18n.t("Oynadığın noktalar, orada kaybettiğin puan kadar koyu. "
                      "Bir noktaya tıkla, o noktadaki hamlelerin listelensin.",
                      "The points you played, shaded by how much you lost there. "
                      "Click a point to list your moves on it.")))
            heat = Heat(self._map, self.game.size, on_click=self._point)
            heat.setMinimumHeight(380)
            d.addWidget(heat)
        d.addStretch(1)
        return _scroll(inner)

    # ---------- tab 2: all games ----------
    def _trend_tab(self, all_games):
        return trend_panel(all_games)


def trend_panel(all_games):
    """All-games trend: the window's tab and Analysis > Progress are the same panel."""
    inner = QWidget()
    d = QVBoxLayout(inner)
    d.setContentsMargins(10, 4, 10, 12)
    t = scoring.game_trend(all_games)
    if not t:
        d.addWidget(QLabel(i18n.t("Trend için değerlendirilmiş hamlesi olan maç yok.",
                                 "No game with graded moves to build a trend from.")))
        d.addStretch(1)
        return _scroll(inner)
    x = list(range(1, len(t) + 1))
    d.addWidget(_heading(
        i18n.t(f"Maç başına ortalama kayıp — {len(t)} maç, eskiden yeniye",
              f"Average loss per game — {len(t)} games, oldest to newest"),
        i18n.t("Tek maç 'bugün ne oldu'yu söyler, örgü 'alışkanlık ne'yi. "
              "Bu grafiğin cevapladığı soru üçüncüsü: gelişiyor muyum? "
              "Aşağı inen eğri iyi.",
              "A single game tells you what happened today; the pattern report tells you "
              "what your habit is. This chart answers a third question: am I improving? "
              "A falling curve is good.")))
    d.addWidget(Line([{"name": i18n.t("Ortalama kayıp/hamle", "Average loss/move"),
                        "color": BAD_R,
                        "data": list(zip(x, [o["mean_loss"] for o in t]))}],
                      y_unit="p", height=175))

    d.addWidget(_heading(
        i18n.t("Maç başına hamle kalitesi", "Move quality per game"),
        i18n.t("Yeşil: Mükemmel + Harika + İyi hamlelerin oranı (yukarı iyi). "
              "Kırmızı: blunder oranı (aşağı iyi).",
              "Green: the share of Perfect + Great + Good moves (up is good). "
              "Red: the blunder rate (down is good).")))
    d.addWidget(Line([{"name": i18n.t("İyi hamle", "Good move"), "color": GOOD_R,
                        "data": list(zip(x, [o["good_ratio"] for o in t]))},
                       {"name": "Blunder", "color": BAD_R,
                        "data": list(zip(x, [o["blunder_ratio"] for o in t]))}],
                      y_unit="%", height=165))

    d.addWidget(_heading(
        i18n.t("Maç sonucu (senin gözünden)", "Game result (from your side)"),
        i18n.t("Son 5 kaydın medyanı — tahta dolunca tek kayıt yanıltıyor, "
              "brief de aynı ihtiyatı kullanıyor.",
              "The median of the last 5 records — once the board fills up a single record "
              "misleads, and the brief takes the same precaution.")))
    d.addWidget(Line([{"name": i18n.t("Sonuç", "Result"), "color": BLUE, "fill": True,
                        "data": list(zip(x, [o["end"] for o in t]))}],
                      zero=True, y_unit="p", height=175))

    d.addWidget(_heading(
        i18n.t("Maç başına ağır hata (≥3p) sayısı",
              "Serious mistakes (≥3p) per game"),
        i18n.t("Ortalama değil sayı: maçı kaybettiren şey ortalaman değil, "
              "bu çubuğun yüksekliği.",
              "A count, not an average: what loses you the game is not your average, "
              "it is the height of this bar.")))
    d.addWidget(Column([_day(o["name"]) for o in t],
                      [{"name": i18n.t("Ağır hata", "Serious mistake"), "color": BAD_R,
                        "data": [o["heavy"] for o in t]}],
                      height=180))
    d.addStretch(1)
    return _scroll(inner)
