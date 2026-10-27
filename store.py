#!/usr/bin/env python3
"""Game records. The file system today, Postgres/S3 tomorrow -- the caller stays the same.

The second (and last) place that changes if this ever moves to the web. The
function signatures are deliberately a repository interface: list / load /
save. No caller ever sees a file path; it sees a `Game` object.
"""
import getpass, json, os, shutil, time
from dataclasses import dataclass, field

import goban, bundle

# Data goes to the WRITABLE root: next to the app in a portable folder, the
# user folder in an installed copy (Program Files / Applications are not writable).
ROOT = os.environ.get("GOMENTOR_DATA", os.path.join(bundle.data_root(), "games"))


@dataclass
class Game:
    """The full state of one game. Serializable -- not a UI object.

    The split matters: had the state lived in the UI, neither "replay an old
    game" nor a server-side session would have been possible. The same data
    feeds both.
    """
    name: str
    mode: str = "game"                      # game | lesson | ranked | duo | sgf (imported)
    size: int = goban.DEFAULT_SIZE
    komi: float = goban.KOMI
    moves: list = field(default_factory=list)
    records: list = field(default_factory=list)   # grade() output per move
    meta: dict = field(default_factory=dict)
    color: str = "B"                       # the user's color; may be W in an imported game
    info: dict = field(default_factory=dict)      # SGF game info (PB, PW, RE...)
    # Variation tree. The main line stays as `moves`; each branch is {"parent":
    # branch index|None, "root": after how many of the parent's moves, "moves":
    # [...], "records": [...]}. Statistics, SP, video and the pattern report look
    # only at `moves`/`records`: branches are structurally kept out.
    branches: list = field(default_factory=list)

    def board_at(self, upto=None, branch=None):
        return goban.replay(self.line_moves(branch), self.size, upto)

    def line_moves(self, branch=None):
        """The branch's full move list: the parent's first `root` moves + its own."""
        if branch is None:
            return self.moves
        d = self.branches[branch]
        return self.line_moves(d["parent"])[:d["root"]] + d["moves"]

    def owner(self, branch, i):
        """Which line the position after i moves lives on: the branch's fork point and
        everything before it belong to the parent (those positions sit on the
        parent, not on the branch)."""
        while branch is not None and i <= self.branches[branch]["root"]:
            branch = self.branches[branch]["parent"]
        return branch

    def record_of(self, move_no, branch=None):
        """The grading of a 1-based move number (if any), both colors."""
        source = self._valid() if branch is None else self.branches[branch]["records"]
        return next((k for k in source if k.get("move_no") == move_no), None)

    def _valid(self):
        """Drops retracted moves.

        retry() does not write OVER the cancelled record, it appends a separate
        {'retracted': True} marker after it (the file is append-only). Dropping
        the markers alone is not enough -- the cancelled move's own record sits
        there unmarked and leaked into the statistics.
        """
        k = self.records
        return [r for i, r in enumerate(k)
                if not r.get("retracted") and "game_end" not in r
                and not (i + 1 < len(k) and k[i + 1].get("retracted"))]

    @property
    def valid_records(self):
        """The user's moves. In an imported game the opponent's moves are graded
        too; they must NOT leak into the statistics, the pattern report or SP."""
        return [r for r in self._valid() if r.get("mover", self.color) == self.color]

    @property
    def opponent_records(self):
        return [r for r in self._valid() if r.get("mover", self.color) != self.color]

    @property
    def unfixed(self):
        """Blunders on the main line (move_no) that have not been fixed.

        Fixed = the first move of a branch leaving the main line at the same
        fork is graded and is not a blunder. The state is not stored
        separately, it is derived from the branches.
        """
        import scoring
        fixed = {d["root"] + 1 for d in self.branches if d["parent"] is None
                   for k in d["records"]
                   if k.get("move_no") == d["root"] + 1
                   and scoring.category(k) not in (None, "blunder")}
        return [r["move_no"] for r in self.valid_records
                if scoring.category(r) == "blunder" and r["move_no"] not in fixed]

    def branch_play(self, branch, i, move):
        """On line `branch`, after i moves, `move` ([color, gtp]) was played.

        Same move, same node: if the line's own continuation or a branch from
        the same fork starts with the same move, enter it. At the end of a
        line (not the main line) the branch grows. Otherwise a new branch.
        Returns: (branch, i + 1, is_new).
        """
        h = self.line_moves(branch)
        if i < len(h) and list(h[i]) == list(move):
            return branch, i + 1, False
        for j, d in enumerate(self.branches):
            if d["parent"] == branch and d["root"] == i and list(d["moves"][0]) == list(move):
                return j, i + 1, False
        if branch is not None and i == len(h):
            self.branches[branch]["moves"].append(list(move))
            return branch, i + 1, True
        self.branches.append({"parent": branch, "root": i, "moves": [list(move)], "records": []})
        return len(self.branches) - 1, i + 1, True

    @property
    def result(self):
        """End-of-game record: {'result': 'resign'|'two_passes', an 'sp' event if ranked...}.

        This cannot be regenerated (SP depends on the profile at that moment),
        so it is written to the raw jsonl, not to the derived folders. {} if none.
        """
        return next((r["game_end"] for r in reversed(self.records)
                     if "game_end" in r), {})


