#!/usr/bin/env python3
"""Tsumego (life-and-death) problems: SGF folder -> Leitner card.

Problem SGF = setup stones (AB/AW) on the root node, the solution on the main
line. The fixed convention of problem collections: at every fork the FIRST
branch is the right answer, sibling branches are wrong attempts.
goban.sgf_nodes already reads the main line -- so the answer key comes for
free, there is no separate solution file.

Cards go into store's reviews.json under a `tsumego#<id>` key. Because a card
id is a string, there is NO code to write on the Leitner side: same boxes,
same intervals, same file.
"""
import os, shutil, zipfile

import bundle, goban, i18n, store

PREFIX = "tsumego#"
# A main line longer than this means the file is a GAME RECORD, not a problem.
# SGFs of handicap games also carry AB setup stones -- that check alone did
# not filter games out and produced a silly "find move 1" problem.
MAX_SOLUTIONS = 40


def folders():
    """Problem folders, in order of precedence.

    If GOMENTOR_TSUMEGO is set, only that one (for tests and custom setups).
    Otherwise tsumego/ next to the app (the bundled sets, READ-ONLY in an
    installed copy) + tsumego/ in the data root (what the user added).
    """
    z = os.environ.get("GOMENTOR_TSUMEGO")
    if z:
        return [z]
    return [bundle.beside("tsumego"), os.path.join(bundle.data_root(), "tsumego")]


def list_all():
    """[(id, path)] -- every .sgf problem, sorted by id.

    id = path relative to its folder, no extension, '/'-separated:
    "basics/01-straight-three-kill". If the same id exists in two folders the
    LATER one wins -- the user's copy overrides the bundled one.
    """
    found = {}
    for k in folders():
        if not os.path.isdir(k):
            continue
        for root_, _, files in os.walk(k):
            for f in files:
                if f.lower().endswith(".sgf"):
                    path_ = os.path.join(root_, f)
                    pid = os.path.relpath(path_, k).replace("\\", "/")[:-4]
                    found[pid] = path_
    return sorted(found.items())


def _read(path_, pid):
    """(Game, move_no, solution_gtp). ValueError for a broken/unsolvable file.

    The returned Game carries only the setup stones; `move_no` is the 1-based
    number of the move the user is about to play. So Game.board_(move_no - 1)
    gives the problem position directly and the drawing code in the UI needs
    no change -- the review card already draws it that way.
    """
    with open(path_, encoding="utf-8", errors="replace") as f:
        text_ = f.read()
    o = goban.sgf_read(text_)                       # AE / setup in the middle of a game -> ValueError
    k = goban.setup_count(o["moves"])
    if not k:
        raise ValueError(i18n.t(f"{pid}: kurulum taşı (AB/AW) yok — bu bir problem değil",
                         f"{pid}: no setup stones (AB/AW) — this is not a problem"))
    solution = o["moves"][k:]
    if not solution or solution[0][1] == "pass":
        raise ValueError(i18n.t(f"{pid}: ana dalda çözüm hamlesi yok", f"{pid}: no solution move on the main line"))
    if len(solution) > MAX_SOLUTIONS:
        raise ValueError(i18n.t(f"{pid}: {len(solution)} hamlelik ana dal — bu bir maç kaydı, problem değil",
                         f"{pid}: a {len(solution)}-move main line — this is a game record, not a problem"))
    m = store.Game(name=PREFIX + pid, mode="tsumego", size=o["size"], komi=o["komi"],
                  moves=o["moves"][:k], color=solution[0][0], info=o["info"])
    return m, k + 1, solution[0][1]


def load_problem(pid):
    """id -> (Game, move_no, solution_gtp). ValueError for an unknown id."""
    path_ = dict(list_all()).get(pid)
    if not path_:
        raise ValueError(i18n.t(f"tsumego yok: {pid}", f"no such tsumego: {pid}"))
    return _read(path_, pid)


def set_name(pid):
    """The set a problem belongs to: the top-level folder name."""
    return pid.split("/")[0] if "/" in pid else "(tek)"


def sets():
    """[(set, total, solved)]. Solved = answered CORRECTLY at least once
    (Leitner box 1+); a wrongly answered one stays in box 0 and does not count."""
    d = store.review_state()
    counter = {}
    for pid, _ in list_all():
        name = set_name(pid)
        t, c = counter.get(name, (0, 0))
        counter[name] = (t + 1, c + (d.get(PREFIX + pid, {}).get("box", 0) >= 1))
    return sorted((name, t, c) for name, (t, c) in counter.items())


