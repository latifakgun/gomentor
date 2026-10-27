#!/usr/bin/env python3
"""End-of-game analysis video: animated scenes + two-voice narration -> mp4.

  python video.py GAME_NAME --draft     make the script (template, no LLM)
  python video.py GAME_NAME --polish    have an LLM polish the script (optional, agy)
  python video.py GAME_NAME             make the video from the script
  python video.py --demo                drawing + timing check

Flow: narration.script() produces section -> utterance -> scene. Each
utterance is voiced with edge-tts; the length of the audio is spread over
that utterance's scene (animation steps included) and joined with ffmpeg
concat. The audio sets the frame timing.

The LLM does NOT write the text -- the template does, and it cannot say what
is not in the data. If agy is installed and the user asks for it, write()
turns the same facts into smoother sentences.
"""
import asyncio, json, os, re, shutil, subprocess, sys, tempfile, textwrap, time

import edge_tts
from PIL import Image, ImageDraw, ImageFont

import narration, i18n, goban, bundle, scoring, store

# Speaker names ("speaker_a"/"speaker_b") are KEYS stored in the script JSON
# and are not translated; only which edge-tts voice reads them changes. The
# voice is picked from the script's OWN language (sen["lang"]), not from the
# UI's current language: rendering a script made in Turkish after switching
# to English had an English voice reading Turkish text. In Turkish both keys
# are Emel: tr-TR-AhmetNeural returned almost no audio in October 2026
# (0 out of 10 tries), and the Multilingual male voices read Turkish with a
# foreign accent. If Ahmet recovers, revert this.
VOICES = {"tr": {"speaker_a": "tr-TR-EmelNeural", "speaker_b": "tr-TR-EmelNeural"},
          "en": {"speaker_a": "en-US-AriaNeural", "speaker_b": "en-US-GuyNeural"}}
W, H = 1280, 720
BOARD_PX, EDGE = 620, 40
BACKGROUND = (28, 26, 24)
WOOD, LINE = (219, 179, 106), (60, 45, 30)
COLOR = {"red": (220, 60, 50), "green": (40, 170, 90), "yellow": (235, 190, 40)}
SPEAKER_COLOR = {"speaker_a": (235, 150, 90), "speaker_b": (110, 170, 230)}
TEXT, FAINT = (245, 240, 230), (170, 165, 155)
YOU_R, ENGINE_R = (235, 150, 90), (110, 170, 230)
# External tools: all found on PATH, overridable by environment variable when needed.
AGY = os.environ.get("GOMENTOR_AGY", "agy")
FFMPEG = os.environ.get("GOMENTOR_FFMPEG", bundle.tool(
    bundle.beside("ffmpeg.exe" if os.name == "nt" else "ffmpeg"), "ffmpeg"))
MODEL = os.environ.get("GOMENTOR_MODEL", "gemini-3.1-pro-high")


def _first_existing(*paths):
    return next((y for y in paths if os.path.exists(y)), None)


