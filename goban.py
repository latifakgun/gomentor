#!/usr/bin/env python3
"""Go rules and coordinates. PURE layer.

No engine, no files, no UI, no network. This module runs identically
everywhere: desktop, a FastAPI worker, Jupyter, tests. If this ever moves to
the web, this is the code that does not change.

Board size is a parameter -- not a module constant. Asking for 19x19 changes
one place.
"""
import re

import i18n

DEFAULT_SIZE = 9
# Chinese-rules standard. A half point makes a draw impossible. Old games were
# played with 7.0; KM[] in the SGF is read, so they are counted with their own komi.
KOMI = 7.5
LETTERS = "ABCDEFGHJKLMNOPQRST"      # no I, Go tradition


def cols(size=DEFAULT_SIZE):
    return LETTERS[:size]


def to_gtp(i, size=DEFAULT_SIZE):
    r, c = divmod(i, size)
    return f"{LETTERS[c]}{size - r}"


def from_gtp(s, size=DEFAULT_SIZE):
    """GTP coordinate -> index. None for 'pass'. ValueError if invalid."""
    s = s.strip().upper()
    if s in ("PASS", "P", ""):
        return None
    if s[0] not in LETTERS[:size]:
        raise ValueError(s)
    n = int(s[1:])
    if not 1 <= n <= size:
        raise ValueError(s)
    return (size - n) * size + LETTERS.index(s[0])


def to_sgf(i, size=DEFAULT_SIZE):
    r, c = divmod(i, size)
    return chr(97 + c) + chr(97 + r)


def star_points(size):
    """Star (hoshi) points -- a list of indices.

    Four corners + tengen on 9x9 and 13x13, nine points on 19x19. The edge
    margin is the 3rd line on 9x9 and the 4th on larger boards -- the standard
    layout.
    """
    if size < 9:
        return []
    m = 2 if size < 13 else 3                      # distance from the edge, 0-based
    edge = (m, size - 1 - m)
    corner = [r * size + c for r in edge for c in edge]
    if size % 2 == 0:                              # no tengen on an even board
        return corner
    o = (size - 1) // 2
    if size < 19:
        return corner + [o * size + o]
    line_ = (m, o, size - 1 - m)
    return [r * size + c for r in line_ for c in line_]


class Board:
    """Board state + rules engine. Entirely in memory, no side effects."""

    def __init__(self, size=DEFAULT_SIZE):
        self.size = size
        self.g = [0] * (size * size)
        self.ko = None
        self.last = None

    def copy(self):
        b = Board(self.size)
        b.g, b.ko, b.last = list(self.g), self.ko, self.last
        return b

    def nbrs(self, i):
        s = self.size
        r, c = divmod(i, s)
        for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            nr, nc = r + dr, c + dc
            if 0 <= nr < s and 0 <= nc < s:
                yield nr * s + nc

    def group(self, i):
        """(stones in the group, the group's liberties)"""
        col, seen, st, libs = self.g[i], {i}, [i], set()
        while st:
            for n in self.nbrs(st.pop()):
                if self.g[n] == 0:
                    libs.add(n)
                elif self.g[n] == col and n not in seen:
                    seen.add(n)
                    st.append(n)
        return seen, libs

    def play(self, i, col):
        """Plays and returns True if legal; if illegal, False WITHOUT touching the board."""
        if i is None:
            self.ko = None
            self.last = None
            return True
        if self.g[i] != 0 or i == self.ko:
            return False
        self.g[i] = col
        caught = set()
        for n in self.nbrs(i):
            if self.g[n] == -col:
                st, libs = self.group(n)
                if not libs:
                    caught |= st
        for x in caught:
            self.g[x] = 0
        st, libs = self.group(i)
        if not libs:                                   # suicide -> undo
            self.g[i] = 0
            for x in caught:
                self.g[x] = -col
            return False
        # ponytail: simple ko. No superko (position repetition) -- rare on 9x9.
        # If needed: keep a hash history of played positions and check it here.
        self.ko = (next(iter(caught)) if len(caught) == 1 and len(st) == 1
                   and len(libs) == 1 else None)
        self.last = i
        return True

    def ascii(self):
        c = cols(self.size)
        head = "   " + " ".join(c)
        row_ = [head]
        for r in range(self.size):
            row = []
            for x in range(self.size):
                i = r * self.size + x
                ch = {0: ".", 1: "X", -1: "O"}[self.g[i]]
                if i == self.last:
                    ch = {1: "#", -1: "@"}[self.g[i]]
                row.append(ch)
            row_.append(f"{self.size - r:2} " + " ".join(row) + f" {self.size - r}")
        row_.append(head)
        return "\n".join(row_)