def reviews(today):
    """Due tsumego cards [(card, due)], ordered by due date."""
    d, out = store.review_state(), []
    for pid, _ in list_all():
        v = d.get(PREFIX + pid, {}).get("next")
        if v and v <= today.isoformat():
            out.append((PREFIX + pid, v))
    return sorted(out, key=lambda t: t[1])


def unseen(set_=None):
    """Ids of problems never answered, optionally filtered by set."""
    d = store.review_state()
    return [pid for pid, _ in list_all()
            if PREFIX + pid not in d and (set_ is None or set_name(pid) == set_)]


def set_problems(set_):
    """[(pid, state)] -- state: "solved" | "next_up" | "locked".

    A set is solved IN ORDER: the first unsolved problem is "next_up", the ones
    after it are locked. A WRONGLY answered problem stays in Leitner box 0, so
    it is still "next_up" -- you do not move on without answering correctly.
    """
    d, out, is_open = store.review_state(), [], True
    for pid, _ in list_all():
        if set_name(pid) != set_:
            continue
        if d.get(PREFIX + pid, {}).get("box", 0) >= 1:
            out.append((pid, "solved"))
        elif is_open:
            out.append((pid, "next_up"))
            is_open = False
        else:
            out.append((pid, "locked"))
    return out


def next_up(set_):
    """The first problem to solve in the set (pid), or None -- if the set is done."""
    return next((pid for pid, state_ in set_problems(set_) if state_ == "next_up"), None)


# ---------- import ----------
def user_folder():
    """WRITABLE root for the sets the user adds. The bundled tsumego/ may be
    read-only in an installed copy; nothing is written there."""
    return os.environ.get("GOMENTOR_TSUMEGO") or os.path.join(bundle.data_root(), "tsumego")


def _safe(name):
    """Makes a relative path from a zip/folder unable to escape the target."""
    return "/".join(p for p in name.replace("\\", "/").split("/")
                    if p not in ("", ".", "..") and ":" not in p)


def _from_source(source):
    """Yields (relative name, content bytes): the .sgf files in a .zip, the .sgf
    files in a folder, or a single .sgf file."""
    if os.path.isfile(source) and zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as z:
            for info in z.infolist():
                if not info.is_dir() and info.filename.lower().endswith(".sgf"):
                    yield info.filename, z.read(info)
    elif os.path.isdir(source):
        for root_, _, files in os.walk(source):
            for f in sorted(files):
                if f.lower().endswith(".sgf"):
                    full_path = os.path.join(root_, f)
                    with open(full_path, "rb") as fh:
                        yield os.path.relpath(full_path, source), fh.read()
    else:
        with open(source, "rb") as fh:
            yield os.path.basename(source), fh.read()


def import_set(source, name=None):
    """A .sgf / folder / .zip -> a new set in the user's tsumego folder.

    Returns: (set name, added, skipped). Outside data: every file is PARSED,
    and nothing that cannot be read as a problem is left in the set --
    otherwise a broken file enters the queue and gets stuck as a card nobody
    can solve.
    """
    name = name or os.path.splitext(os.path.basename(source.rstrip("/\\")))[0]
    name = "".join(c for c in name if c not in r'<>:"/\|?*').strip() or "set"
    goal = os.path.join(user_folder(), name)
    appended = skipped = 0
    try:
        incoming = list(_from_source(source))
    except zipfile.BadZipFile as e:                # a broken archive: ValueError to the caller
        raise ValueError(i18n.t(f"arşiv okunamadı: {e}", f"could not read the archive: {e}"))
    for relative, content in incoming:
        relative = _safe(relative)
        if not relative:
            skipped += 1
            continue
        path_ = os.path.join(goal, relative.replace("/", os.sep))
        os.makedirs(os.path.dirname(path_) or goal, exist_ok=True)
        with open(path_, "wb") as f:
            f.write(content)
        try:
            _read(path_, relative)
        except (OSError, ValueError):
            os.remove(path_)
            skipped += 1
        else:
            appended += 1
    if not appended and os.path.isdir(goal):
        shutil.rmtree(goal, ignore_errors=True)
    return name, appended, skipped