def _path(name, extension):
    return os.path.join(ROOT, name + extension)


# Layout: raw records (sgf/jsonl) and state (profile/rank) in ROOT; everything
# derived goes into a subfolder. The split is not arbitrary -- delete ALL the
# subfolders and not a single game is lost, all of it is regenerated. When
# moving to another machine or sending to a friend, ROOT itself is all that
# needs to travel.
DERIVED = {"_brief.md": "brief", "_script.json": "brief",
             "_analysis.json": "brief", "_analysis.mp4": "video",
             "_patterns.md": "report", "_error.log": "log"}


def sub(folder, file_):
    """Path of a derived file; creates the folder if needed."""
    d = os.path.join(ROOT, folder)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, file_)


def arrange():
    """Moves the old flat layout into subfolders. Idempotent, runs on every start.

    Frame folders (_<name>_frame) are deleted: intermediate output of the
    video montage, regenerable, and they took 1-2 MB.
    """
    if not os.path.isdir(ROOT):
        return 0
    n = 0
    for f in list(os.listdir(ROOT)):
        full_path = os.path.join(ROOT, f)
        if os.path.isdir(full_path):
            if f.startswith("_") and f.endswith("_frame"):
                shutil.rmtree(full_path, ignore_errors=True)
                n += 1
            continue
        goal = next((k for end, k in DERIVED.items() if f.endswith(end)), None)
        if goal:
            try:
                os.replace(full_path, sub(goal, f))
                n += 1
            except OSError:
                pass
    return n


def new_name(mode="game"):
    """Timestamped name. A second record in the same second (two SGF imports
    back to back) used to overwrite the first: -2, -3 suffix if taken."""
    name = mode + time.strftime("_%Y%m%d-%H%M%S")
    candidate, n = name, 1
    while os.path.exists(_path(candidate, ".sgf")):
        n += 1
        candidate = f"{name}-{n}"
    return candidate


def list_all():
    """[(name, move_count, time, size)] -- newest first.

    The size is returned too: 9x9, 13x13 and 19x19 games sit mixed in the
    archive, and if the list does not say which is which you cannot tell
    without clicking. The text is read anyway.
    """
    if not os.path.isdir(ROOT):
        return []
    out = []
    for f in os.listdir(ROOT):
        if not f.endswith(".sgf"):
            continue
        name = f[:-4]
        p = _path(name, ".sgf")
        try:
            o = goban.sgf_read(open(p, encoding="utf-8").read())
            out.append((name, goban.move_count(o["moves"]), os.path.getmtime(p), o["size"]))
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda t: -t[2])


MODE_NAME = {"ranked": ("Dereceli", "Ranked"), "game": ("Serbest", "Casual"),
           "lesson": ("Ders", "Lesson"), "sgf": ("İçe aktarılan", "Imported"),
           "duo": ("Karşılıklı", "Two players")}


def display_name(name):
    """The game name on screen: 'ranked_20260916-162549' -> 'Ranked 17' (the nth
    game of its mode, in order of creation). The file name does NOT change:
    video, brief, review cards and branches depend on it; renaming would break
    them. A new game not yet saved gets the next number."""
    import i18n
    mode = mode_of(name)
    try:
        same = sorted(f[:-4] for f in os.listdir(ROOT)
                      if f.endswith(".sgf") and mode_of(f[:-4]) == mode)
    except OSError:
        same = []
    turn = same.index(name) + 1 if name in same else len(same) + 1
    return f"{i18n.t(*MODE_NAME.get(mode, (mode, mode)))} {turn}"