def color(col):
    """'B'/'AB' -> 1, 'W'/'AW' -> -1."""
    return 1 if col[-1] == "B" else -1


# Setup stones (handicap): ["AB", "D4"] rows at the START of the move list.
# The SGF itself works this way: AB/AW are not moves, they are stones put on
# the board. Kept in the list, nobody outside replay() and Engine.query() needs
# to know. The internal `move_no` counts the setup rows too (compatible with
# the jsonl and the review cards); the number shown to the user is shifted by
# `display_no()`.
SETUP = ("AB", "AW")


def setup_count(moves):
    return next((i for i, (col, _) in enumerate(moves) if col not in SETUP), len(moves))


def move_count(moves):
    """Number of real moves played, setup stones (AB/AW) excluded."""
    return len(moves) - setup_count(moves)


def display_no(moves, move_no):
    """Turns the internal 1-based index into the move number shown to the user."""
    return max(1, int(move_no) - setup_count(moves))


def turn(moves):
    """Color to move ('B'/'W'). With no moves: if there are black setup stones
    it is handicap -- White starts."""
    for col, _ in reversed(moves):
        if col not in SETUP:
            return "W" if col == "B" else "B"
    return "W" if any(col == "AB" for col, _ in moves) else "B"


def replay(moves, size=DEFAULT_SIZE, upto=None):
    """Builds the board from the move list.

    upto: apply only the first N moves. Undo, lesson mode and the whole
    "step through old games" feature are this one parameter.
    """
    b = Board(size)
    for col, mv in moves[:upto]:
        if col in SETUP:
            b.g[from_gtp(mv, size)] = color(col)      # a stone is placed, not played: no ko/captures
        else:
            b.play(None if mv == "pass" else from_gtp(mv, size), color(col))
    return b


# ---------- SGF ----------
# Reading with regular expressions broke on real files: ";B[dd]" inside a
# comment counted as a move, and "\]" closed the comment early. Hence a
# character-by-character scanner.

def sgf_nodes(text):
    """Nodes of the main line: [{property: [value, ...]}, ...].

    Main line = the FIRST subtree at every fork. The first ')' ends the main
    line: everything after it is a sibling variation. In a collection the
    first game is read.
    """
    nodes, name, name_done, i, n = [], "", True, 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "[":
            j, value = i + 1, []
            while j < n and text[j] != "]":
                if text[j] == "\\":
                    j += 1                   # escape: next character as is
                if j < n:
                    value.append(text[j])
                j += 1
            if nodes and name:
                nodes[-1].setdefault(name, []).append("".join(value))
            name_done, i = True, j + 1
            continue
        if ch == ";":
            nodes.append({})
            name, name_done = "", True
        elif ch == ")" and nodes:
            break
        elif "A" <= ch <= "Z":
            name, name_done = (ch, False) if name_done else (name + ch, False)
        i += 1
    return nodes


def _sgf_point(v, size):
    """'dd' -> GTP. Empty or 'tt' (old way of writing pass) -> 'pass'."""
    if v == "" or (v == "tt" and size <= 19):
        return "pass"
    if len(v) != 2:
        raise ValueError(i18n.t(f"SGF koordinati okunamadı: [{v}]", f"Could not read SGF coordinate: [{v}]"))
    c, r = ord(v[0]) - 97, ord(v[1]) - 97
    if not (0 <= c < size and 0 <= r < size):
        raise ValueError(i18n.t(f"SGF koordinati tahta dışında: [{v}]", f"SGF coordinate is off the board: [{v}]"))
    return to_gtp(r * size + c, size)


# Game info kept verbatim from the root node.
SGF_INFO = ("PB", "PW", "BR", "WR", "RE", "DT", "EV", "PC", "RU")

RULE_MAP = {
    "japanese": "japanese", "jp": "japanese", "nihon": "japanese",
    "korean": "korean", "kr": "korean",
    "aga": "aga", "tromp-taylor": "tromp-taylor", "nz": "nz",
    "chinese": "chinese", "cn": "chinese",
}


def sgf_rule(info=None):
    """Turns the SGF RU[] value into a KataGo rules name (default 'chinese')."""
    ru = ((info or {}).get("RU") if isinstance(info, dict) else info) or ""
    return RULE_MAP.get(str(ru).strip().lower(), "chinese")


