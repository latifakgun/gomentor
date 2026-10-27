#!/usr/bin/env python3
"""Move grading: point loss, mechanism detection, level estimate.

This module is the app's CORE. Pure functions: (engine, moves) -> data.
It knows no UI, no files, no session. It runs the same on the desktop or in
a FastAPI worker.
"""
import math, re

import i18n
import goban
from goban import DEFAULT_SIZE, KOMI, Board, from_gtp, to_gtp, replay


# Level ladder: weakest to strongest. One humanPolicy query per rung
# (maxVisits=1, cheap). Extending it improves accuracy, and cost.
LADDER = ["20k", "15k", "10k", "6k", "2k", "1d"]

REGIONS = ["top_left", "top", "top_right", "left", "center", "right",
            "bottom_left", "bottom", "bottom_right"]


def region_of(i, size=DEFAULT_SIZE):
    r, c = divmod(i, size)
    t = size / 3.0
    return (0 if r < t else 1 if r < 2 * t else 2) * 3 + \
           (0 if c < t else 1 if c < 2 * t else 2)


# ---------- level scale ----------
def rank_value(label):
    """'15k' -> -14, '1k' -> 0, '1d' -> 1. A continuous scale with no gap at the
    kyu/dan border; interpolation happens on it."""
    label = label.strip().lower()
    n = int(label[:-1])
    return 1 - n if label.endswith("k") else n


def value_rank(v):
    """Turns a numeric value into a readable label. In-between values are rounded."""
    n = int(round(v))
    return f"{1 - n}k" if n <= 0 else f"{n}d"


def expected_loss(mi, policy, sign):
    """Expected point loss in this position for someone playing with this move distribution.

    mi: KataGo moveInfos (sorted by order), policy: [(move, probability)].

    ponytail: KataGo only searches moves it finds promising; the part of the
    distribution that falls on moves the engine never looked at is charged as
    much as "the worst move it did look at". Dropping them made weak levels
    look better than they are and squeezed the ladder. Better: grade the
    remaining moves one by one too (expensive).
    """
    if not mi:
        return None
    best = mi[0]["scoreLead"]
    losses = {m["move"]: max(0.0, sign * (best - m["scoreLead"])) for m in mi}
    worst = max(losses.values()) if losses else 0.0
    top_p = top = 0.0
    for mv, p in policy:
        top_p += p
        top += p * losses.get(mv, worst)
    return top / top_p if top_p > 1e-9 else None


def level_curve(eng, moves, mi, sign, ladder=LADDER, size=None):
    """[(label, expected_loss)] -- for every rung of the ladder."""
    out = []
    for r in ladder:
        try:
            pol = eng.human_policy(moves, f"rank_{r}", size)
        except Exception:
            continue
        bk = expected_loss(mi, pol, sign)
        if bk is not None:
            out.append((r, bk))
    return out


# MIN_SEPARATION and LEVEL_OFFSET: both MEASURED (calibration run, 2026-09-07).
# Bots of known rank played, their moves were graded with grade(), and the
# resulting game_level was compared with the true label:
#
#   true    measured  offset       linear fit: measured = 0.97*true + 3.96
#     20k      16k     +4.2r        i.e. NO SCALE ERROR (slope ~1), only a
#     15k      11k     +4.5r        CONSTANT shift. The spread of the offset
#     10k       5k     +5.0r        across ranks is 0.54r -- consistent and
#      6k       3k     +3.5r        correctable.
#      2k       1d     +2.0r  <-- SATURATED, the ladder ends at 1d
#
# MIN_SEPARATION sweep (from the same raw curves, no rerun): 0.75 is BEST.
# Raising the threshold lowers the mean offset but destroys consistency
# (0.57r -> 3.48r) -- a consistent offset can be corrected, an inconsistent one cannot.
#   thresh  coverage  mean off  consistency
#   0.50    83%     +4.61r      1.88r
#   0.75    79%     +4.29r      0.57r   <-- chosen
#   1.00    72%     +3.92r      1.98r
#   2.00    48%     +2.54r      3.48r
MIN_SEPARATION = 0.75          # if the ends of the ladder do not separate this much, no estimate is made

# The shift that turns a ladder position into a player rank. "Is the tool
# inflated, or do the bots play stronger than their labels?" CANNOT be told
# apart -- but for the product it does not matter: a GoMentor rank is defined
# as "which bot you can beat", and the offset puts the estimate in the same
# language as the ladder. It is not calibrated to real-world Go ranks.
LEVEL_OFFSET = 4.3

# LEVEL_OFFSET and OPEN_THRESHOLD were measured on 9x9; on other sizes they
# are approximate. Play is NOT BLOCKED -- rules, counting and point loss (p)
# are correct regardless of size. calibration_note() tells the user what was
# measured.
#
# The level estimate (game_level / "you played ~Xk this game") is on only here.
# On 13x13 it was MEASURED and DOES NOT WORK: measured = 0.40*true - 5.4, with
# a 5.2-rank spread of the offset (9x9: 0.54). The cause is the ladder's
# spread: the expected move loss of 20k and 1d differs by only 1.09p on 13x13,
# while the median real move loss is 2.25p -- the scale is half the size of
# what it measures, and every estimate saturates at an end of the ladder. A
# threshold (MIN_SEPARATION) sweep does not save it either: consistency stays
# at 5.8-6.5 ranks from 0.30 to 2.00. An offset cannot fix that, so it is off.
#
# 19x19 was MEASURED too and stays OFF (2026-09-16). The rank medians look
# deceptively good (slope 1.01, inconsistency 2.8r; 1.0r without 15k), but the
# product shows a single-game estimate and that does not work: the
# game-to-game spread at the same rank is 4.5r (9x9: 2.8r), and 5 of 20 games
# saturated at the floor (24k). The root cause is worse than on 13x13: ladder
# spread 0.69p, median real loss 2.45p (3.6x; 13x13 2.1x).
LEVEL_CALIBRATED = (9,)          # sizes that produce an estimate
LEVEL_MEASURED = (9, 13, 19)   # sizes that have been measured -- 13 and 19: done, kept off


def d_const(size):
    """This size's Elo D constant. An unmeasured size falls back to 9x9's."""
    return D_BY_SIZE.get(size, D_BY_SIZE[DEFAULT_SIZE])


def calibration_note(size):
    """What is measured and what is approximate at this size; empty string if everything is measured."""
    if size in D_BY_SIZE and size in LEVEL_CALIBRATED:
        return ""
    n = chr(10)
    s = f"{n}{n}⚠ {size}x{size}: "
    if size in D_BY_SIZE:
        s += i18n.t(f"rank/SP kalibre (D={D_BY_SIZE[size]}), ",
                   f"rank/SP calibrated (D={D_BY_SIZE[size]}), ")
    else:
        s += i18n.t("rank/SP 9x9 sabitiyle hesaplanıyor (yaklaşık), ",
                   "rank/SP computed with the 9x9 constant (approximate), ")
    if size not in LEVEL_CALIBRATED:
        s += (i18n.t("seviye tahmini KAPALI (", "level estimate OFF (")
              + (i18n.t("ölçüldü, bu boyda çözmüyor", "measured, does not work at this size")
                 if size in LEVEL_MEASURED
                 else i18n.t("bu boyda ölçülmedi", "not measured at this size"))
              + "). ")
    return s + i18n.t("Puan kaybı (p) ve sayım etkilenmez.",
                     "Point loss (p) and the count are unaffected.")

# CEILING: because the ladder ends at 1d and the offset is 4.3, the tool cannot
# report above ~5k. That does not bind in the range it was built for; if it
# does, the LADDER gets extended upward (one maxVisits=1 query per rung,
# measured cost 0.02 s).


def level_estimate(lost, curve, min_separation=MIN_SEPARATION):
    """Where the player's loss falls on the ladder -> estimated level.

    The intuition: if I am better than the average 70 but worse than the
    average 90, call it ~80. Linear interpolation between two rungs, exactly that.

    SEPARATION THRESHOLD: in positions like the opening, where "every move is
    about equal", the whole ladder squeezes into a narrow range; an estimate
    there is flattery (like saying "you played 1d" on move 5). If the ends do
    not separate enough it returns None -- the position does not measure level.
    """
    def result_(value, label_raw, at_limit, between=None):
        """Turns the raw ladder position into a player rank and packs it.

        `value`/`label` are CORRECTED (shown to the user), `raw_value` is the
        plain interpolation on the ladder.
        """
        d = value - LEVEL_OFFSET
        out = {"label": value_rank(d), "value": round(d, 1),
               "raw_value": round(value, 1), "raw_label": label_raw,
               "at_limit": at_limit}
        if between:
            out["between"] = between
        return out

    if len(curve) < 2:
        return None
    # expected loss falls toward the strong end; monotonicity is not guaranteed, so sort
    e = sorted(curve, key=lambda t: -t[1])          # worst to best (loss decreasing)
    if e[0][1] - e[-1][1] < min_separation:
        return None
    if lost >= e[0][1]:
        return result_(rank_value(e[0][0]), e[0][0], "bottom")
    if lost <= e[-1][1]:
        return result_(rank_value(e[-1][0]), e[-1][0], "top")
    for (r1, k1), (r2, k2) in zip(e, e[1:]):
        if k1 >= lost >= k2:
            v1, v2 = rank_value(r1), rank_value(r2)
            t = 0.0 if abs(k1 - k2) < 1e-9 else (k1 - lost) / (k1 - k2)
            v = v1 + t * (v2 - v1)
            return result_(v, value_rank(v), None, (r1, r2))
    return None


# ---------- mechanism detection ----------
# ---------- big point (strategic detection) ----------