def mode_of(name):
    """The file name prefix gives the mode: game_ / lesson_ / ranked_ ..."""
    return name.split("_")[0] if "_" in name else "game"


def load_game(name):
    m = Game(name=name, mode=mode_of(name))
    with open(_path(name, ".sgf"), encoding="utf-8") as f:
        o = goban.sgf_read(f.read())
    m.size = o["size"]                     # size from the file; assuming a fixed 9 was wrong
    m.komi = o["komi"]                     # old games used 7.0: counted with their own komi
    m.moves, m.color, m.info = o["moves"], o["you"], o["info"]
    try:
        with open(_path(name, ".jsonl"), encoding="utf-8") as f:
            m.records = [json.loads(l) for l in f if l.strip()]
    except OSError:
        pass
    try:
        with open(_path(name, "_branches.json"), encoding="utf-8") as f:
            m.branches = json.load(f)
    except (OSError, ValueError):
        pass
    return m


def save_branch(m):
    """Branches are raw records (moves the user tried, cannot be regenerated): they
    go to ROOT, not to a derived subfolder. They are also written into the SGF
    file as a variation tree."""
    os.makedirs(ROOT, exist_ok=True)
    with open(_path(m.name, "_branches.json"), "w", encoding="utf-8") as f:
        json.dump(m.branches, f, ensure_ascii=False)
    save(m)


def save(m):
    """The SGF (the game played + variation branches) is rewritten in full."""
    os.makedirs(ROOT, exist_ok=True)
    with open(_path(m.name, ".sgf"), "w", encoding="utf-8") as f:
        # Black = the player. The name is the computer's user; done here so goban stays pure.
        f.write(goban.sgf_dump(m.moves, m.size, m.komi, pb=getpass.getuser(),
                               info=m.info, you=m.color, branches=m.branches))