def sgf_read(text):
    """{'size', 'komi', 'moves', 'info', 'you'}. ValueError if invalid.

    moves starts with the setup stones (AB/AW). `SN` is our own property: the
    user's color (black if missing -- always black in our own games).
    """
    nodes = sgf_nodes(text)
    if not nodes:
        raise ValueError(i18n.t("SGF'te düğüm yok", "The SGF has no nodes"))
    root_ = nodes[0]
    first_ = lambda k, v: (root_.get(k) or [v])[0].strip()
    try:
        size = int(first_("SZ", "19").split(":")[0])     # spec: 19 if SZ is missing
        komi = float(first_("KM", str(KOMI)) or KOMI)
    except ValueError:
        raise ValueError(i18n.t("SGF'te SZ/KM okunamadı", "Could not read SZ/KM in the SGF"))
    if not 2 <= size <= len(LETTERS):
        raise ValueError(i18n.t(f"tahta boyu desteklenmiyor: {size}", f"board size not supported: {size}"))
    moves = []
    for d in nodes:
        for col in SETUP:
            for v in d.get(col, []):
                if any(c not in SETUP for c, _ in moves):
                    raise ValueError(i18n.t("oyun ortasında taş kurulumu (AB/AW) desteklenmiyor",
                                         "stone setup (AB/AW) in the middle of a game is not supported"))
                moves.append([col, _sgf_point(v, size)])
        if "AE" in d:
            raise ValueError(i18n.t("taş silme (AE) desteklenmiyor", "removing stones (AE) is not supported"))
        for col in ("B", "W"):
            for v in d.get(col, []):
                moves.append([col, _sgf_point(v, size)])
    if any(c in SETUP and mv == "pass" for c, mv in moves):
        raise ValueError(i18n.t("boş kurulum taşı", "empty setup stone"))
    return {"size": size, "komi": komi, "moves": moves,
            "info": {k: root_[k][0] for k in SGF_INFO if root_.get(k)},
            "you": "W" if first_("SN", "B").upper() == "W" else "B"}


def _escape(s):
    return str(s).replace("\\", "\\\\").replace("]", "\\]")


def sgf_dump(moves, size=DEFAULT_SIZE, komi=KOMI, pb="Player", pw="GoMentor",
             info=None, you="B", branches=None):
    """Setup stones go on the root node (AB/AW), then moves and variation branches in order.
    info: SGF_INFO keys (names and result of an imported game); overrides pb/pw."""
    info = {"RU": "Chinese", "PB": pb, "PW": pw, **(info or {})}
    root_ = "".join(f"{k}[{_escape(v)}]" for k, v in info.items())
    for col in SETUP:
        stones_ = [to_sgf(from_gtp(mv, size), size) for c, mv in moves if c == col]
        if stones_:
            root_ += col + "".join(f"[{t}]" for t in stones_)
    if you != "B":
        root_ += f"SN[{you}]"

    def node(col, mv):
        return f";{col}[{'' if mv == 'pass' else to_sgf(from_gtp(mv, size), size)}]"

    def tree(parent, start, line_):
        out = []
        for k, (col, mv) in enumerate(line_):
            i = start + k
            bottom = [j for j, d in enumerate(branches or ())
                   if d.get("parent") == parent and d.get("root") == i and d.get("moves")]
            if bottom:
                main_ = node(col, mv) + tree(parent, i + 1, line_[k + 1:])
                beside = "".join(f"({tree(j, i, branches[j]['moves'])})" for j in bottom)
                out.append(f"({main_}){beside}")
                break
            out.append(node(col, mv))
        return "".join(out)

    start = setup_count(moves)
    body = tree(None, start, [m for m in moves if m[0] not in SETUP])
    return f"(;GM[1]FF[4]CA[UTF-8]SZ[{size}]KM[{komi}]{root_}{body})"


def sgf_komi(text):
    return sgf_read(text)["komi"]


def sgf_load(text):
    return sgf_read(text)["moves"]


def sgf_result(re_):
    """SGF RE[] -> (winner 'B'/'W'/None, margin or None). Draw: (None, 0).

    'B+R' resignation, 'W+T' time, 'B+3.5' points. Unknown/void: (None, None).
    """
    s = (re_ or "").strip().upper()
    if s in ("0", "DRAW", "JIGO"):
        return None, 0.0
    if len(s) < 2 or s[0] not in "BW" or s[1] != "+":
        return None, None
    try:
        return s[0], float(s[2:])
    except ValueError:
        return s[0], None


def game_over(moves):
    """Two passes in a row."""
    return len(moves) >= 2 and all(m[1] == "pass" for m in moves[-2:])