def open_scores(b):
    """Each region's 'openness' score: the sum of the distance from every empty
    point to the nearest stone (capped at 3). A stoneless region scores high,
    a full one near zero.

    Pure board math -- the engine is not asked. At most 81x81 comparisons on 9x9.
    """
    size = b.size
    stones_ = [i for i, v in enumerate(b.g) if v]
    if not stones_:
        return [0.0] * 9
    score = [0.0] * 9
    for i, v in enumerate(b.g):
        if v:
            continue
        r, c = divmod(i, size)
        d = min(max(abs(r - t // size), abs(c - t % size)) for t in stones_)
        score[region_of(i, size)] += min(d, 3)
    return score


# The thresholds were MEASURED, not made up (see `python coach.py --big-points`):
# 418 graded moves from 10 saved games were scanned. (6.0, 2.0) won on every
# axis -- fires on 8%, average loss 5.04p when it fires and 1.48p when silent
# (3.4x separation), 11/43 of the mistakes above 5p, in 9/10 games.
# LIMIT: one player's 9x9 games. It should be measured again on 13x13/19x19
# and with other players -- like every constant in this project.
OPEN_THRESHOLD = 6.0
OPEN_RATIO = 2.0


def big_point(prior_, best_, played_move, size=DEFAULT_SIZE):
    """Detects "you played small while the big point was elsewhere".

    detection() found tactics (atari, liberties, captures) and was completely
    silent on strategic mistakes -- yet that is where a weak player loses the
    most. In the first ranked game tested, the `findings` of all six expensive
    moves were empty; the mistakes were not tactical, they were "not seeing the
    big point".

    It only speaks when two sources agree: the region the engine wanted must
    be clearly more open than the region played, by pure board math. Openness
    alone misleads in the endgame; "the engine wanted somewhere else" alone is
    already in the point loss. The new information is in the intersection.

    It can be called on a saved jsonl too (best_move is stored there), so it
    applies to old games retroactively -- the engine is not run again.
    """
    if not best_ or best_ == "pass" or not played_move or played_move == "pass":
        return None
    try:
        pi = region_of(from_gtp(played_move, size), size)
        ii = region_of(from_gtp(best_, size), size)
    except ValueError:
        return None
    if pi == ii:
        return None
    score = open_scores(replay(prior_, size))
    if score[ii] < OPEN_THRESHOLD or score[ii] < OPEN_RATIO * max(score[pi], 0.1):
        return None                       # stay silent in the endgame and on a balanced board
    return {"kind": "big_point", "empty": REGIONS[ii], "point": best_,
            "yours": REGIONS[pi]}


def detection(moves, mover, reply, size=DEFAULT_SIZE, best_=None):
    """Works out from the board WHY the move was bad.

    Point loss says 'how much'; these say 'what happened'. None of them ask the
    engine -- all cheap board math.
    """
    mv = moves[-1][1]
    if mv == "pass":
        return []
    stone = 1 if mover == "B" else -1
    idx = from_gtp(mv, size)
    b0, b1 = replay(moves[:-1], size), replay(moves, size)
    finding = []

    # 1. you put a stone in your own territory
    neighbor = list(b0.nbrs(idx))
    if neighbor and all(b0.g[n] == stone for n in neighbor):
        # Chinese rules: a stone counts as area too, the score does NOT change. The old
        # text said "costs 1 point" -- that is the Japanese rule. The loss is tempo and liberties.
        finding.append({"kind": "own_area"})

    # 2. my groups that FALL INTO atari with this move (ones already in atari do not count)
    def ataris(b):
        out, seen_ = {}, set()
        for i, v in enumerate(b.g):
            if v != stone or i in seen_:
                continue
            group_, libs = b.group(i)
            seen_ |= group_
            if len(libs) == 1:
                out[min(group_)] = (len(group_), next(iter(libs)))
        return out

    new = {k: v for k, v in ataris(b1).items() if k not in ataris(b0)}
    for dimension, liberty in new.values():
        # One liberty = if the opponent plays there the group is surely lost. No need
        # to wait for KataGo to CHOOSE that move; the threat is there either way.
        finding.append({"kind": "one_liberty", "dimension": dimension, "point": to_gtp(liberty, size)})
    if not new:
        group_, libs = b1.group(idx)
        if len(libs) == 2 and len(group_) >= 4:
            finding.append({"kind": "two_liberties", "dimension": len(group_)})

    # 3. does the opponent's best answer also capture something
    # 4. playing small while the big point is elsewhere (strategic)
    if best_:
        bn = big_point(moves[:-1], best_, mv, size)
        if bn:
            finding.append(bn)

    if reply and reply != "pass":
        b2 = replay(moves, size)
        previous = sum(1 for v in b2.g if v == stone)
        try:
            if b2.play(from_gtp(reply, size), -stone):
                k = previous - sum(1 for v in b2.g if v == stone)
                if k > 0:
                    finding.append({"kind": "capture", "point": reply, "num": k})
        except ValueError:
            pass
    return finding


# ---------- engine helpers ----------
def analyze_position(eng, moves, visits=None, size=None):
    """(root result, moveInfos sorted by order)"""
    size = size or eng.cfg.size
    r = eng.query(moves=moves, analyzeTurns=[len(moves)], boardXSize=size, boardYSize=size,
                  maxVisits=visits or eng.cfg.visits, includeOwnership=True)[0]
    return r, sorted(r["moveInfos"], key=lambda m: m["order"])


def selfplay(eng, profile, n_moves):
    """HumanSL self-play -- the source of lesson positions.
    No copyright issue: the positions are ours. Fast because maxVisits=1."""
    size = eng.cfg.size
    b, moves = Board(size), []
    for t in range(n_moves):
        col, stone = ("B", 1) if t % 2 == 0 else ("W", -1)
        for mv in eng.human_sample(moves, profile, k=6):
            if mv != "pass" and b.play(from_gtp(mv, size), stone):
                moves.append([col, mv])
                break
        else:
            b.play(None, stone)
            moves.append([col, "pass"])
    return moves


def teaching_position(eng, profile, n_moves=None, tries=4):
    """A teaching position: Black to play, with a meaningful gap between the best
    and the second-best move (i.e. it has one right answer)."""
    import random
    last = None
    for _ in range(tries + 2):
        # The number of moves scales with the board area. On 9x9, moves 12-24 are the
        # middle game; on 19x19 it is the very start of the opening -- every move
        # looks equal there, the "one right answer" condition never holds, and no
        # lesson position is found.
        scale = max(1, eng.cfg.size ** 2 // DEFAULT_SIZE ** 2)   # 9:1 13:2 19:4
        n = n_moves or random.choice([12, 16, 20, 24]) * scale   # even -> black
        moves = selfplay(eng, profile, n)
        root, mi = analyze_position(eng, moves)
        if not mi:
            continue
        last = (moves, root, mi)
        if len(mi) >= 2 and (mi[0]["scoreLead"] - mi[1]["scoreLead"]) >= 2.0:
            return last
    if last is None:
        raise RuntimeError(i18n.t("öğretici pozisyon üretilemedi",
                                 "could not produce a teaching position"))
    return last


# ---------- main grading ----------
def grade(eng, moves, mover, my_rank, next_rank, ladder=LADDER,
          force_level=False, size=None, komi=None, rule=None):
    """moves: the full list INCLUDING the move played. mover: 'B' / 'W'.

    size/komi/rule: the engine's settings if not given (live play). The archive
    and imports pass the game's own values -- the settings belong to the live
    game and are not changed."""
    size = size or eng.cfg.size
    n = len(moves) - 1
    extra = {"boardXSize": size, "boardYSize": size}
    if komi is not None:
        extra["komi"] = komi
    if rule is not None:
        extra["rules"] = rule
    before, after = eng.query(moves=moves, analyzeTurns=[n, n + 1], **extra,
                              maxVisits=eng.cfg.visits, includeOwnership=True)
    sign = 1 if mover == "B" else -1
    sl_b = sign * before["rootInfo"]["scoreLead"]
    sl_a = sign * after["rootInfo"]["scoreLead"]

    mi = sorted(before["moveInfos"], key=lambda m: m["order"])
    if not mi:
        raise RuntimeError(i18n.t("KataGo aday hamle döndürmedi (pozisyon dolu olabilir)",
                                 "KataGo returned no candidate moves (the board may be full)"))
    best = mi[0]

    # Measure the loss from a SINGLE search when possible. Subtracting two
    # separate before/after searches mixed the searches' evaluation drift into
    # the move's cost: the engine's own first choice showed up as -2.9p.
    #
    # The `before-after` fallback path was VERIFIED (2026-09-07, measurement run):
    #  * on 58 moves where single-search could also be computed, mean absolute
    #    difference 0.16p.
    #  * The real evidence: for the 7 moves that fell back, a SEPARATE 613-visit
    #    search was run on the played move with allowMoves (an independent
    #    measurement) -- it agreed with before-after within 0.2p on average.
    #  * the visits threshold was verified too: error 0.11p in the 10-30 visit
    #    range, 0.14p at 300+ visits. Raising the threshold buys nothing.
    # So this path is NOT noisy; it should not be changed as "suspicious".
    #
    # Putting {best, played} into the same tree with `allowMoves` and subtracting
    # from there was tried and FAILED: allowMoves restricts which moves are LEGAL
    # at the root, not how visits are DISTRIBUTED. PUCT gave the move it found bad
    # 1 of 613 visits -- so a 613-visit evaluation was being compared with a
    # 1-visit one.
    played_move = next((m for m in mi if m["move"] == moves[-1][1]), None)
    if played_move is not None and played_move["visits"] >= 10:
        loss = max(0.0, sign * (best["scoreLead"] - played_move["scoreLead"]))
        measurement = "single-search"
    else:
        loss = max(0.0, sl_b - sl_a)
        measurement = "before-after"

    prior = moves[:-1]
    curve = level_curve(eng, prior, mi, sign, ladder, size)
    # One gate: no estimate is PRODUCED on an uncalibrated size. Everything
    # downstream (the record, the level strip in the charts, game_level, the brief,
    # the quality signal in ranked placement) goes quiet on its own because of it
    # -- no need for a separate gate in every consumer. `force_level`: only for
    # calibration, which cannot calibrate without measuring.
    estimate = (level_estimate(loss, curve)
              if (force_level or size in LEVEL_CALIBRATED) else None)
    mine = dict(curve).get(my_rank)

    # The 0-10 grade (TAU decay) was REMOVED: the constant was never calibrated and
    # it produced "10/10 but 1.2p lost" contradictions on screen. Quality = category(loss).

    ownership_d = [0.0] * 9
    for i, (a, b) in enumerate(zip(after["ownership"], before["ownership"])):
        ownership_d[region_of(i, size)] += sign * (a - b)
    ri = max(range(9), key=lambda k: abs(ownership_d[k]))

    after_q = sorted(after["moveInfos"], key=lambda m: m["order"])
    reply = after_q[0]["move"] if after_q else "pass"

    bottom = [{"move": m["move"],
            "diff": round(sign * (best["scoreLead"] - m["scoreLead"]), 1),
            "continuation": m["pv"][:3]} for m in mi[:3]]

    return {
        "move": moves[-1][1], "mover": mover,
        "points_lost": round(loss, 1), "measurement": measurement,
        "level_estimate": estimate,
        "level_curve": [(r, round(k, 2)) for r, k in curve],
        "your_level_expected_loss": None if mine is None else round(mine, 2),
        "best_move": best["move"],
        "opponent_reply": reply,
        "alternatives": bottom,
        "findings": detection(moves, mover, reply, size, best["move"]),
        "katago_line": best["pv"][:4],
        "prev_score_lead": round(sl_b, 1),
        "next_score_lead": round(sl_a, 1),
        "most_changed_region": REGIONS[ri],
        "region_change_points": round(ownership_d[ri], 1),
        "your_level_plays": eng.human_top(prior, f"rank_{my_rank}", size=size),
        "next_level_plays": eng.human_top(prior, f"rank_{next_rank}", size=size),
    }


# ---------- move category (OGS-style six groups) ----------
# The ONLY measure of move quality (the 0-10 grade was removed): straight from
# the loss. Thresholds are size INDEPENDENT -- a point is a point at every size.
# In the archive 13x13 losses are higher than 9x9 ones, but that is the game
# itself, not the scale.
# ponytail: fixed thresholds; if the category ratios mislead players, re-pick them from the archive distribution.
CATEGORIES = ("excellent", "great", "good", "inaccuracy", "mistake", "blunder")
CATEGORY_THRESHOLD = ((1.0, "good"), (3.0, "inaccuracy"), (6.0, "mistake"))
SINGLE_MOVE_GAP = 3.0      # if the second option is this far behind, "the only right move"
CATEGORY_COLOR = {"excellent": "#3b9ad9", "great": "#1fa35c", "good": "#6aa82a",
                 "inaccuracy": "#e8b923", "mistake": "#f08c3a", "blunder": "#e5484d"}
_CATEGORY_NAME = {"excellent": ("Mükemmel", "Excellent"), "great": ("Harika", "Great"),
                "good": ("İyi", "Good"), "inaccuracy": ("Kusurlu", "Inaccuracy"),
                "mistake": ("Hata", "Mistake"), "blunder": ("Blunder", "Blunder")}


def category_name(k):
    return i18n.t(*_CATEGORY_NAME[k])


def category(r):
    """Grading record -> one of CATEGORIES. None if not graded."""
    loss = r.get("points_lost") if r else None
    if loss is None:
        return None
    bottom = r.get("alternatives") or []
    first = bool(r.get("move")) and r.get("move") == r.get("best_move")
    if first and len(bottom) >= 2 and bottom[1].get("diff", 0) >= SINGLE_MOVE_GAP:
        return "great"
    if first or loss <= 0.2:
        return "excellent"
    for threshold, name in CATEGORY_THRESHOLD:
        if loss < threshold:
            return name
    return "blunder"


def category_count(records):
    """{category: count} -- every category, zeros included."""
    say = dict.fromkeys(CATEGORIES, 0)
    for r in records:
        k = category(r)
        if k:
            say[k] += 1
    return say


def game_level(records):
    """Game-wide level estimate. A single move is noisy; the real signal is here."""
    d = [r["level_estimate"]["value"] for r in records
         if r.get("level_estimate") and r["level_estimate"].get("value") is not None]
    if not d:
        return None
    d.sort()
    median = d[len(d) // 2] if len(d) % 2 else (d[len(d) // 2 - 1] + d[len(d) // 2]) / 2
    return {"label": value_rank(median), "value": round(median, 1), "move_count": len(d)}


# ---------- end of game: area counting (Chinese rules) ----------
# The engine is queried with `rules: chinese`; explaining the score must use
# the same rule. Area = living stones + surrounded empty points + the dead
# opponent stones in them. Captures do NOT count; a stone in your own
# territory is 0 points.
OWNERSHIP_THRESHOLD = 0.5        # below this |ownership| an empty point belongs to nobody


def captures(moves, size=DEFAULT_SIZE):
    """{'B': white stones captured by Black, 'W': black stones captured by White}."""
    b, e = Board(size), {"B": 0, "W": 0}
    for col, mv in moves:
        if col in goban.SETUP:
            b.g[from_gtp(mv, size)] = goban.color(col)
            continue
        opponent = -1 if col == "B" else 1
        earlier = b.g.count(opponent)
        b.play(None if mv == "pass" else from_gtp(mv, size), -opponent)
        e[col] += earlier - b.g.count(opponent)
    return e


def area_count(b, ownership=None, komi=KOMI):
    """Chinese-rules count. ownership: KataGo ownership (Black's view, [-1, 1]).

    A stone counts as dead only if its ownership has turned CLEARLY to the
    opponent. Without ownership, empty regions are flood-filled: a region
    touching only one color belongs to that color.
    ponytail: flood fill does NOT remove dead stones -- a dead stone in the
    opponent's area makes the region 'nobody's'. With an engine available the
    ownership path is always used.
    """
    if ownership:
        owner_ = []
        for v, o in zip(b.g, ownership):
            s = 1 if o >= OWNERSHIP_THRESHOLD else -1 if o <= -OWNERSHIP_THRESHOLD else 0
            owner_.append(s if not v or s == -v else v)
    else:
        owner_, seen_ = list(b.g), set()
        for i, v in enumerate(b.g):
            if v or i in seen_:
                continue
            region, color, st = {i}, set(), [i]
            while st:
                for k in b.nbrs(st.pop()):
                    if b.g[k]:
                        color.add(b.g[k])
                    elif k not in region:
                        region.add(k)
                        st.append(k)
            seen_ |= region
            for k in region:
                owner_[k] = next(iter(color)) if len(color) == 1 else 0

    say = {1: {"stone": 0, "region": 0, "dead": 0}, -1: {"stone": 0, "region": 0, "dead": 0}}
    neutral = 0
    for v, s in zip(b.g, owner_):
        if not s:
            neutral += 1
        else:
            say[s]["stone" if v == s else "dead" if v else "region"] += 1
    for d in say.values():
        d["total"] = d["stone"] + d["region"] + d["dead"]
    return {"black": say[1], "white": say[-1], "neutral": neutral, "komi": komi,
            "diff": say[1]["total"] - say[-1]["total"] - komi,
            "method": "ownership" if ownership else "approximate"}


def final_count(eng, moves, size=DEFAULT_SIZE, komi=KOMI):
    """Count + captures of a finished game. Approximate if eng is None or the query fails."""
    ownership = None
    if eng is not None:
        try:
            ownership = analyze_position(eng, moves, size=size)[0].get("ownership")
        except Exception:      # the count is an explanation; an engine error must not lock the game
            pass
    s = area_count(replay(moves, size), ownership, komi)
    s["capture"] = captures(moves, size)
    return s


def side_names(mode):
    """Side names in score texts. In a two-player game there is a human on both
    sides of the board: "you"/"bot" and "You won" are wrong there."""
    return (i18n.t("Siyah", "Black"), i18n.t("Beyaz", "White")) if mode == "duo" else None


def count_lines(s, lead=None, sides=None):
    """For the end-of-game screen and the archive: what the score is based on."""
    b, w, komi = s["black"], s["white"], s["komi"]
    sy, bz = sides or (i18n.t("Siyah (sen)", "Black (you)"),
                          i18n.t("Beyaz (bot)", "White (bot)"))
    t = [i18n.t("Skor nasıl çıktı · Çin kuralı (alan sayımı)",
               "How the score came out · Chinese rules (area scoring)"),
         i18n.t(f"{sy}: {b['stone']} taş + {b['region']} boş nokta",
               f"{sy}: {b['stone']} stones + {b['region']} empty points")
         + (i18n.t(f" + {b['dead']} ölü beyaz taş", f" + {b['dead']} dead white stones")
            if b["dead"] else "") + f" = {b['total']}",
         i18n.t(f"{bz}: {w['stone']} taş + {w['region']} boş nokta",
               f"{bz}: {w['stone']} stones + {w['region']} empty points")
         + (i18n.t(f" + {w['dead']} ölü siyah taş", f" + {w['dead']} dead black stones")
            if w["dead"] else "")
         + f" + {komi:g} komi = {w['total'] + komi:g}"]
    if s["neutral"]:
        t.append(i18n.t(f"Kimsenin değil (dame/seki): {s['neutral']} nokta",
                       f"Nobody's (dame/seki): {s['neutral']} points"))
    t.append(i18n.t(f"Sayım: {b['total']} − {w['total'] + komi:g} = {s['diff']:+g}",
                   f"Count: {b['total']} − {w['total'] + komi:g} = {s['diff']:+g}")
             + (i18n.t(f"   ·   KataGo tahmini {lead:+.1f}",
                      f"   ·   KataGo estimate {lead:+.1f}") if lead is not None else ""))
    if s.get("capture"):
        t.append(i18n.t(
            f"Esir: {sy} {s['capture']['B']} taş aldı, {bz} {s['capture']['W']}. "
            "Çin kuralında esir puan etmez, taşın yeri alanına geçer.",
            f"Captures: {sy} took {s['capture']['B']} stones, {bz} {s['capture']['W']}. "
            "Under Chinese rules captures score nothing; the vacated point becomes area."))
    if s["method"] != "ownership":
        t.append(i18n.t("(Yaklaşık: motor yoktu, ölü taşlar ayıklanmadı.)",
                       "(Approximate: no engine, dead stones were not removed.)"))
    t.append(i18n.t("Ayrıntı: Ayarlar → Skor kuralları",
                   "Details: Settings → Scoring rules"))
    return t


# The game result is Elo's S: 1 won, 0.5 draw, 0 lost. It used to be a bool,
# and because a draw does not fit in a bool it counted as a loss. True/False in
# old records equal 1/0 in Python -- dict lookups work unchanged. The keys
# (1.0/0.5/0.0 and "counted"/"estimate"/...) are data stored in the RECORD;
# only the displayed text is translated. A FUNCTION, not a dict: a constant at
# module top level would freeze in the language active at import (see i18n.py).
_RESULT = {1.0: ("Kazandın", "You won"), 0.5: ("Berabere", "Draw"),
          0.0: ("Kaybettin", "You lost")}
_SOURCE = {"counted": (" (sayım)", " (counted)"),
           "estimate": (" (KataGo tahmini: ölü taşlar ayıklanamadı)",
                      " (KataGo estimate: dead stones could not be removed)"),
           "approximate_count": (" (yaklaşık sayım)", " (approximate count)")}


def result_label(won):
    """1.0/0.5/0.0 (and True/False in old records) -> label in the selected language."""
    return i18n.t(*_RESULT[won])


def game_result(count=None, lead=None, resign=False):
    """(S, source, diff) -- from Black's (the player's) point of view.

    The COUNT decides the winner, not the estimate. But only a count made with
    the ownership map is reliable: flood fill cannot remove dead stones and was
    off by up to 27 points in 8 games. In that case, KataGo's estimate (scoreLead).
    """
    if resign:
        return 0.0, "resign", None
    if count and count.get("method") == "ownership":
        diff, source = count["diff"], "counted"
    elif lead is not None:
        diff, source = lead, "estimate"
    elif count:
        diff, source = count["diff"], "approximate_count"
    else:
        return None, None, None
    return (1.0 if diff > 0 else 0.0 if diff < 0 else 0.5), source, diff


def result_sentence(S, source, diff, sides=None):
    """Arguments in the order game_result returns them: result_sentence(*game_result(...)).

    If `sides` is given (two-player game) the result is written from the
    board's point of view, not the player's: "Black won", not "You won"."""
    if sides:
        start = (i18n.t("Berabere", "Draw") if S == 0.5 else
               i18n.t(f"{sides[0 if S == 1.0 else 1]} kazandı",
                     f"{sides[0 if S == 1.0 else 1]} won"))
    else:
        start = result_label(S)
    if source == "resign":
        return start + i18n.t(" — pes edildi.", " — by resignation.")
    if source == "clock":
        return start + i18n.t(" — süre doldu.", " — on time.")
    sy, bz = sides or (i18n.t("Siyah (sen)", "Black (you)"),
                          i18n.t("Beyaz (bot)", "White (bot)"))
    who = sy if diff > 0 else bz
    what = (i18n.t("eşit skor", "level score") if S == 0.5 else
          i18n.t(f"{who} {abs(diff):.1f} puan önde", f"{who} ahead by {abs(diff):.1f} points"))
    return f"{start} — {what}{i18n.t(*_SOURCE[source])}"


def rules(komi=KOMI):
    """The 'Scoring rules' page (markdown). Constants are read from the code so the text cannot go stale.

    The WHOLE page in two languages, not line by line: with two full texts side
    by side instead of 30 separate i18n.t() calls, a missing translation is
    visible while reading.
    """
    return i18n.t("\n".join([
        "# Skor nasıl hesaplanıyor",
        f"Çin kuralı (alan sayımı) · komi {komi:g}",
        "",
        "Oyun üst üste iki pasla biter. Sonra her renk için:",
        "",
        "- **Alan** = yaşayan taşlar + tamamen çevrilen boş noktalar + çevrilen "
        "bölgedeki ölü rakip taşları",
        f"- **Skor** = siyah alan − beyaz alan − {komi:g} (komi)",
        "",
        f"**Komi:** ilk hamle siyahın; bu avantajı dengelemek için beyaza {komi:g} puan. "
        "Yarım puanlı komiyle beraberlik olmaz. (Komi 7 ile oynanmış eski maçlar kendi "
        "komisiyle sayılır; onlarda beraberlik mümkün.)",
        "",
        "## Sık sorulanlar",
        "",
        "**Esir aldığım taş puan mı?**  ",
        "Doğrudan değil, alan sayımında esir sayılmaz. Ama aldığın taşın yeri artık "
        "senin alanın; puan oradan gelir.",
        "",
        "**Kendi alanıma taş koyarsam eksi mi yerim?**  ",
        "Hayır: taş da alan sayılır, puan değişmez. (Japon kuralında −1 olurdu.) Ama "
        "bedava değil: o hamleyi puan eden bir yerde oynayabilirdin (tempo) ve grubunun "
        "bir nefesini kapattın. Hoca bu yüzden uyarıyor.",
        "",
        "**Rakibin alanına taş koyarsam?**  ",
        "Taş ölürse rakibe sayılır, sana 0. Yaşarsa o bölgeyi bozmuş olursun.",
        "",
        "**Dame (iki renge de değen boş nokta)?**  ",
        "Kimseye sayılmaz. Doldurursan +1, çünkü taşın alan olur. Pas geçmeden önce "
        "dame'leri doldur.",
        "",
        "**Seki (birbirini alamayan gruplar)?**  ",
        "Aradaki ortak nefesler kimseye sayılmaz.",
        "",
        "**Ölü taşlar?**  ",
        "Pastan sonra tahtada kalan ama kurtulamayacak taşlar. Maç sonu sayımında "
        "KataGo'nun sahiplik haritasıyla ayıklanır.",
        "",
        "## Ekrandaki sayılar",
        "",
        "- **Skor +34.6:** KataGo'nun tahmini (scoreLead). Oyun iyi oynanarak sürseydi "
        "beklenen fark; maç içi gösterge.",
        "- **Maç sonu sayımı:** tahtadaki alan sayımı. Kazananı bu belirler. Ölü taşlar "
        "ayıklanamazsa KataGo tahmini kullanılır.",
        "- **3.2p kayıp:** hamlenin skoru, motorun en iyi hamlesine göre ne kadar düşürdüğü.",
        f"- **Kategori:** Mükemmel (motorun ilk tercihi ya da ≤0.2p), Harika (tek doğru "
        f"hamle), İyi (<{CATEGORY_THRESHOLD[0][0]:g}p), Kusurlu (<{CATEGORY_THRESHOLD[1][0]:g}p), "
        f"Hata (<{CATEGORY_THRESHOLD[2][0]:g}p), Blunder (≥{CATEGORY_THRESHOLD[2][0]:g}p).",
        "",
        "## Dereceli (SP)",
        "",
        f"- İlk {PLACEMENT_GAMES} maç yerleştirme: SP yok, rank belirlenir.",
        f"- Sonra {SP_RANK} SP = bir rank. Puan farkı SP'ye girmez, yalnız sonuç.",
        "- Kazanınca +K·(1−E), berabere K·(½−E), kaybedince −K·E.",
        "  E = bu rakibe karşı beklenen kazanma olasılığın. Güçlü rakibi",
        "  yenmek çok SP kazandırır, ona kaybetmek az kaybettirir.",
        f"- K = {K_PROVISIONAL} ilk {PROVISIONAL_GAMES} maçta (provizyonel), sonra {K}.",
        "- Pes = mağlubiyet.",
        "- 2+ maçlık galibiyet serisi daha güçlü rakip getirir. Beraberlik",
        "  seriyi ne uzatır ne bozar.",
    ]), "\n".join([
        "# How the score is computed",
        f"Chinese rules (area scoring) · komi {komi:g}",
        "",
        "The game ends on two consecutive passes. Then, for each colour:",
        "",
        "- **Area** = living stones + fully surrounded empty points + dead enemy "
        "stones inside the surrounded region",
        f"- **Score** = black area − white area − {komi:g} (komi)",
        "",
        f"**Komi:** black moves first; white gets {komi:g} points to offset that "
        "advantage. A half-point komi makes draws impossible. (Older games played "
        "with komi 7 are counted with their own komi; those can be drawn.)",
        "",
        "## FAQ",
        "",
        "**Does a stone I capture score a point?**  ",
        "Not directly — captures do not count in area scoring. But the point the "
        "captured stone vacated is now your area; that is where the point comes from.",
        "",
        "**Do I lose a point for playing inside my own area?**  ",
        "No: a stone counts as area too, so the score does not change. (Under Japanese "
        "rules it would be −1.) It is not free, though: you could have played that move "
        "somewhere it scored (tempo), and you took away one of your group's liberties. "
        "That is why the coach warns you.",
        "",
        "**What if I play inside my opponent's area?**  ",
        "If the stone dies it counts for them and nothing for you. If it lives, you "
        "have broken up that region.",
        "",
        "**Dame (an empty point touching both colours)?**  ",
        "It counts for nobody. Filling it is +1, because your stone becomes area. "
        "Fill the dame before you pass.",
        "",
        "**Seki (groups that cannot capture each other)?**  ",
        "The shared liberties between them count for nobody.",
        "",
        "**Dead stones?**  ",
        "Stones still on the board after the passes that cannot be saved. They are "
        "removed in the final count using KataGo's ownership map.",
        "",
        "## The numbers on screen",
        "",
        "- **Score +34.6:** KataGo's estimate (scoreLead). The gap expected if the game "
        "continued with good play; an in-game indicator.",
        "- **Final count:** area scoring on the board. This decides the winner. If dead "
        "stones cannot be removed, KataGo's estimate is used instead.",
        "- **3.2p lost:** how much the move dropped the score against the engine's best move.",
        f"- **Category:** Perfect (the engine's first choice or ≤0.2p), Great (the only "
        f"right move), Good (<{CATEGORY_THRESHOLD[0][0]:g}p), Inaccuracy "
        f"(<{CATEGORY_THRESHOLD[1][0]:g}p), Mistake (<{CATEGORY_THRESHOLD[2][0]:g}p), "
        f"Blunder (≥{CATEGORY_THRESHOLD[2][0]:g}p).",
        "",
        "## Ranked (SP)",
        "",
        f"- The first {PLACEMENT_GAMES} games are placement: no SP, they set your rank.",
        f"- After that {SP_RANK} SP = one rank. The point margin does not affect SP, "
        "only the result does.",
        "- A win is +K·(1−E), a draw K·(½−E), a loss −K·E.",
        "  E = your expected win probability against this opponent. Beating a strong",
        "  opponent earns a lot of SP; losing to them costs little.",
        f"- K = {K_PROVISIONAL} for the first {PROVISIONAL_GAMES} games (provisional), "
        f"then {K}.",
        "- Resigning = a loss.",
        "- A 2+ game win streak brings a stronger opponent. A draw neither extends",
        "  nor breaks the streak.",
    ]))


# ---------- ranked games: skill points (SP) and rank ----------
# Elo, but the D constant was NOT made up: it was measured with bot-vs-bot
# games (calibration run, 2026-09-07). Standard Elo uses D=400; with D=400 a
# 5-rung gap predicts 95% wins, the measured value is 73%. Because the HumanSL
# ladder is ~2.7x more compressed than real Go ranks, D came out at 1090.
# Measured with komi 7.0; the switch to 7.5 only removed half a point, and
# alternating colors already cancelled the komi bias.
SP_RANK = 100      # one rung = 100 SP. The EGF GoR system works this way too.
K = 20             # weight of one game
D = 1090           # 9x9, MEASURED -- rerun the calibration before changing it

# D MEASURED per size. 13x13: 180 bot-vs-bot games, 2026-09-16, 6/6 pairs in
# the right direction and all significant (2.6-13.2 sigma) -- cleaner than 9x9
# (9/12 significant). D=730: the ladder is WIDER on 13x13 than on 9x9; a larger
# board has room to show skill differences. A size not in the list falls back
# to 9x9's. 19x19: 180 games, 2026-09-16, 6/6 in the right direction, all
# significant (2.8-18 sigma). D=460 -- same method (squared error on win rate;
# it reproduces 730 exactly on 13x13). The likelihood method says 550; that is
# the size of the uncertainty. As the board grows D approaches standard Elo's
# 400: HumanSL was trained on 19x19 human games, so its rank range is most
# realistic there.
D_BY_SIZE = {DEFAULT_SIZE: D, 13: 730, 19: 460}

# PROVISIONAL K -- found by simulation, not by guessing.
# Placement leaves a +-4 rank error (3 games cannot resolve better). With K=20
# a badly placed player needed a MEDIAN OF 158 GAMES to recover; the reason is
# plain arithmetic: someone placed 11 rungs too low wins 91% but only gains
# +8.2 SP per game against an equal opponent, and 1100 SP = ~134 games.
# Because the ladder is compressed (D=1090), K=20 is too small.
# K=80 for the first 30 games -> median 64 games (2.5x). It does NOT disturb
# the steady state because it reverts to K=20 when it expires: at the right
# rank the drift is 0.5r, the same as with K=20. A cheap counterpart of
# Glicko's rating deviation: while new, we are not sure.
K_PROVISIONAL = 80
PROVISIONAL_GAMES = 30

# HumanSL profiles are 20k..9d. Below 20k does NOT come from the engine: the
# 20k profile is weakened with temperature (bot_setting). Reason (2026-09-16):
# a player lost 2/3 against 20k on 13x13, placement locked to the floor after
# the first loss, and the remaining games carried no information.
HUMAN_FLOOR = rank_value("20k")
WEAKEST = rank_value("30k")          # the lowest across all sizes; the per-size floor is weakest()
STRONGEST = rank_value("9d")
# The first placement game is 20k -- a deliberate choice. Meeting a new player
# with 30k just because the floor went down would be wrong; if they lose, the
# search goes down.
PLACEMENT_START = HUMAN_FLOOR

# BOTS BELOW 20k: the 20k profile with temperature T > 1 over the WHOLE
# distribution (no tail pruning).
# MEASURED (2026-09-17, calibration run): each T played 30 games DIRECTLY
# against a 20k (reference) bot; win rates were smoothed with an isotonic fit
# (a bot cannot get stronger as T rises) and turned into rungs with that size's
# D (weak_table). Table: (T, how many rungs weaker than 20k). (1.0, 0): full
# distribution sampling counts as 20k -- an 18-game pre-measurement showed it
# is not weaker (won 12/18).
#
#   9x9  (D=1090) T 1.1:3.2  1.2:2.4  1.3:7.2  1.4:6.3  1.5:11.3 / 14.1 (x2 runs)
#   13x13 (D=730) T 1.1:2.2  1.2:4.8  1.3:3.0  1.5:4.8  1.7:6.5  2.0:7.6  2.5:5.7  3.0:6.5
#   19x19 (D=460) T 1.1:3.0  1.2:1.9  1.3:2.3  1.5:4.8  1.7:6.1  2.0:1.9/3.0  2.5:3.0  3.0:3.0
#   (single-measurement error +-0.8..4 rungs)
#
# THE KNOB SATURATES ON LARGER BOARDS: above T=1.7 on 13x13 and above T=1.5 on
# 19x19 it does NOT produce a weaker bot (the win rate stays at 10-17%). So
# the floor depends on size: no rank LABEL below the weakest measured point --
# writing "30k" while playing a 23k would be a lie. Going lower needs another
# knob (for example mixing in random moves); none was added without measuring.
WEAK_MEASURED = {
    9: [(1.0, 0.0), (1.1, 2.88), (1.3, 6.86), (1.5, 13.11)],
    13: [(1.0, 0.0), (1.1, 2.17), (1.2, 3.93), (1.5, 4.79), (1.7, 6.54), (2.0, 6.74)],
    19: [(1.0, 0.0), (1.1, 2.46), (1.5, 3.49)],
}


def _weak_measured(size):
    return WEAK_MEASURED.get(size, WEAK_MEASURED[DEFAULT_SIZE])


def weakest(size=DEFAULT_SIZE):
    """The weakest bot level measured at this size: 9x9 30k, 13x13 26k, 19x19 23k."""
    return max(WEAKEST, HUMAN_FLOOR - int(_weak_measured(size)[-1][1]))


def bot_setting(value, size=DEFAULT_SIZE, setting=None):
    """Opponent level -> (humanSL profile, human_sample setting).

    20k and up: its own profile, setting unchanged. Below: the 20k profile with
    a temperature linearly interpolated from the measured table. An unmeasured
    size falls back to 9x9.
    """
    setting = dict(setting or {})
    if value >= HUMAN_FLOOR:
        return f"rank_{value_rank(value)}", setting
    measurement = _weak_measured(size)
    diff = HUMAN_FLOOR - max(value, weakest(size))
    t = measurement[-1][0]
    for (t1, f1), (t2, f2) in zip(measurement, measurement[1:]):
        if diff <= f2:
            t = t1 + (t2 - t1) * (diff - f1) / (f2 - f1)
            break
    return f"rank_{value_rank(HUMAN_FLOOR)}", {**setting, "temp": round(t, 3), "tail": 1.0}
# The placement ceiling is the top of the LADDER: calibration showed that
# 2k:1d already struggles, and above it the search-free human net is meaningless.
PLACEMENT_CAP = rank_value("1d")
PLACEMENT_GAMES = 3
# 3 games only resolve +-4 ranks. Measured: a one-rung gap produces a 72% win
# rate and a single game's standard deviation is ~20 points. That is not a
# flaw, it is the physics of noise -- and the reason the SP ladder exists.
PLACEMENT_UNCERTAINTY = 4

# Placement games do not ONLY produce won/lost -- each game also produces a
# move-quality estimate (game_level), which is far more informative.
# MEASURED (calibration run): after the offset correction a single game's
# quality estimate is UNBIASED (mean -0.16 rank) with a spread of 3.16 ranks.
# The binary search's own error is ~4 ranks (measured by simulation).
# Inverse-variance weighting: quality gets ~83%, the binary search ~17%, and
# the combined error drops to ~1.7 ranks.
#
# NOTE: this is move quality, NOT the score margin. Rewarding the margin
# teaches greedy play; the only way to raise move quality is to play better.
QUALITY_SD = 3.16       # spread of a single game's game_level estimate
BINARY_SD = 4.0         # error of the 3-game won/lost binary search

# A streak does not give SP, it gives the OPPONENT. The Elo weight already
# rewards beating that stronger opponent more: the two mechanisms do not stack,
# they chain. The steps are large because the ladder is compressed -- +1 rung
# only lifts the SP from 10.0 to 10.6, +8 rungs to 16.9.
STREAK_STEP = (0, 0, 2, 4, 6, 8)


def crop(value, bottom=None, upper=None, size=DEFAULT_SIZE):
    return max(weakest(size) if bottom is None else bottom,
               min(STRONGEST if upper is None else upper, value))


def win_prob(diff, d=D):
    """Win probability. diff: how many rungs STRONGER (+) / weaker (-) the opponent is.
    d: this size's Elo constant (d_const(size)); 9x9's if not given."""
    return 1 / (1 + 10 ** (SP_RANK * diff / d))


def k_factor(ranked_game):
    """K by the number of ranked games played since placement."""
    return K_PROVISIONAL if ranked_game < PROVISIONAL_GAMES else K


def sp_change(S, diff, k=K, d=D):
    """The SP worth of one game: Elo's k*(S-E). S: 1 / 0.5 / 0.

    The score margin does NOT enter -- real Go rating systems (EGF GoR, OGS
    Glicko-2, KGS) only look at won/lost too. Reason: good players shrink the
    margin ON PURPOSE once ahead; taking risks is bad Go. A system that rewards
    the margin teaches greedy play.
    """
    return round(k * (float(S) - win_prob(diff, d)), 1)


def apply_sp(value, sp, S, opponent_value, ranked_game=PROVISIONAL_GAMES, d=D,
           size=DEFAULT_SIZE):
    """Apply a game result. Returns: (new_value, new_sp, sp_change).

    ranked_game: the number of the ranked game since placement (for the provisional K).

    The (rank, sp) pair is kept as one continuous SP number; that way promotion
    and demotion run through the same code, with no sign errors.
    """
    diff_sp = sp_change(S, opponent_value - value, k_factor(ranked_game), d)
    total = value * SP_RANK + sp + diff_sp
    total = max(weakest(size) * SP_RANK, min(STRONGEST * SP_RANK, total))
    new, remaining = divmod(total, SP_RANK)
    return int(new), round(remaining, 1), diff_sp


def opponent_choice(value, streak, size=DEFAULT_SIZE):
    """The level of the bot you face in a ranked game."""
    return crop(value + STREAK_STEP[min(streak, len(STREAK_STEP) - 1)], size=size)


def placement_opponent(history, size=DEFAULT_SIZE):
    """The next placement opponent. history: [(opponent_value, S)], S 1 / 0.5 / 0.

    The first game is a fixed 20k (the lowest rung) -- a deliberate choice; on
    a platform the user would be asked their level and start from there.
    After that, a binary search: a win raises the floor, a loss lowers the
    ceiling, a draw brings both to that point.
    """
    if not history:
        return PLACEMENT_START
    bottom, upper = weakest(size), PLACEMENT_CAP
    for d, S in history:
        if S >= 0.5:
            bottom = max(bottom, d)
        if S <= 0.5:
            upper = min(upper, d)
    return bottom if bottom >= upper else int(round((bottom + upper) / 2))


def placement_result(history, quality=None, size=DEFAULT_SIZE):
    """Rank once placement is over.

    history: [(opponent_value, won)] -- the binary search signal
    quality: [game_level value, ...] -- the move-quality signal (may be None)

    The two independent signals are combined with inverse-variance weights:
    each gets a say in proportion to its precision. Without a quality signal
    (old record, or no game produced an estimate) it falls back to the old behavior.
    """
    binary = placement_opponent(history, size)
    k = [x for x in (quality or []) if x is not None]
    if not k:
        b = PLACEMENT_UNCERTAINTY
        return {"value": binary, "label": value_rank(binary), "uncertainty": b,
                "source": "binary-search",
                "spacing": (value_rank(crop(binary - b, size=size)),
                           value_rank(crop(binary + b, size=size)))}

    k_mean = sum(k) / len(k)
    w_binary = 1.0 / BINARY_SD ** 2
    w_quality = len(k) / QUALITY_SD ** 2
    d = crop((binary * w_binary + k_mean * w_quality) / (w_binary + w_quality), size=size)
    b = (w_binary + w_quality) ** -0.5
    return {"value": int(round(d)), "label": value_rank(d),
            "uncertainty": round(b, 1), "source": "binary+quality",
            "binary": value_rank(binary), "quality": value_rank(k_mean),
            "quality_games": len(k),
            "spacing": (value_rank(crop(d - b, size=size)), value_rank(crop(d + b, size=size)))}


# ---------- findings: stored as structure, text made when shown ----------
# detection() used to produce a Turkish sentence and the jsonl stored the
# SENTENCE: when the language changed, old games' findings stayed in Turkish.
# Now {"kind", ...fields} is stored; the text comes from finding_text() in the
# selected language. Sentences in old records are parsed by finding_struct()
# -- the files are not rewritten.
REGION_EN = {"top_left": "top left", "top": "top", "top_right": "top right",
            "left": "left", "center": "center", "right": "right",
            "bottom_left": "bottom left", "bottom": "bottom", "bottom_right": "bottom right"}

_OLD_FINDING = [
    ("one_liberty", re.compile(r"(?P<dimension>\d+) taşlık grubunun TEK nefesi kaldı \((?P<point>\w+)\)")),
    ("two_liberties", re.compile(r"(?P<dimension>\d+) taşlık grubunun sadece 2 nefesi kaldı")),
    ("capture", re.compile(r"Rakip (?P<point>\w+) oynayarak (?P<num>\d+) taşını esir alıyor")),
    ("own_area", re.compile(r"Bu nokta zaten senin bölgendi")),
    ("big_point", re.compile(r"asıl boş yeri (?P<empty>.+?) bölgesiydi ve motorun istediği "
                               r"(?P<point>\w+) orada\. Sen (?P<yours>.+?) bölgesinde")),
]


# Region names on screen. REGIONS are written into records as keys (ASCII);
# the display uses the Turkish spelling.
REGION_SHORT = {"top_left": "sol üst", "top": "üst", "top_right": "sağ üst", "left": "sol",
              "center": "merkez", "right": "sağ", "bottom_left": "sol alt", "bottom": "alt",
              "bottom_right": "sağ alt"}
_REGION_BACK = {v: k for k, v in REGION_SHORT.items()} | dict(zip(
    ["sol ust", "ust", "sag ust", "sol", "merkez", "sag", "sol alt", "alt", "sag alt"], REGIONS))


def region_name(b):
    return i18n.t(REGION_SHORT.get(b, b), REGION_EN.get(b, b))


def finding_struct(b):
    """Finding -> {"kind", ...}. A new record is already a structure; an old record
    is a Turkish sentence, parsed from the format we produced ourselves.
    Unrecognized sentence -> None."""
    if isinstance(b, dict):
        return b
    for kind, rx in _OLD_FINDING:
        m = rx.search(b or "")
        if m:
            return {"kind": kind, **{k: int(v) if v.isdigit() else _REGION_BACK.get(v, v)
                                   for k, v in m.groupdict().items()}}
    return None


def finding_text(b):
    """The finding's sentence in the selected language. An unrecognized old sentence is returned as is."""
    y = finding_struct(b)
    if not y:
        return b if isinstance(b, str) else ""
    kind = y["kind"]
    if kind == "one_liberty":
        return i18n.t(f"{y['dimension']} taşlık grubunun TEK nefesi kaldı ({y['point']}). "
                 f"Rakip istediği an oraya oynayıp hepsini alabilir.",
                 f"Your {y['dimension']}-stone group is down to ONE liberty ({y['point']}). "
                 f"Your opponent can capture it any time by playing there.")
    if kind == "two_liberties":
        return i18n.t(f"{y['dimension']} taşlık grubunun sadece 2 nefesi kaldı, dikkat.",
                 f"Your {y['dimension']}-stone group has only 2 liberties left. Careful.")
    if kind == "capture":
        return i18n.t(f"Rakip {y['point']} oynayarak {y['num']} taşını esir alıyor.",
                 f"Your opponent captures {y['num']} "
                 f"{'stone' if y['num'] == 1 else 'stones'} by playing {y['point']}.")
    if kind == "own_area":
        return i18n.t("Bu nokta zaten senin bölgendi. Çin kuralında buraya taş koymak puan "
                 "değiştirmez ama bir hamleyi boşa harcar ve grubunun 1 nefesini kapatır.",
                 "This point was already your territory. Under Chinese rules a stone here "
                 "doesn't change the score, but it wastes a move and fills one of your "
                 "group's liberties.")
    if kind == "big_point":
        return i18n.t(f"Tahtanın asıl boş yeri {REGION_SHORT.get(y['empty'], y['empty'])} bölgesiydi "
                 f"ve motorun istediği {y['point']} orada. Sen "
                 f"{REGION_SHORT.get(y['yours'], y['yours'])} bölgesinde oynadın.",
                 f"The biggest empty area was the {REGION_EN.get(y['empty'], y['empty'])}, "
                 f"and the engine's move {y['point']} was there. You played in the "
                 f"{REGION_EN.get(y['yours'], y['yours'])}.")
    return ""


def findings_full(moves, size, r):
    """Findings in the record + the ones that can be produced retroactively.

    big_point() was added to detection() later and is missing from old games'
    jsonl. Being pure board math, it can be filled in without running the
    engine -- old games benefit from the new detection too.
    """
    out = [finding_struct(b) or b for b in (r.get("findings") or [])]
    bn = big_point(moves[:r.get("move_no", 1) - 1],
                     r.get("best_move"), r.get("move"), size)
    if bn and bn not in out:
        out.append(bn)
    return out


# Names of the stages that split a game in three: the brief and the pattern
# report print the same three. A function, not a constant -- a string at
# module top level would freeze in the language active at import.
STAGE = (lambda: i18n.t("Açılış", "Opening"),
        lambda: i18n.t("Orta oyun", "Middle game"),
        lambda: i18n.t("Bitiriş", "Endgame"))

# Maps a finding kind to a habit. Keywords used to be searched for in the
# sentence; since findings are structures, the kind is enough.
HABITS = {
    "big_point": ("büyük noktayı bırakıp küçük yerde oynamak",
                    "leaving the big point to play somewhere small"),
    "one_liberty": ("grubunu tek nefese düşürmek",
                  "reducing your own group to one liberty"),
    "two_liberties": ("grubunu dar nefese sokmak",
                  "squeezing your own group short of liberties"),
    "capture": ("rakibe taş kaptırmak", "letting your stones be captured"),
    "own_area": ("kendi bölgene taş koymak", "playing inside your own territory"),
}


def habits_of(finding):
    """Habit name in the selected language. Grouping is by KIND -- even if the name
    changes, the pattern table collects it on the same row."""
    name = HABITS.get((finding_struct(finding) or {}).get("kind"))
    return i18n.t(*name) if name else None


def pattern(games):
    """Cross-game mistake pattern (markdown). Pure: knows no files or engine.

    games: [(name, size, moves, records)] -- newest first.

    The single-game brief tells "what happened in this game". This covers what
    it misses: what the HABIT is. Tools on the market (AI Sensei, KaTrain)
    analyze one game at a time; nobody shows the mistake a player repeats
    across games.
    """
    if not games:
        return i18n.t("# Hata örgüsü\n\nKayıtlı maç yok.\n",
                     "# Mistake pattern\n\nNo saved games.\n")

    counter, total, seen_games = {}, {}, {}
    n_move = n_loss = 0
    for name, size, moves, records in games:
        for r in records:
            n_move += 1
            n_loss += r.get("points_lost", 0.0)
            for b in findings_full(moves, size, r):
                a = habits_of(b)
                if not a:
                    continue
                counter[a] = counter.get(a, 0) + 1
                total[a] = total.get(a, 0.0) + r.get("points_lost", 0.0)
                seen_games.setdefault(a, set()).add(name)

    o = [i18n.t("# Hata örgüsü", "# Mistake pattern"), "",
         i18n.t(f"{len(games)} maç · {n_move} değerlendirilmiş hamle · toplam kayıp "
               f"**{n_loss:.0f}p** (ortalama {n_loss / max(n_move, 1):.2f}p/hamle)",
               f"{len(games)} games · {n_move} graded moves · total loss "
               f"**{n_loss:.0f}p** (average {n_loss / max(n_move, 1):.2f}p/move)")]

    if counter:
        o += ["", i18n.t("## En pahalı alışkanlıkların", "## Your most expensive habits"), "",
              i18n.t("| Alışkanlık | Kez | Toplam | Kez başına | Kaç maçta |",
                    "| Habit | Times | Total | Per time | In how many games |"),
              "|---|---|---|---|---|"]
        for a in sorted(counter, key=lambda k: -total[k]):
            o.append(f"| {a[:1].upper() + a[1:]} | {counter[a]} | {total[a]:.0f}p | "
                     f"{total[a] / counter[a]:.1f}p | {len(seen_games[a])}/{len(games)} |")
        margin = 100 * sum(total.values()) / max(n_loss, 1)
        o.append("")
        o.append(i18n.t(
            f"Bu alışkanlıklar toplam kaybının **%{margin:.0f}**'ini açıklıyor. "
            "Gerisi tespit üretmeyen hamleler: hepsi küçük kayıplar değil, "
            "yalnızca otomatik tespitin yakalayamadıkları.",
            f"These habits account for **{margin:.0f}%** of your total loss. "
            "The rest are moves that produced no detection: not all of them are small "
            "losses, just the ones automatic detection cannot catch."))

    # Stage trend: older half vs newer half. An average alone lies, which is why
    # the single-game brief is split into stages too.
    half = max(1, len(games) // 2)
    o += ["", i18n.t("## Evre trendi (eski yarı → yeni yarı)",
                    "## Phase trend (older half → newer half)"), "",
          i18n.t("| Evre | Eski | Yeni |", "| Phase | Older | Newer |"), "|---|---|---|"]
    for name_dat, margin_lo, margin_hi in ((STAGE[0](), 0.0, 1 / 3), (STAGE[1](), 1 / 3, 2 / 3),
                                 (STAGE[2](), 2 / 3, 1.0)):
        row_ = []
        for group_ in (games[half:], games[:half]):        # older first, newer after
            k = t = 0
            for _, _, _, records in group_:
                ordered = sorted(records, key=lambda r: r.get("move_no", 0))
                g = ordered[int(margin_lo * len(ordered)):int(margin_hi * len(ordered))]
                k += len(g)
                t += sum(r.get("points_lost", 0.0) for r in g)
            row_.append(f"{t / k:.2f}p" if k else "-")
        o.append(f"| {name_dat} | {row_[0]} | {row_[1]} |")

    if counter:
        en = max(counter, key=lambda k: total[k])
        o += ["", i18n.t("## Tek iş", "## One job"), "",
              i18n.t(f"**{en[:1].upper() + en[1:]}**: {counter[en]} kez, {total[en]:.0f} puan. "
                    "Başka hiçbir şeye dokunmadan yalnızca bunu düzeltsen kaybının "
                    f"%{100 * total[en] / max(n_loss, 1):.0f}'i gider.",
                    f"**{en[:1].upper() + en[1:]}**: {counter[en]} times, "
                    f"{total[en]:.0f} points. Fix only this and nothing else and "
                    f"{100 * total[en] / max(n_loss, 1):.0f}% of your loss goes away.")]
    return "\n".join(o) + "\n"


# ---------- game brief ----------
# A ranked game has NO live feedback; learning happens after the game. No LLM
# API is wired in for this: the jsonl is 66 KB and mostly noise, and no model
# gets it raw. The hard part is not reaching a model but THROWING AWAY THE
# NOISE -- and that part is plain Python, from data we already have.
#
# The resulting file can be dropped into any model the user likes: zero
# integration, works today. If doing it by hand 20 times still feels like a
# chore, then it gets automated.
def brief(name, mode, size, records, sv=None):
    """A game's analysis brief (markdown). Pure: knows no files or engine."""
    if not records:
        return f"# {name}\n\n" + i18n.t("Değerlendirilmiş hamle yok.\n",
                                     "No graded moves.\n")
    n = len(records)
    top = sum(r["points_lost"] for r in records)
    # The final score is NOT taken from a single record: once the board is
    # completely full KataGo's evaluation degenerates (in a real game the last
    # record said -11.9, the right value was -88.0). The median of the last few
    # records is robust to that.
    ends = sorted(r.get("next_score_lead", 0.0) for r in records[-5:])
    end = ends[len(ends) // 2]
    o = [f"# {name}", "",
         i18n.t(f"- Mod: **{mode}** · tahta {size}x{size} · {n} değerlendirilmiş hamle",
               f"- Mode: **{mode}** · board {size}x{size} · {n} graded moves"),
         i18n.t(f"- Sonuç: **{end:+.1f}** puan (senin gözünden)",
               f"- Result: **{end:+.1f}** points (from your side)"),
         i18n.t(f"- Toplam kayıp **{top:.1f}p** · ortalama {top / n:.2f}p/hamle",
               f"- Total loss **{top:.1f}p** · average {top / n:.2f}p/move")]
    if sv:
        o.append(i18n.t(f"- Hamle kalitesi: **~{sv['label']}** "
                       f"({sv['move_count']} hamlenin medyanı)",
                       f"- Move quality: **~{sv['label']}** "
                       f"(median of {sv['move_count']} moves)"))

    # Stage split -- an average alone lies.
    o += ["", i18n.t("## Evreler", "## Phases"), "",
          i18n.t("| Evre | Hamle | Kayıp | Hamle başına | Blunder |",
                "| Phase | Moves | Loss | Per move | Blunders |"),
          "|---|---|---|---|---|"]
    threes = [(STAGE[0](), 1, n // 3), (STAGE[1](), n // 3 + 1, 2 * n // 3),
             (STAGE[2](), 2 * n // 3 + 1, 10 ** 9)]
    ordered = sorted(records, key=lambda r: r["move_no"])
    for name_dat, lo, hi in threes:
        g = ordered[max(0, lo - 1):min(n, hi)]
        if not g:
            continue
        k = sum(r["points_lost"] for r in g)
        o.append(f"| {name_dat} | {len(g)} | {k:.1f}p | {k / len(g):.2f}p | "
                 f"{sum(category(r) == 'blunder' for r in g)} |")

    # A single-move list writes the same mistake four times; a run says "D11 has
    # been waiting for you for four moves" in one line. That first, then the singles.
    sr = sorted(runs(records), key=lambda s: -s["lost"])
    if sr:
        o += ["", i18n.t("## Kaçırılan kilit noktalar", "## Key points you missed"), "",
              i18n.t(f"Motor aynı noktayı tekrar tekrar istedi, sen başka yerde oynadın "
                    f"(arada en fazla {RUN_GAP} hamlelik kesinti). Bu seriler toplam "
                    f"kaybının **%{100 * sum(s['lost'] for s in sr) / max(top, 1):.0f}**'i.",
                    f"The engine asked for the same point again and again while you played "
                    f"elsewhere (gaps of at most {RUN_GAP} moves). These runs are "
                    f"**{100 * sum(s['lost'] for s in sr) / max(top, 1):.0f}%** of your "
                    f"total loss."),
              ""]
        for s in sr[:3]:
            o.append(i18n.t(f"- **{s['move']}**: {s['n']} kez ({s['start']}–{s['end']}) · "
                           f"{s['lost']:.1f}p · oynadıkların: ",
                           f"- **{s['move']}**: {s['n']} times ({s['start']}–{s['end']}) · "
                           f"{s['lost']:.1f}p · what you played: ")
                     + ", ".join(r.get("move", "?") for r in s["records"])
                     + (i18n.t(f" · araya giren: {', '.join(map(str, s['interrupted']))}",
                              f" · interrupted by: {', '.join(map(str, s['interrupted']))}")
                        if s["interrupted"] else ""))

    o += ["", i18n.t("## En pahalı hamleler", "## Your most expensive moves"), ""]
    for r in sorted(records, key=lambda r: -r["points_lost"])[:5]:
        if r["points_lost"] < 0.3:
            break
        o.append(i18n.t(f"**{r['move_no']}. {r['move']}**: {r['points_lost']:.1f}p kayıp "
                       f"({category_name(category(r))}) · en iyi **{r['best_move']}** "
                       f"· skor {r.get('prev_score_lead', 0):+.1f} → "
                       f"{r.get('next_score_lead', 0):+.1f}",
                       f"**{r['move_no']}. {r['move']}**: {r['points_lost']:.1f}p lost "
                       f"({category_name(category(r))}) · best **{r['best_move']}** "
                       f"· score {r.get('prev_score_lead', 0):+.1f} → "
                       f"{r.get('next_score_lead', 0):+.1f}"))
        for b in r.get("findings", []):
            o.append(f"  - {finding_text(b)}")
        bottom = r.get("alternatives") or []
        if bottom:
            o.append(i18n.t("  - Alternatifler: ", "  - Alternatives: ")
                     + " · ".join(f"{a['move']} ({a['diff']:+.1f}p)" for a in bottom))
        o.append("")

    # Measurement transparency: which path was used affects the judgment.
    count = {}
    for r in records:
        count[r.get("measurement", "?")] = count.get(r.get("measurement", "?"), 0) + 1
    make = sum(1 for r in records if r.get("level_estimate"))
    o += [i18n.t("## Ölçüm notları", "## Measurement notes"), "",
          i18n.t("- Yöntem: ", "- Method: ")
          + " · ".join(f"{k} {v}" for k, v in count.items()),
          i18n.t(f"- Seviye tahmini üreten hamle: {make}/{n} "
                f"(gerisinde merdiven ayrışmıyor, pozisyon seviye ölçmüyor)",
                f"- Moves that produced a level estimate: {make}/{n} "
                f"(on the rest the ladder does not separate — the position does not "
                f"measure level)"),
          i18n.t("- `once-sonra` yolu doğrulandı (bağımsız ölçümle 0.2p içinde). Kategori "
                "doğrudan puan kaybından çıkıyor.",
                "- The `once-sonra` path is verified (within 0.2p of an independent "
                "measurement). The category comes straight from the point loss."),
          "", i18n.t("## Soru", "## Question"), "",
          i18n.t("Bu maçta tekrar eden hata örgüsü ne? Hangi TEK şeyi çalışsam en çok puan "
                "kazanırım?",
                "What is the repeating mistake pattern in this game? Which ONE thing "
                "should I study to gain the most points?"), ""]
    return "\n".join(o)


# ---------- chart data ----------
# All pure: a record list goes in, numbers/lists come out. Qt never sees this;
# charts.py turns these functions into drawings. The split makes testing cheap
# -- --selftest can run all of this math without importing the UI.

def _ordered(records):
    return sorted(records, key=lambda r: r.get("move_no", 0))


def distribution(records):
    """Category buckets in CATEGORIES order: [[record, ...]] -- 6 elements.

    'How many did I make' and 'which ones' come from the same structure; len()
    gives the count, the bucket itself gives the list."""
    buckets = [[] for _ in CATEGORIES]
    for r in _ordered(records):
        k = category(r)
        if k:
            buckets[CATEGORIES.index(k)].append(r)
    return buckets


RUN_GAP = 2     # at most this many of your moves between two misses


def runs(records, at_least=3):
    """A stretch where the engine kept asking for the same point and you did not play it.
    Returns: [dict], sorted by start.

    A miss: the engine said X, you played something other than X AND lost
    points. If there are <= RUN_GAP moves between two misses, it is the same
    run. The ones in between are bridges ("interrupted"): they count toward
    neither n nor the loss -- for a moment the engine put another spot first,
    or your move was as good as X. If you play X the run ends there.

    The first definition allowed no gap; in the first 13x13 game C3 was missed
    9 times between moves 47 and 73 but split into 4 pieces and became
    invisible (2026-09-16). The earlier measurement (31 runs in 16 games) used
    the gapless definition.
    """
    k = _ordered(records)
    points = {r.get("best_move") for r in k} - {None, "pass"}
    out = []
    for ei in points:
        group_, last_i = [], None
        for i, r in enumerate(k + [None]):          # None: close the last group
            played = r is not None and r.get("move") == ei
            escaped = (r is not None and not played and r.get("best_move") == ei
                     and r.get("points_lost", 0.0) >= 0.1)
            if group_ and (r is None or played or (escaped and i - last_i - 1 > RUN_GAP)):
                if len(group_) >= at_least:
                    members = {id(x) for x in group_}
                    start, end = group_[0]["move_no"], group_[-1]["move_no"]
                    out.append({"move": ei, "n": len(group_), "start": start, "end": end,
                                "lost": sum(x.get("points_lost", 0.0) for x in group_),
                                "records": group_,
                                "interrupted": [x["move_no"] for x in k
                                          if start < x["move_no"] < end and id(x) not in members]})
                group_ = []
            if escaped:
                group_.append(r)
                last_i = i
    return sorted(out, key=lambda s: s["start"])


def line_of(mv, size=DEFAULT_SIZE):
    """Distance from the edge: line 1 is the edge. None for 'pass'/invalid."""
    if not mv or mv == "pass":
        return None
    try:
        i = from_gtp(mv, size)
    except (ValueError, IndexError):
        return None
    if i is None:
        return None
    r, c = divmod(i, size)
    return min(r, size - 1 - r, c, size - 1 - c) + 1


def line_distribution(records, size=DEFAULT_SIZE):
    """(yours, the engine's) -- line -> move count. Both on the same scale.

    In ranked games the player played on line 1 TWICE as often as the engine
    (31% / 15%). Side by side on one chart, there is nothing left to argue about.
    """
    me, engine_ = {}, {}
    for r in _ordered(records):
        for mv, counter in ((r.get("move"), me), (r.get("best_move"), engine_)):
            h = line_of(mv, size)
            if h:
                counter[h] = counter.get(h, 0) + 1
    return me, engine_


def heatmap(records, size=DEFAULT_SIZE):
    """Point played -> {'lost': total, 'records': [...]}.

    The GEOGRAPHY of loss. "You lose 4.7p on average" is abstract; "you gave
    away 38 points in the bottom left" is something you can show on the board.
    """
    out = {}
    for r in _ordered(records):
        mv = r.get("move")
        if not mv or mv == "pass":
            continue
        try:
            i = from_gtp(mv, size)
        except (ValueError, IndexError):
            continue
        if i is None:
            continue
        h = out.setdefault(i, {"lost": 0.0, "records": []})
        h["lost"] += r.get("points_lost", 0.0)
        h["records"].append(r)
    return out


def cumulative_loss(records):
    """[(move_no, total loss up to that point)].

    The live score carries both players' mistakes at once; this curve carries
    only YOUR loss. A flat stretch is good play, a steep one is where the game
    was decided -- a continuous version of the brief's stage table.
    """
    t, out = 0.0, []
    for r in _ordered(records):
        t += r.get("points_lost", 0.0)
        out.append((r.get("move_no", 0), t))
    return out


def level_series(records):
    """[(move_no, label|None, value|None)] -- level estimate move by move.

    About half of the records produce no estimate (the position does not
    separate the ladder); those moves return None and stay gray in the
    drawing. Filling the gap would be a lie.
    """
    out = []
    for r in _ordered(records):
        sv = r.get("level_estimate")
        et = sv.get("label") if sv else None
        out.append((r.get("move_no", 0), et,
                    rank_value(et) if et else None))
    return out


def game_summary(name, records):
    """One game's trend row. game_trend() lines them up."""
    k = _ordered(records)
    n = len(k)
    if not n:
        return None
    top = sum(r.get("points_lost", 0.0) for r in k)
    # Same caution as brief(): the last record may be degenerate, so the median of the last 5.
    ends = sorted(r.get("next_score_lead", 0.0) for r in k[-5:])
    return {"name": name, "n": n, "lost": top, "mean_loss": top / n,
            "good_ratio": 100 * sum(category(r) in ("excellent", "great", "good")
                                  for r in k) / n,
            "blunder_ratio": 100 * sum(category(r) == "blunder" for r in k) / n,
            "end": ends[len(ends) // 2],
            "heavy": sum(1 for r in k if r.get("points_lost", 0.0) >= 3)}


def game_trend(games):
    """games: [(name, records)] -- newest first. Returns: summaries oldest to newest.

    A single game told 'what happened today', the pattern report 'what the
    habit is'. This answers the third question: AM I IMPROVING.
    """
    out = [game_summary(name, k) for name, k in games]
    return [o for o in reversed(out) if o]