def add_record(m, row_):
    """Appends a single grading (append-only log).

    Retracted moves are written too but marked with {'retracted': True} --
    unmarked, they mixed with real moves and broke the statistics.
    """
    os.makedirs(ROOT, exist_ok=True)
    m.records.append(row_)
    with open(_path(m.name, ".jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(row_, ensure_ascii=False) + "\n")


# ---------- ranked profile ----------
# A SEPARATE rank per board size. 9x9 and 19x19 are different skills -- 9x9 is
# almost pure tactics, 19x19 has strategy and a sense of direction. OGS keeps
# them apart too. A single rank would be a lie. The schema is keyed by size:
# 13x13 came later as a NEW KEY, no migration needed.
PROFILE = os.path.join(ROOT, "profile.json")


def _empty_state():
    import scoring
    return {"value": scoring.PLACEMENT_START, "sp": 0.0, "streak": 0,
            "game": 0, "placement": []}


def load_profile():
    try:
        with open(PROFILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_profile(p):
    os.makedirs(ROOT, exist_ok=True)
    with open(PROFILE, "w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False, indent=1)


def setting(key_, default_=None):
    """User preference (lang, video_llm...). profile.json -> settings."""
    return load_profile().get("settings", {}).get(key_, default_)


def set_setting(key_, value):
    p = load_profile()
    p.setdefault("settings", {})[key_] = value
    save_profile(p)


# ---------- mistake review (Leitner) ----------
# Card = a fixed blunder ("<name>#<move_no>"). State in one file:
# {card: {"box": 0-4, "next": "YYYY-MM-DD"}}. A fixed blunder with no record
# (fixed before this feature existed) counts as due today.
INTERVALS = (1, 3, 7, 14, 30)       # box -> days


def _reviews_path():
    return os.path.join(ROOT, "reviews.json")     # ROOT changes in tests: every time


def review_state():
    try:
        with open(_reviews_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def leitner(box, correct):
    """(box, correct) -> (new box, in how many days). Wrong: box 0, tomorrow."""
    new = min(box + 1, len(INTERVALS) - 1) if correct else 0
    return new, INTERVALS[new]


def _write_reviews(card, box, day, today):
    import datetime
    d = review_state()
    d[card] = {"box": box, "next": (today + datetime.timedelta(days=day)).isoformat()}
    os.makedirs(ROOT, exist_ok=True)
    with open(_reviews_path(), "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    return d[card]


def add_review(card, today):
    """A blunder was fixed: the card enters box 0, first review tomorrow. Leaves an existing card alone."""
    return review_state().get(card) or _write_reviews(card, 0, INTERVALS[0], today)


def apply_review(card, correct, today):
    """Review answer. today: datetime.date. Returns: the new record."""
    box, day = leitner(review_state().get(card, {}).get("box", 0), correct)
    return _write_reviews(card, box, day, today)


def review_cards(today):
    """[(card, due)] every fixed blunder, ordered by due date (ISO date)."""
    import scoring
    d = review_state()
    out = []
    for name, *_ in list_all():
        try:
            m = load_game(name)
        except (OSError, ValueError):
            continue
        if not m.branches:
            continue
        is_open = set(m.unfixed)
        for r in m.valid_records:
            if scoring.category(r) == "blunder" and r["move_no"] not in is_open:
                card = f"{name}#{r['move_no']}"
                out.append((card, d.get(card, {}).get("next", today.isoformat())))
    # Tsumego has its own queue (tsumego.reviews) shown on the Problems screen --
    # this queue holds ONLY the user's own blunders.
    return sorted(out, key=lambda t: t[1])


def rank_state(size):
    """The rank state for this board size. If none, an empty state awaiting placement."""
    return load_profile().get(f"{size}x{size}") or _empty_state()


def write_rank(size, state_):
    p = load_profile()
    p[f"{size}x{size}"] = state_
    save_profile(p)
    return state_


def apply_rank_result(size, S, opponent_value, quality=None):
    """A ranked game ended. During placement it advances placement, otherwise it
    applies SP. Returns: (state, event) -- the event tells the UI what to write.

    S: 1 won, 0.5 draw, 0 lost (scoring.game_result). True/False in old
    records equal 1/0, so reading works unchanged.
    quality: this game's `game_level(...)['value']` (None if missing).
    During placement it is combined with the binary search; in the SP stage it
    is NOT USED -- ranked SP looks only at the result (as in real Go).
    """
    import scoring
    d = rank_state(size)
    d["game"] += 1
    # A draw neither extends nor breaks the streak: it is no proof that the rank is wrong.
    d["streak"] = d["streak"] + 1 if S == 1 else 0 if S == 0 else d["streak"]

    if len(d["placement"]) < scoring.PLACEMENT_GAMES:
        d["placement"].append([opponent_value, float(S), quality])
        done_ = len(d["placement"]) >= scoring.PLACEMENT_GAMES
        # Old records may have 2 elements -- treat the quality as None.
        history = [(x[0], x[1]) for x in d["placement"]]
        qualities = [x[2] if len(x) > 2 else None for x in d["placement"]]
        if done_:
            s = scoring.placement_result(history, qualities, size)
            d["value"], d["sp"] = s["value"], 0.0
            event = {"kind": "placement_done", **s}
        else:
            event = {"kind": "placement", "remaining": scoring.PLACEMENT_GAMES - len(history),
                    "next_opponent": scoring.value_rank(scoring.placement_opponent(history, size))}
    else:
        old_value, old_sp = d["value"], d["sp"]
        ranked = d["game"] - scoring.PLACEMENT_GAMES - 1     # 0-based
        d["value"], d["sp"], diff = scoring.apply_sp(
            d["value"], d["sp"], S, opponent_value, ranked,
            scoring.d_const(size), size)    # Elo constant and floor by size
        event = {"kind": "sp", "sp_change": diff,
                "provisional": ranked < scoring.PROVISIONAL_GAMES,
                "previous": (scoring.value_rank(old_value), old_sp),
                "current": (scoring.value_rank(d["value"]), d["sp"]),
                "rank_up": d["value"] - old_value}
    write_rank(size, d)
    return d, event


def next_opponent(size):
    """The bot you face in a ranked game. During placement, a binary search;
    otherwise your own rank, shifted up by the streak."""
    import scoring
    d = rank_state(size)
    if len(d["placement"]) < scoring.PLACEMENT_GAMES:
        return scoring.placement_opponent([(x[0], x[1]) for x in d["placement"]], size)
    return scoring.opponent_choice(d["value"], d["streak"], size)
