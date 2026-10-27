#!/usr/bin/env python3
"""KataGo client + configuration.

The ONE place that changes if this ever moves to the web. Callers see
`Engine.query(...)`; whether a single process, a pool of N processes or a
remote service sits behind it makes no difference.

Configuration is read from environment variables: on a server or in a
container the paths will differ, and a hard-coded path at module top level
breaks on the first deploy.
"""
import json, os, queue, re, shutil, subprocess, threading, time
from dataclasses import dataclass, field

import bundle, goban, i18n

def katago_root():
    """KataGo folder: GOMENTOR_KATAGO_DIR > katago/ next to the app > ~/katago.
    If none exists, the one next to the app -- so the error message points there.

    `beside` is the .exe's folder when frozen, the project when run from source.
    The source path used to be pinned to a single developer machine.
    """
    if os.environ.get("GOMENTOR_KATAGO_DIR"):
        return os.environ["GOMENTOR_KATAGO_DIR"]
    candidates = [bundle.beside("katago"), os.path.join(os.path.expanduser("~"), "katago")]
    return next((y for y in candidates if os.path.isdir(y)), candidates[0])


DEFAULT_ROOT = katago_root()


@dataclass
class Config:
    # If the binary is not in the package, from PATH (on macOS the one Homebrew
    # installed). Weights and cfg always come from DEFAULT_ROOT -- they are
    # platform independent.
    katago: str = field(default_factory=lambda: bundle.tool(
        os.path.join(DEFAULT_ROOT, "katago.exe" if os.name == "nt" else "katago"),
        "katago"))
    model: str = field(default_factory=lambda: os.path.join(DEFAULT_ROOT, "kata1.bin.gz"))
    human: str = field(default_factory=lambda: os.path.join(DEFAULT_ROOT, "humanv0.bin.gz"))
    cfg: str = field(default_factory=lambda: os.path.join(DEFAULT_ROOT, "analysis_example.cfg"))
    # Logs and the tuning cache go to the writable data root, NOT the KataGo
    # folder. In an installed copy katago/ is read-only; when KataGo cannot write
    # its cache it re-tunes the GPU on every start (measured: 3+ minutes).
    logdir: str = field(default_factory=lambda: os.path.join(bundle.engine_data_root(), "katago-logs"))
    data: str = field(default_factory=lambda: os.path.join(bundle.engine_data_root(), "katago-data"))
    size: int = 9
    komi: float = goban.KOMI
    visits: int = 600
    timeout: float = 120.0
    rule: str = "chinese"

    @classmethod
    def env(cls):
        """Environment overrides: GOMENTOR_SIZE, GOMENTOR_VISITS, GOMENTOR_KOMI..."""
        c = cls()
        for name, kind in (("size", int), ("visits", int),
                        ("komi", float), ("timeout", float)):
            v = os.environ.get("GOMENTOR_" + name.upper())
            if v:
                setattr(c, name, kind(v))
        return c


class EngineError(RuntimeError):
    pass


def _prepare_cache(cfg):
    """Set up the tuning cache somewhere writable; copy the bundled one once.

    The bundled `katago/KataGoData` may be read-only (Program Files in an
    installed copy). If it is not copied, KataGo cannot use it and re-tunes
    the GPU on every start.
    """
    if os.path.isdir(cfg.data):
        return
    source = os.path.join(os.path.dirname(cfg.katago), "KataGoData")
    try:
        if os.path.isdir(source):
            shutil.copytree(source, cfg.data)
        else:
            os.makedirs(cfg.data, exist_ok=True)
    except OSError:
        pass


def missing_files(cfg, human=True):
    """Files the engine needs to start but that cannot be found.

    Without this check a missing binary was a raw FileNotFoundError from
    Popen, and missing weights made KataGo die silently and the first query
    time out.
    """
    required = [cfg.katago, cfg.model, cfg.cfg] + ([cfg.human] if human else [])
    return [y for y in required if not (os.path.isfile(y) or shutil.which(y))]