FONT = _first_existing("C:/Windows/Fonts/arial.ttf",
                "/System/Library/Fonts/Supplemental/Arial.ttf",
                "/Library/Fonts/Arial.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
FONT_B = _first_existing("C:/Windows/Fonts/arialbd.ttf",
                  "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                  "/Library/Fonts/Arial Bold.ttf",
                  "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")


def _font(path_, size_):
    if not path_:                           # without a familiar font the video still comes out
        return ImageFont.load_default()
    try:
        return ImageFont.truetype(path_, size_)
    except OSError:
        return ImageFont.load_default()


# ---------- drawing pieces ----------

def _canvas():
    img = Image.new("RGB", (W, H), BACKGROUND)
    return img, ImageDraw.Draw(img)


def _board(d, b, rings=(), numbers=()):
    """Board on the left half. rings: [gtp, color, label]; numbers: {idx: number}."""
    size = b.size
    d.rectangle([EDGE, EDGE, EDGE + BOARD_PX, EDGE + BOARD_PX], fill=WOOD)
    step = BOARD_PX / (size + 1)
    xy = lambda i: (EDGE + step * (i % size + 1), EDGE + step * (i // size + 1))
    for k in range(size):
        d.line([xy(k), xy(k + size * (size - 1))], fill=LINE)
        d.line([xy(k * size), xy(k * size + size - 1)], fill=LINE)
    for i in goban.star_points(size):
        x, y = xy(i)
        d.ellipse([x - 3, y - 3, x + 3, y + 3], fill=LINE)
    f_coord = _font(FONT, int(step * 0.42))
    for k in range(size):
        x, _ = xy(k)
        d.text((x, EDGE + step * 0.35), goban.LETTERS[k], fill=LINE, font=f_coord, anchor="mm")
        _, y = xy(k * size)
        d.text((EDGE + step * 0.35, y), str(size - k), fill=LINE, font=f_coord, anchor="mm")

    r = step * 0.45
    f_no = _font(FONT_B, int(step * 0.5))
    for i, t in enumerate(b.g):
        if t:
            x, y = xy(i)
            d.ellipse([x - r, y - r, x + r, y + r],
                      fill=(20, 20, 20) if t > 0 else (245, 245, 240), outline=LINE)
            if i in numbers:
                d.text((x, y), str(numbers[i]), anchor="mm", font=f_no,
                       fill=(245, 245, 240) if t > 0 else (20, 20, 20))
    f_label = _font(FONT_B, 20)
    for gtp, color, label in rings:
        try:
            i = goban.from_gtp(gtp, size)
        except ValueError:
            continue
        if i is None:
            continue
        x, y = xy(i)
        c = COLOR.get(color, color)
        d.ellipse([x - r - 4, y - r - 4, x + r + 4, y + r + 4], outline=c, width=4)
        if label:                        # keep a label on an edge point from spilling over the coordinates
            d.text((max(x, EDGE + step * 1.6), y - r - 16), label, fill=c, font=f_label, anchor="mm")


def _text(d, x, y, text_, width_, size_, color, line_gap=1.4, at_most=99):
    f = _font(FONT, size_)
    for row_ in textwrap.wrap(text_, width_)[:at_most]:
        d.text((x, y), row_, fill=color, font=f)
        y += int(size_ * line_gap)
    return y


def _panel(d, section, utterance, upper=""):
    """Right panel: title, subtitle, speaker and what they say."""
    px = EDGE * 2 + BOARD_PX
    d.text((px, EDGE + 6), section.get("title", ""), fill=TEXT, font=_font(FONT_B, 34))
    y = EDGE + 58
    if section.get("subtitle"):
        y = _text(d, px, y, section["subtitle"], 38, 22, FAINT)
    if upper:
        d.text((px, y + 6), upper, fill=COLOR["yellow"], font=_font(FONT_B, 28))
        y += 44
    y += 18
    who = utterance.get("speaker", "speaker_a")
    d.text((px, y), who.capitalize(), fill=SPEAKER_COLOR.get(who, TEXT), font=_font(FONT_B, 22))
    _text(d, px, y + 34, utterance.get("text_", ""), 44, 20, (205, 200, 190), at_most=17)


def _subtitle(d, section, utterance):
    """For full-width scenes: title on top, speaker and text at the bottom."""
    d.text((60, 30), section.get("title", ""), fill=TEXT, font=_font(FONT_B, 36))
    if section.get("subtitle"):
        d.text((60, 76), section["subtitle"], fill=FAINT, font=_font(FONT, 22))
    who = utterance.get("speaker", "speaker_a")
    d.text((60, 560), who.capitalize(), fill=SPEAKER_COLOR.get(who, TEXT), font=_font(FONT_B, 22))
    _text(d, 60, 592, utterance.get("text_", ""), 100, 20, (205, 200, 190), at_most=4)


# ---------- scenes ----------
# Each scene yields [(Image, weight)]; the weights are spread over the
# utterance's audio length. Animation = consecutive frames of the same scene,
# no extra engine.

def _board_scene(m, section, utterance, s):
    img, d = _canvas()
    _board(d, goban.replay(m.moves, m.size, upto=s.get("move_no", 0)), s.get("rings", ()))
    _panel(d, section, utterance)
    return [(img, 1.0)]


def _continuation_scene(m, section, utterance, s):
    """Position + rings first, then the engine's continuation stone by stone, numbered."""
    n = s.get("move_no", 0)
    ring = s.get("rings", ())
    img, d = _canvas()
    _board(d, goban.replay(m.moves, m.size, upto=n), ring)
    _panel(d, section, utterance, i18n.t("motorun planı", "the engine's plan"))
    frames = [(img, 3.0)]
    color = 1 if (n < len(m.moves) and m.moves[n][0] == "B") or n >= len(m.moves) else -1
    b = goban.replay(m.moves, m.size, upto=n)
    number = {}
    for j, mv in enumerate(s.get("continuation", ())):
        try:
            i = goban.from_gtp(mv, m.size)
        except ValueError:
            break
        if i is None or not b.play(i, color if j % 2 == 0 else -color):
            break
        number = {k: v for k, v in number.items() if b.g[k]}      # captured stones come off
        number[i] = j + 1
        img, d = _canvas()
        # The red ring on the played move stays: "you played here, the plan was this".
        _board(d, b, [h for h in ring if h[1] == "red"], number)
        _panel(d, section, utterance, i18n.t("motorun planı", "the engine's plan"))
        frames.append((img, 1.0))
    if len(frames) > 1:
        frames[-1] = (frames[-1][0], 3.0)                          # hold the final state
    return frames


def _run_scene(m, section, utterance, s):
    """The waiting point is a fixed green ring; each of the player's moves in turn is red."""
    point, moves_ = s["point"], s.get("move_nos", [])
    frames = []
    for j, h in enumerate(moves_):
        img, d = _canvas()
        played_move = m.moves[h - 1][1] if 0 < h <= len(m.moves) else None
        ring = [[point, "green", "waiting"]]
        if played_move and played_move != "pass":
            ring.append([played_move, "red", ""])
        _board(d, goban.replay(m.moves, m.size, upto=h), ring)
        _panel(d, section, utterance, f"{j + 1} / {len(moves_)}  ·  "
               + i18n.t(f"{h}. hamle", f"move {h}"))
        frames.append((img, 1.0))
    if frames:
        frames[-1] = (frames[-1][0], 2.5)
    return frames or _board_scene(m, section, utterance, {})


def _score_scene(m, section, utterance, s):
    """The score curve draws itself left to right; the marked move is a yellow line."""
    k = scoring._ordered(m.valid_records)
    # Two points per move (before / after), same as charts.py: drawing only
    # "after" hid the opponent's mistakes, and the narration's "you were X ahead
    # before" had no place on the chart.
    point = []
    for r in k:
        point += [(r["move_no"] - 1, r.get("prev_score_lead", 0.0)),
                  (r["move_no"], r.get("next_score_lead", 0.0))]
    if len(point) < 2:
        img, d = _canvas()
        _subtitle(d, section, utterance)
        return [(img, 1.0)]
    x0, x1, y0, y1 = 90, 1210, 130, 520
    xmax = max(1, max(n for n, _ in point))
    ymax = max(10.0, max(abs(v) for _, v in point)) * 1.1
    X = lambda n: x0 + (x1 - x0) * n / xmax
    Y = lambda v: (y0 + y1) / 2 - (y1 - y0) / 2 * v / ymax
    mark = s.get("mark")
    frames = []
    steps = 8
    for a in range(1, steps + 1):
        img, d = _canvas()
        _subtitle(d, section, utterance)
        d.line([x0, Y(0), x1, Y(0)], fill=(90, 85, 80), width=1)
        f = _font(FONT, 18)
        d.text((x0 - 10, Y(ymax / 1.1)), f"+{ymax / 1.1:.0f}", fill=FAINT, font=f, anchor="rm")
        d.text((x0 - 10, Y(-ymax / 1.1)), f"-{ymax / 1.1:.0f}", fill=FAINT, font=f, anchor="rm")
        d.text((x0 - 10, Y(0)), "0", fill=FAINT, font=f, anchor="rm")
        d.text((x1, y1 + 14), i18n.t("önde sen ↑   geride sen ↓",
                                    "you ahead ↑   you behind ↓"),
               fill=FAINT, font=f, anchor="rm")
        end = point[:max(2, len(point) * a // steps)]
        d.line([(X(n), Y(v)) for n, v in end], fill=ENGINE_R, width=4)
        if mark and a == steps:
            d.line([X(mark), y0, X(mark), y1], fill=COLOR["yellow"], width=2)
            v = next((v for n, v in point if n == mark - 1), 0.0)     # before the move
            d.ellipse([X(mark) - 8, Y(v) - 8, X(mark) + 8, Y(v) + 8], fill=COLOR["yellow"])
            d.text((X(mark) + 12, y0 + 4), i18n.t(f"{mark}. hamle", f"move {mark}"),
                   fill=COLOR["yellow"],
                   font=_font(FONT_B, 22))
        frames.append((img, 0.25))
    frames[-1] = (frames[-1][0], 4.0)
    return frames


def _line_scene(m, section, utterance, s):
    """You / engine percentage per line; the bars grow in."""
    me, engine_ = scoring.line_distribution(m.valid_records, m.size)
    group_ = lambda h: min(h, 5)
    def ratio(counter):
        n = max(sum(counter.values()), 1)
        o = {}
        for h, c in counter.items():
            o[group_(h)] = o.get(group_(h), 0) + c / n
        return o
    ob, om = ratio(me), ratio(engine_)
    x0, y0, y1 = 140, 150, 500
    frames = []
    for a in range(1, 7):
        img, d = _canvas()
        _subtitle(d, section, utterance)
        f = _font(FONT, 20)
        for h in range(1, 6):
            x = x0 + (h - 1) * 210
            d.text((x + 60, y1 + 22),
                   i18n.t(f"{h}{'+' if h == 5 else ''}. hat",
                         f"line {h}{'+' if h == 5 else ''}"),
                   fill=TEXT, font=f, anchor="mm")
            for j, (o, c) in enumerate(((ob, YOU_R), (om, ENGINE_R))):
                v = o.get(h, 0.0) * a / 6
                bx = x + j * 64
                d.rectangle([bx, y1 - (y1 - y0) * v, bx + 56, y1], fill=c)
                if a == 6:
                    d.text((bx + 28, y1 - (y1 - y0) * v - 14), f"%{100 * o.get(h, 0):.0f}",
                           fill=c, font=f, anchor="mm")
        d.rectangle([1000, 40, 1020, 60], fill=YOU_R)
        d.text((1028, 50), "you", fill=TEXT, font=f, anchor="lm")
        d.rectangle([1090, 40, 1110, 60], fill=ENGINE_R)
        d.text((1118, 50), "engine_", fill=TEXT, font=f, anchor="lm")
        frames.append((img, 0.25))
    frames[-1] = (frames[-1][0], 4.0)
    return frames


def _card_scene(m, section, utterance, s):
    img, d = _canvas()
    d.text((W // 2, 200), s.get("big", ""), fill=COLOR["yellow"], font=_font(FONT_B, 64), anchor="mm")
    y = 270
    for row_ in s.get("rows", []):
        for piece in textwrap.wrap(row_, 60):
            d.text((W // 2, y), piece, fill=TEXT, font=_font(FONT, 30), anchor="mm")
            y += 42
    who = utterance.get("speaker", "speaker_a")
    d.text((60, 560), who.capitalize(), fill=SPEAKER_COLOR.get(who, TEXT), font=_font(FONT_B, 22))
    _text(d, 60, 592, utterance.get("text_", ""), 100, 20, (205, 200, 190), at_most=4)
    return [(img, 1.0)]


SCENES = {"board": _board_scene, "continuation": _continuation_scene, "run": _run_scene,
         "score": _score_scene, "edge_line": _line_scene, "card": _card_scene}


def scene_frames(m, section, utterance, time_):
    """[(Image, seconds)] -- adds up to `time_`. Weights are spread in proportion to the time."""
    s = utterance.get("scene") or {"kind": "board"}
    frames = SCENES.get(s.get("kind"), _board_scene)(m, section, utterance, s)
    total = sum(a for _, a in frames)
    return [(img, time_ * a / total) for img, a in frames]


# ---------- script ----------

def draft(name):
    """Full script from the template (text included). No LLM needed."""
    m = store.load_game(name)
    previous = []
    for old, _, _, size_ in reversed(store.list_all()):          # oldest to newest
        if size_ == m.size and old < name and store.mode_of(old) == m.mode:
            self_ = scoring.game_summary(old, store.load_game(old).valid_records)
            if self_:
                previous.append(self_)
    you = narration.script(name, m.size, m.moves, m.valid_records, m.result, previous)
    path_ = store.sub("brief", f"{name}_script.json")
    with open(path_, "w", encoding="utf-8") as f:
        json.dump(you, f, ensure_ascii=False, indent=2)
    return path_


# ---------- optional: polishing with an LLM ----------

PROMPT = """Asagida bir Go maci analiz videosunun seslendirme metni var. Iki
konusmaci sirayla konusuyor (emel, ahmet). Metin olculmus verilerden sablonla
uretildi: gercekler dogru ama cumleler kaliptan geliyor.

GOREVIN: her repligi daha akici, konusma diline yakin yeniden yaz.

KESIN KURALLAR
1. Hicbir sayiyi, koordinati (ornek "D on bir"), hamle numarasini, bolge adini
   degistirme, silme ya da yeni bir tane ekleme.
2. Yeni gercek, yorum, Go kavrami ya da taktik UYDURMA. Metinde olmayan bir
   sey soyleme. Terim tanimlarini koru.
3. Sayilar yaziyla kalsin ("on bir buçuk"). Rakam kullanma.
4. Tam diakritikli Turkce. Ikinci tekil sahis ("oynadin").
5. Her replik ayni kisinin agzindan kalsin; uzunluk yaklasik ayni.
6. Art arda repliklerin ayni kelimeyle baslamamasina dikkat et.

REPLIKLER
{replikler}

Her replik icin id ile eslesen yeni `metin` uret. Dosya okuma, arama yapma.
"""

# The English script's prompt is SEPARATE, not translated: rules 3 and 4 are
# language-specific (plain en-US here instead of Turkish vowel harmony and
# diacritics).
PROMPT_EN = """Below is the voice-over script of a Go game analysis video. Two
speakers alternate (emel, ahmet). The text was generated from measured data with
templates: the facts are correct but the sentences are formulaic.

YOUR TASK: rewrite every line to be more fluent and closer to spoken English.

HARD RULES
1. Do not change, remove or add any number, coordinate (e.g. "D eleven"), move
   number or region name.
2. Do NOT invent new facts, commentary, Go concepts or tactics. Say nothing that
   is not in the text. Keep the term definitions as they are.
3. Keep numbers spelled out ("eleven and a half"). Do not use digits.
4. Plain en-US English. Address the player as "you" ("you played").
5. Keep each line in the same speaker's voice; keep the length about the same.
6. Take care that consecutive lines do not start with the same word.

LINES
{replikler}

Produce a new `metin` for each line, matched by id. Do not read files or search.
"""

SCHEMA = {"type": "object", "properties": {"texts": {"type": "array", "items": {
    "type": "object",
    "properties": {"id": {"type": "integer"}, "text_": {"type": "string"}},
    "required": ["id", "text_"]}}}, "required": ["texts"]}


def has_llm():
    """Is agy installed. If not, the polishing option stays off in the UI."""
    return shutil.which(AGY) is not None


def _prompt(you):
    """The prompt for the script's own language (not the UI's current language)."""
    return PROMPT_EN if you.get("lang") == "en" else PROMPT


def polish(name, model=MODEL):
    """Polishes the template text with the Antigravity CLI. OPTIONAL.

    The numbers come from the template; the LLM only changes the sentence.
    Digits in the answer are spelled out, and an utterance missing from the
    answer keeps its template text.
    """
    path_ = store.sub("brief", f"{name}_script.json")
    with open(path_, encoding="utf-8") as f:
        you = json.load(f)
    utterances = [{"id": r["id"], "speaker": r["speaker"], "section": b.get("title", ""),
                  "text_": r["text_"]} for b in you["sections"] for r in b["utterances"]]
    tmp = tempfile.mkdtemp()                    # keep agy's file tools out of the project
    schema = os.path.join(tmp, "sema.json")
    with open(schema, "w", encoding="utf-8") as f:
        json.dump(SCHEMA, f)
    r = subprocess.run(
        [AGY, "-p", _prompt(you).format(
            utterances=json.dumps(utterances, ensure_ascii=False, indent=1)),
         "--json-schema", schema, "--output-format", "json", "--model", model,
         "--print-timeout", "300s"],
        cwd=tmp, capture_output=True, text=True, encoding="utf-8", check=True)
    answer = json.loads(r.stdout)
    match = {x["id"]: x["text_"] for x in answer["structured_output"]["texts"]}
    for b in you["sections"]:
        for rp in b["utterances"]:
            if match.get(rp["id"], "").strip():
                rp["text_"] = narration._without_digits(match[rp["id"]])
    with open(path_, "w", encoding="utf-8") as f:
        json.dump(you, f, ensure_ascii=False, indent=2)
    return path_, answer.get("usage", {})


# ---------- audio + montage ----------

def _duration(path_):
    """mp3 length. ffprobe is not on this machine, so ffmpeg's own output is read."""
    r = subprocess.run([FFMPEG, "-i", path_, "-f", "null", "-"],
                       capture_output=True, text=True, errors="replace")   # the path has emoji, cp1254 cannot decode it
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", r.stderr)
    if not m:
        raise RuntimeError(i18n.t(f"Ses süresi okunamadı: {path_}",
                                 f"Could not read audio duration: {path_}"))
    sa, minutes, seconds = m.groups()
    return int(sa) * 3600 + int(minutes) * 60 + float(seconds)


def _synthesize(text_, sound, path_):
    """edge-tts is called as a LIBRARY, not as a subprocess.

    `sys.executable -m edge_tts` is right from source; in the frozen app
    sys.executable is no longer python but GoMentor.exe and that call fails
    silently. It was the one line that broke packaging.

    The service now and then returns empty audio (NoAudioReceived, upstream
    #473). Repeating the same request gets through.
    """
    for attempt in range(6):   # ponytail: fixed retry; if the service goes down for good a local TTS is needed
        try:
            asyncio.run(edge_tts.Communicate(text_, sound).save(path_))
            return _duration(path_)
        except edge_tts.exceptions.NoAudioReceived:
            if attempt == 5:
                raise
            time.sleep(1 + attempt)


def video_path(name):
    """Where the game's analysis video lives (whether or not it has been made)."""
    return store.sub("video", f"{name}_analysis.mp4")


def make(name, output=None, progress_=None):
    """mp4 from the script. progress(i, n): callback per utterance (for the UI)."""
    with open(store.sub("brief", f"{name}_script.json"), encoding="utf-8") as f:
        you = json.load(f)
    if you.get("version") != 2:
        raise SystemExit(i18n.t(
            "Eski senaryo biçimi: önce `--draft` ile yeniden üret",
            "Old script format: regenerate it with `--draft` first"))
    m = store.load_game(name)
    output = output or video_path(name)
    sound = VOICES.get(you.get("lang", "tr"), VOICES["tr"])   # old script: Turkish
    jobs = [(b, r) for b in you["sections"] for r in b["utterances"] if r["text_"].strip()]
    # Frames and audio pieces are INTERMEDIATE output: they must not pile up in the data folder.
    with tempfile.TemporaryDirectory() as tmp:
        frames, sounds = [], []
        for i, (b, r) in enumerate(jobs):
            if progress_:
                progress_(i, len(jobs))
            mp3 = f"{tmp}/{i:03}.mp3"
            time_ = _synthesize(r["text_"], sound.get(r.get("speaker"), sound["speaker_a"]), mp3)
            sounds.append(mp3)
            for j, (img, s) in enumerate(scene_frames(m, b, r, time_)):
                png = f"{tmp}/{i:03}_{j:03}.png"
                img.save(png)
                frames.append((png, s))

        with open(f"{tmp}/kare.txt", "w", encoding="utf-8") as f:
            for png, time_ in frames:
                f.write(f"file '{os.path.abspath(png)}'\nduration {time_:.3f}\n")
            f.write(f"file '{os.path.abspath(frames[-1][0])}'\n")   # the concat demuxer wants the last frame
        with open(f"{tmp}/ses.txt", "w", encoding="utf-8") as f:
            for mp3 in sounds:
                f.write(f"file '{os.path.abspath(mp3)}'\n")

        subprocess.run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", f"{tmp}/kare.txt",
                        "-f", "concat", "-safe", "0", "-i", f"{tmp}/ses.txt",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "25",
                        "-c:a", "aac", "-shortest", output],
                       check=True, capture_output=True)
    return output


def has_ffmpeg():
    return os.path.isfile(FFMPEG) or shutil.which(FFMPEG) is not None


def make_all(name, llm=False, model=MODEL, progress_=None):
    """draft -> (optional polishing) -> video. What the UI button calls.

    If llm=True but agy is missing or fails, carry on with the template text:
    the video comes out no matter what, polishing is a bonus.
    """
    # ffmpeg is not bundled (to avoid carrying the GPLv3 source obligation): the
    # user installs it. If it cannot be found, say what to do instead of a raw WinError.
    if not has_ffmpeg():
        raise RuntimeError(i18n.t(
            "FFmpeg kurulu değil. Windows'ta `winget install Gyan.FFmpeg`, "
            "macOS'ta `brew install ffmpeg` çalıştır, sonra uygulamayı yeniden başlat.",
            "FFmpeg is not installed. Run `winget install Gyan.FFmpeg` on Windows "
            "or `brew install ffmpeg` on macOS, then restart the app."))
    draft(name)
    if llm and has_llm():
        try:
            polish(name, model)
        except (subprocess.CalledProcessError, OSError, ValueError, KeyError):
            pass
    return make(name, progress_=progress_)


# ---------- check ----------

def demo():
    """Every scene type draws, times are spread evenly over the audio (no audio/ffmpeg needed)."""
    moves = [["B", "E5"], ["W", "C5"], ["B", "G3"], ["W", "C3"], ["B", "A1"], ["W", "D4"]]
    m = store.Game(name="t", size=9, moves=moves)
    m.records = [
        {"move_no": 1, "move": "E5", "best_move": "E5", "points_lost": 0.0, "points": 10,
         "next_score_lead": 1.0},
        {"move_no": 3, "move": "G3", "best_move": "C7", "points_lost": 3.0, "points": 5,
         "next_score_lead": -2.0},
        {"move_no": 5, "move": "A1", "best_move": "C7", "points_lost": 6.0, "points": 2,
         "next_score_lead": -8.0}]
    section = {"title": "attempt", "subtitle": "alt " * 10}
    for s in ({"kind": "board", "move_no": 2, "rings": [["C5", "red", "oynadın"]]},
              {"kind": "continuation", "move_no": 4, "rings": [["A1", "red", "oynadın"]],
               "continuation": ["C7", "D7", "C6"]},
              {"kind": "run", "point": "C7", "move_nos": [3, 5]},
              {"kind": "score", "mark": 5}, {"kind": "edge_line"},
              {"kind": "card", "big": "Tek iş", "rows": ["uzun " * 30]}):
        rec = scene_frames(m, section, {"speaker": "speaker_b", "text_": "deneme " * 40, "scene": s}, 7.5)
        assert rec and all(img.size == (W, H) for img, _ in rec), s["kind"]
        assert abs(sum(t for _, t in rec) - 7.5) < 1e-6, (s["kind"], sum(t for _, t in rec))
    rec = scene_frames(m, section, {"scene": {"kind": "continuation", "move_no": 4, "continuation": ["C7", "D7"]}}, 5)
    assert len(rec) == 3, "continuation: position + one frame per stone"
    assert goban.replay(moves, 9, upto=2).g[goban.from_gtp("G3", 9)] == 0, "upto leaked"
    print("video ok")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # a folder name with emoji broke cp1254
    a = sys.argv[1:]
    if not a or a[0] == "--demo":
        demo()
    elif "--draft" in a:
        print(draft(a[0]))
    elif "--polish" in a:
        print(*polish(a[0]))
    elif "--all" in a:
        print(make_all(a[0], llm="--llm" in a))
    else:
        print(make(a[0]))