# ---------- verification ----------
# ponytail: exhaustive search. Only for SMALL enclosed regions -- branching is
# exponential as the region grows. Written to verify the bundled set in
# selftest; NOT used to check live answers (there the answer key is the SGF
# itself). If large sets ever need verifying: KataGo + allowMoves + includeOwnership.
MAX_REGIONS = 6


def _lives(b, region, turn, goal, defender, pass_=0, depth=0, memo=None, limit=True):
    """Does the group at index `goal` live. turn/defender: 1 black, -1 white.

    memo: transposition cache. The key carries the depth too, because the
    depth limit affects the result -- without it the cache could change the
    answer. limit: the value returned when the depth runs out (a fight that
    does not end, typically ko).
    """
    if b.g[goal] == 0:
        return False                               # captured
    if pass_ >= 2:
        return True                                # nobody can make progress (seki included)
    if depth > 2 * len(region) + 4:
        return limit
    if memo is None:
        memo = {}
    key_ = (tuple(b.g), b.ko, turn, pass_, depth)
    if key_ in memo:
        return memo[key_]
    # The defender looks for ONE living variation; the attacker must kill in EVERY variation.
    caller = any if turn == defender else all

    def branches():
        for i in list(region) + [None]:
            c = b.copy()
            if i is None:
                c.ko = None
                yield _lives(c, region, -turn, goal, defender, pass_ + 1, depth + 1, memo, limit)
            elif c.g[i] == 0 and c.play(i, turn):
                yield _lives(c, region, -turn, goal, defender, 0, depth + 1, memo, limit)
    memo[key_] = result_ = caller(branches())
    return result_


def solve(b, turn, maximum=MAX_REGIONS, limit=True):
    """(winning move indices, goal, region), or None if the region is large.

    Goal = the group with the FEWEST liberties. The group in question in a
    tsumego is always the surrounded one; assuming its color would be wrong,
    because in "kill" problems the goal is the mover's opponent and in "live"
    problems it is their own group.
    Region = the goal's liberties + the empty points next to them (inside the
    eye space).
    """
    groups, seen_ = [], set()
    for i, v in enumerate(b.g):
        if v and i not in seen_:
            st, libs = b.group(i)
            seen_ |= st
            groups.append((len(libs), i, libs))
    if not groups:
        return None
    _, goal, libs = min(groups, key=lambda g: g[0])
    defender = b.g[goal]
    region = set(libs)
    for x in libs:
        region |= {n for n in b.nbrs(x) if b.g[n] == 0}
    if len(region) > maximum:
        return None
    region, memo, winner = sorted(region), {}, []
    for i in region:
        c = b.copy()
        if c.play(i, turn):
            lived = _lives(c, region, -turn, goal, defender, memo=memo, limit=limit)
            # If the defender moves the aim is to live; if the attacker moves, to kill.
            if lived == (turn == defender):
                winner.append(i)
    return winner, goal, region


def verify(pid):
    """Is the SGF's solution really the ONE right move. Returns: (is_right, explanation)."""
    m, no, solution = load_problem(pid)
    b, turn = m.board_at(no - 1), goban.color(m.color)
    if not any(b.g):
        return False, "empty board"
    r = solve(b, turn)
    if r is None:
        return True, "region too large — verification skipped"
    winner = [goban.to_gtp(i, m.size) for i in r[0]]
    # If the result depends on the depth limit (ko) it is not a clean problem.
    if solve(b, turn, limit=False)[0] != r[0]:
        return False, "result depends on ko — the solver cannot decide at the limit"
    if winner == [solution]:
        return True, f"{solution} is the only right move"
    return False, f"the SGF says {solution}, the solver says {winner or 'none'}"


def demo():
    """Verifies every problem of the bundled basic set."""
    import datetime
    problems = list_all()
    assert problems, f"no problems found: {folders()}"
    for pid, _ in problems:
        ok, reason = verify(pid)
        assert ok, f"{pid}: {reason}"
        print(f"  {pid:36} {reason}")
    s = sets()
    assert s and sum(t for _, t, _ in s) == len(problems), s
    assert all(0 <= c <= t for _, t, c in s), s
    assert not (set(unseen()) & {k[len(PREFIX):] for k, _ in reviews(datetime.date.max)}),         "a problem cannot be both new and up for review"
    print(f"{len(problems)} problem, {len(s)} set: " +
          ", ".join(f"{name} {c}/{t}" for name, t, c in s))


if __name__ == "__main__":
    demo()