class Engine:
    """A single KataGo process. Thread-safe.

    ponytail: one process. When concurrent users grow, pointing `query` at a
    pool of N processes is enough -- the caller does not change.
    """

    def __init__(self, cfg=None, human=True):
        self.cfg = cfg or Config.env()
        missing = missing_files(self.cfg, human)
        if missing:
            raise EngineError(
                i18n.t("KataGo bulunamadı. Eksik:\n  ", "KataGo not found. Missing:\n  ") + "\n  ".join(missing)
                + i18n.t("\n\nKataGo klasörünü uygulamanın yanına `katago/` ya da ~/katago "
                       "olarak koy, veya GOMENTOR_KATAGO_DIR ile yolunu ver.",
                       "\n\nPut the KataGo folder next to the app as `katago/` or as "
                       "~/katago, or give its path with GOMENTOR_KATAGO_DIR."))
        _prepare_cache(self.cfg)
        cmd = [self.cfg.katago, "analysis", "-model", self.cfg.model,
               "-config", self.cfg.cfg, "-override-config",
               f"logDir={self.cfg.logdir},homeDataDir={self.cfg.data}"]
        if human:
            cmd[4:4] = ["-human-model", self.cfg.human]
        self.err_path = os.path.join(os.path.dirname(self.cfg.katago), "engine_stderr.log")
        self._err = open(self.err_path, "w", encoding="utf-8", errors="replace")
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=self._err, text=True, bufsize=1)
        self.n = 0
        # The engine is a single pipe. If two threads write and read at once the
        # answers interleave and the stream is corrupted FOR GOOD -- all queries are serial.
        self.lock = threading.Lock()
        # Reading happens on its own thread: readline() blocks forever, and in an
        # unexpected state the app would hang for good. Queue + timeout ends that.
        self.lines = queue.Queue()
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        try:
            for line in self.p.stdout:
                self.lines.put(line)
        except (OSError, ValueError):
            pass
        self.lines.put(None)                       # EOF sentinel

    # On a new GPU KataGo runs OpenCL tuning on its FIRST START and that takes
    # MINUTES (measured: 3+ min with an empty cache, 13 s with one). Meanwhile
    # query() blocks and the UI sits on "analyzing" -- misleading: it is a one-time
    # setup, not analysis. The information was already on stderr; nobody read it.
    _TUNE = re.compile(r"Tuning (\d+)/(\d+)")

    def status_line(self):
        """A short line about what the engine is doing; None if nothing interesting."""
        end = self._tail_err(1)
        m = self._TUNE.search(end)
        if m:
            return i18n.t(f"KataGo GPU ayarı yapıyor: {m.group(1)}/{m.group(2)} · ilk açılışta "
                          "bir kereye mahsus, birkaç dakika sürebilir",
                          f"KataGo is tuning the GPU: {m.group(1)}/{m.group(2)} · once, on first "
                          "start, may take a few minutes")
        if "Started, ready" in end:
            return None
        if "Loading" in end or "loading" in end:
            return i18n.t("KataGo ağları yüklüyor…", "KataGo is loading its networks…")
        return None

    def _tail_err(self, n=6):
        try:
            self._err.flush()
            with open(self.err_path, encoding="utf-8", errors="replace") as f:
                return " | ".join(f.read().strip().splitlines()[-n:])
        except OSError:
            return ""

    def close(self):
        try:
            self.p.terminate()
        except OSError:
            pass

    def query(self, **kw):
        """One analysis query. Returns as many results as `analyzeTurns` is long."""
        c = self.cfg
        # Setup stones (handicap) sit at the start of the move list as ["AB", "D4"];
        # KataGo wants them as `initialStones` and turn numbers count from the moves.
        # None of the callers need to know this.
        mv = kw.get("moves") or []
        k = goban.setup_count(mv)
        if k:
            kw["initialStones"] = [[col[1], m] for col, m in mv[:k]]
            kw["moves"] = mv[k:]
            if not kw["moves"]:
                kw.setdefault("initialPlayer", "W" if any(c == "AB" for c, _ in mv[:k]) else "B")
            kw["analyzeTurns"] = [t - k for t in kw.get("analyzeTurns", [len(mv)])]
        with self.lock:
            self.n += 1
            qid = kw.setdefault("id", f"q{self.n}")
            kw.setdefault("rules", c.rule)
            kw.setdefault("komi", c.komi)
            kw.setdefault("boardXSize", c.size)
            kw.setdefault("boardYSize", c.size)
            self.p.stdin.write(json.dumps(kw) + "\n")
            self.p.stdin.flush()
            want, out = len(kw.get("analyzeTurns", [0])), []
            end = time.monotonic() + c.timeout
            while len(out) < want:
                try:
                    line = self.lines.get(timeout=max(1.0, end - time.monotonic()))
                except queue.Empty:
                    raise EngineError(i18n.t(f"KataGo {c.timeout:.0f}sn içinde cevap vermedi ",
                                             f"KataGo did not answer within {c.timeout:.0f}s ")
                                      + f"({len(out)}/{want}). stderr: " + self._tail_err())
                if line is None:
                    raise EngineError(i18n.t("KataGo process öldü. stderr: ", "The KataGo process died. stderr: ") + self._tail_err())
                line = line.strip()
                if not line:
                    continue                       # an empty line is not JSON
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue                       # swallow noise, no deadlock on desync
                if r.get("id") != qid:
                    continue                       # the answer to another query
                if "error" in r:
                    raise EngineError(r["error"])
                if "turnNumber" not in r:
                    continue                       # warning/progress line
                out.append(r)
            return sorted(out, key=lambda r: r["turnNumber"])

    # ---------- human model ----------
    def human_policy(self, moves, profile, size=None):
        """[(coordinate, probability)] -- the move distribution of a human at this level."""
        size = size or self.cfg.size
        n = size * size
        r = self.query(moves=moves, analyzeTurns=[len(moves)], maxVisits=1,
                       includePolicy=True, boardXSize=size, boardYSize=size,
                       overrideSettings={"humanSLProfile": profile})[0]
        pol = r.get("humanPolicy") or r["policy"]
        from goban import to_gtp
        out = [(to_gtp(i, size), q) for i, q in enumerate(pol[:n]) if q > 0]
        if len(pol) > n and pol[n] > 0:            # last element is pass
            out.append(("pass", pol[n]))
        return out

    def human_top(self, moves, profile, k=3, size=None):
        c = sorted(self.human_policy(moves, profile, size), key=lambda t: -t[1])
        return [(m, round(p, 3)) for m, p in c[:k]]

    def human_sample(self, moves, profile, k=5, temp=1.0, tail=0.0, pass_=True):
        """Pick a move from the human policy.

        KataGo reference (gtp_human5k_example.cfg):
          chosenMoveTemperature = 0.70   (0.85 in the opening)
          chosenMoveTemperatureOnlyBelowProb = 0.01
            -> temperature applies ONLY to the tail below 1%; the main
               distribution stays as it is. So the goal is not to sharpen the
               distribution but to snuff out very unlikely moves.
          humanSLChosenMoveIgnorePass = true
            -> the human net does not choose pass reliably (the docs' own warning).

        temp=1.0, tail=0.0, pass_=True  -> old behavior (raw sampling)
        temp=0.70, tail=0.01, pass_=False -> aligned with the reference
        tail=1.0 applies the temperature to the whole distribution (for comparison).
        """
        import random
        c = self.human_policy(moves, profile)
        if not pass_:
            c = [(m, q) for m, q in c if m != "pass"]
        if not c:
            return []
        if temp != 1.0:
            c = [(m, q ** (1.0 / temp) if q < tail else q) for m, q in c]
        mv, w = zip(*c)
        if not any(w):
            return []
        return list(random.choices(mv, weights=w, k=k))
