#!/usr/bin/env python3
"""Imports an outside SGF (OGS, KGS, Fox...) into the archive and grades the user's moves.

Separate mode in the archive ('sgf'): it never touches ranked SP or the
opponent ladder. Grading uses the game's own settings (size, komi, rules,
handicap stones); the engine's live-game settings stay as they are -- you can
keep playing while an import runs.
"""
import goban, i18n, scoring, store

SIZES = (9, 13, 19)


def read(text_):
    """SGF text -> the sgf_read dict. ValueError for an unsupported file."""
    o = goban.sgf_read(text_)
    if o["size"] not in SIZES:
        raise ValueError(i18n.t(f"{o['size']}x{o['size']} desteklenmiyor (9, 13, 19)",
                                f"{o['size']}x{o['size']} is not supported (9, 13, 19)"))
    if not any(c in ("B", "W") and mv != "pass" for c, mv in o["moves"]):
        raise ValueError(i18n.t("SGF'te oynanmış hamle yok", "The SGF has no played moves"))
    return o


def create(text_, color):
    """Writes the game to the archive (not graded yet). Returns: store.Game."""
    o = read(text_)
    m = store.Game(name=store.new_name("sgf"), mode="sgf", size=o["size"], komi=o["komi"],
                  moves=o["moves"], color=color, info=o["info"])
    store.save(m)
    winner, diff = goban.sgf_result(o["info"].get("RE"))
    if winner or diff == 0:
        S = 0.5 if winner is None else 1.0 if winner == color else 0.0
        store.add_record(m, {"game_end": {
            "result": "sgf", "re": o["info"]["RE"], "won": S,
            # from the user's point of view; no score for games ended by resignation or time
            "score": None if diff is None else (diff if S == 1.0 else -diff)}})
    return m


def missing(m):
    """Ungraded moves (1-based move_no), BOTH colors: in an imported game the
    opponent is human too, so the category means something. The opponent's
    records are told apart by `mover` and stay out of the user's statistics
    (store.valid_records). An interrupted import resumes from here: records
    already written are not queried again."""
    exists = {r.get("move_no") for r in m._valid()}
    return [n for n, (c, mv) in enumerate(m.moves, 1)
            if c in ("B", "W") and mv != "pass" and n not in exists]


def grade_moves(eng, m, my_rank, next_rank, progress_=None):
    """Each move is written on its own (append-only) -- nothing is lost if the app closes."""
    turn = missing(m)
    rule = goban.sgf_rule(m.info)
    for i, n in enumerate(turn):
        if progress_:
            progress_(i, len(turn))
        f = scoring.grade(eng, m.moves[:n], m.moves[n - 1][0], my_rank, next_rank,
                          size=m.size, komi=m.komi, rule=rule)
        store.add_record(m, {"move_no": n, **f})
    return m
