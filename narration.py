#!/usr/bin/env python3
"""Game narration: a video script from the records. PURE -- knows no files, engine or audio.

No LLM. Three layers:
  1. events()    -- the events worth saying from the records, with measured numbers
  2. script()    -- arranges the events into a story skeleton (section -> utterance -> scene)
  3. TEMPLATES   -- sentence variants for each event type

The text cannot say anything that is not in the data; the risk of making
things up is structurally absent. Whoever wants can have video.write() polish
it with an LLM -- same numbers, smoother sentences.

Script format (video.py draws and voices it):
  {"game", "size", "version": 2, "sections": [
     {"title", "subtitle", "utterances": [
        {"id", "speaker": "speaker_a"|"speaker_b", "text_", "scene": {...}}]}]}
Scene kinds: board, continuation, run, score, edge_line, card (video.scene_frames).
"""
import re, zlib

import i18n, goban, scoring

# ---------- language: numbers, suffixes, coordinates ----------
# Text to speech reads digits and decimals badly; every number that goes into
# the text passes through here. Turkish suffixes follow vowel harmony.

_ONES = ["", "bir", "iki", "üç", "dört", "beş", "altı", "yedi", "sekiz", "dokuz"]
_TENS = ["", "on", "yirmi", "otuz", "kırk", "elli", "altmış", "yetmiş", "seksen", "doksan"]
_VOWELS = "aeıioöuü"


def number_text(n):
    """0 <= n < 1 000 000, an integer.

    In English DIGITS are returned: en-US edge-tts reads numbers correctly,
    the Turkish voice did not (which is why they are spelled out). Turkish
    vowel harmony needed the spoken form itself; English has no suffixes.
    """
    if i18n.selected() == "en":
        return str(int(n))
    n = int(n)
    if n == 0:
        return "sıfır"
    out = []
    if n >= 1000:
        b, n = divmod(n, 1000)
        out += ([] if b == 1 else [number_text(b)]) + ["bin"]
    if n >= 100:
        y, n = divmod(n, 100)
        out += ([] if y == 1 else [_ONES[y]]) + ["yüz"]
    if n >= 10:
        out.append(_TENS[n // 10])
    if n % 10:
        out.append(_ONES[n % 10])
    return " ".join(out)


def points_text(x):
    """Points -> words. Below 20 the nearest half ('on bir buçuk'), above it whole."""
    x = abs(x)
    if x >= 20:
        return number_text(round(x))
    y = round(x * 2) / 2
    if i18n.selected() == "en":
        return f"{y:g}"
    full_path, unfinished = int(y), y % 1 == 0.5
    if full_path == 0:
        return "yarım" if unfinished else "sıfır"
    return number_text(full_path) + (" buçuk" if unfinished else "")


def suffix(text_, kind):
    """Suffix on the LAST word of the spoken form. kind: i (accusative), e (dative), de, den, in, inci.

    In English it is the IDENTITY function: the language has no case suffixes,
    the relation is built by a preposition and that preposition is part of the
    template. So every call site in script() ('move_to', 'point_at'...) stays
    the same in both languages -- the English template reads the same key and
    simply sees it without a suffix.
    """
    if i18n.selected() == "en":
        return text_
    root_ = text_
    if root_.endswith("dört") and kind in ("i", "e", "in", "inci"):
        root_ = root_[:-1] + "d"                      # consonant softening: dordu, dorde
    end = next((c for c in reversed(text_.lower()) if c in _VOWELS), "e")
    thin = end in "eiöü"
    two = "e" if thin else "a"
    four = {"e": "i", "i": "i", "a": "ı", "ı": "ı", "o": "u", "u": "u",
            "ö": "ü", "ü": "ü"}[end]
    last_vowel = text_[-1].lower() in _VOWELS
    harsh = text_[-1].lower() in "fstkçşhp"
    d = "t" if harsh else "d"
    return root_ + {"i": ("y" if last_vowel else "") + four,
                  "e": ("y" if last_vowel else "") + two,
                  "de": d + two,
                  "den": d + two + "n",
                  "in": ("n" if last_vowel else "") + four + "n",
                  "inci": ("" if last_vowel else four) + "nc" + four}[kind]


def ordinal_text(n):
    if i18n.selected() == "en":
        n = int(n)
        ending = "th" if n % 100 in (11, 12, 13) else \
            {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
        return f"{n}{ending}"
    return suffix(number_text(n), "inci")


def coord_text(gtp):
    """'D11' -> 'D on bir' (in English 'D 11'). pass -> 'pas'/'pass'."""
    if not gtp or gtp == "pass":
        return i18n.t("pas", "pass")
    return f"{gtp[0]} {number_text(int(gtp[1:]))}"


def rank_text(label):
    """'20k' -> 'yirmi kyu', '1d' -> 'bir dan'."""
    return f"{number_text(int(label[:-1]))} {'kyu' if label[-1] == 'k' else 'dan'}"


def _without_digits(text_):
    """Safety net: spells out leftover digits. The templates already produce words;
    this keeps an overlooked number from reaching the voice as digits."""
    return re.sub(r"\d+", lambda m: number_text(int(m.group())), text_)


REGION_TR = {"top_left": "sol üst köşe", "top": "üst kenar", "top_right": "sağ üst köşe",
            "left": "sol kenar", "center": "merkez", "right": "sağ kenar",
            "bottom_left": "sol alt köşe", "bottom": "alt kenar", "bottom_right": "sağ alt köşe"}

# In the narration the region name sits inside a sentence: not the short form
# of scoring.REGION_EN ("top left") but the full form with an article ("the top
# left corner").
REGION_SPOKEN_EN = {"top_left": "the top left corner", "top": "the top edge",
                    "top_right": "the top right corner", "left": "the left edge",
                    "center": "the centre", "right": "the right edge",
                    "bottom_left": "the bottom left corner", "bottom": "the bottom edge",
                    "bottom_right": "the bottom right corner"}


def region_name_spoken(b):
    """Region key -> its name in the narration language. Unknown keys are returned as is."""
    return REGION_SPOKEN_EN.get(b, b) if i18n.selected() == "en" else REGION_TR.get(b, b)


def region_text(gtp, size):
    return region_name_spoken(
        scoring.REGIONS[scoring.region_of(goban.from_gtp(gtp, size), size)])


def lead_text(x):
    """Score from Black's (the player's) point of view -> a phrase."""
    if i18n.selected() == "en":
        if abs(x) < 1:
            return "you were level"
        return f"you were {points_text(x)} points {'ahead' if x > 0 else 'behind'}"
    if abs(x) < 1:
        return "başa baştın"
    return f"{points_text(x)} puan {'öndeydin' if x > 0 else 'gerideydin'}"


def score_swing(earlier, later):
    """(before, after) phrases. Breaks the "you were ten points ahead, then five
    points ahead" repetition: same sign -> the direction of the change; sign
    flipped -> moving ahead / falling behind."""
    o = lead_text(earlier)
    if i18n.selected() == "en":
        if abs(later) < 1:
            return o, "you stayed level"
        p = f"to {points_text(later)} points"
        if earlier >= 1 and later > 0:
            return o, f"your lead {'shrank' if later < earlier else 'grew'} {p}"
        if earlier <= -1 and later < 0:
            return o, f"the gap {'grew' if later < earlier else 'shrank'} {p}"
        return o, (f"you went {points_text(later)} points "
                   f"{'ahead' if later > 0 else 'behind'}")
    if abs(later) < 1:
        return o, "başa baş kaldın"
    p = points_text(later) + " puana"
    if earlier >= 1 and later > 0:
        return o, f"önün {p} {'indi' if later < earlier else 'çıktı'}"
    if earlier <= -1 and later < 0:
        return o, f"aradaki fark {p} {'çıktı' if later < earlier else 'indi'}"
    return o, f"{points_text(later)} puan {'öne geçtin' if later > 0 else 'geriye düştün'}"


def _tr_upper(c):
    if i18n.selected() == "en":
        return c.upper()
    return {"i": "İ", "ı": "I"}.get(c, c.upper())


def sentence_start(text_):
    """Capitalizes the first letter of every sentence (Turkish i/ı included)."""
    return re.sub(r"(^|[.!?]\s+)(\w)", lambda m: m.group(1) + _tr_upper(m.group(2)), text_)


# ---------- glossary ----------
# For beginners: the FIRST time a term comes up in the video it brings a
# one-sentence definition. Only terms of detected events -- we do not add
# concepts out of our own heads.
GLOSSARY = {
    "engine_": "Motor dediğimiz KataGo, açık kaynaklı en güçlü Go yapay zekâlarından biri; "
             "her hamleni onun gözünden ölçüyoruz.",
    "point_loss": "Puan kaybı, senin hamlenle motorun en iyi bulduğu hamle arasındaki "
                  "skor farkı demek.",
    "key_spot": "Motor bir noktayı hamleler boyunca ısrarla istiyorsa, o nokta "
                   "oynanana kadar tahtanın en değerli yeri olarak kalıyor demektir.",
    "big_point": "Büyük nokta, henüz kimsenin sahiplenmediği ve en çok alan "
                   "kazandıran boş bölgedir.",
    "liberty": "Nefes, bir taş grubuna bitişik boş noktalardır; hepsi dolarsa grup "
             "tahtadan alınır.",
    "atari": "Tek nefesi kalmış gruba atari denir: rakip bir sonraki hamlede o grubu alabilir.",
    "capture": "Esir, tahtadan alınan taştır. Çin kuralında skoru esirler değil, tahtadaki "
            "taşlar ve çevrelediğin boş alan belirler.",
    "own_area": "Kendi bölgenin içine taş koymak Çin kuralında puanı değiştirmez, "
                   "ama bir hamleyi boşa harcar.",
    "edge_line": "Hat, kenardan uzaklıktır: birinci hat tahtanın en dış çizgisi. Oraya konan "
           "taş çok az alan çevreler; bölge ve etki dengesi üçüncü ve dördüncü hatta kurulur.",
    "komi": "Komi, ilk hamleyi yapan siyahın avantajını dengelemek için beyaza verilen puan.",
    "kyu": "Kyu, Go'da başlangıç rankıdır; sayı küçüldükçe oyuncu güçlenir, "
           "birinci kyudan sonra dan başlar.",
    "sp": "SP, dereceli maçlarda kazanıp kaybettiğin seviye puanı; yüz SP bir rank.",
}


# ---------- templates ----------
# Variants for each event type: v (event data) -> sentence. The choice is
# deterministic from the game name (crc32). Adding variants is fine; numbers
# and suffixes always come from the helpers.
TEMPLATES = {
    "intro_win": [
        lambda v: f"Bu maçı {v['diff']} puanla kazandın. {v['opponent_sentence']}",
        lambda v: f"Sonuç senden yana: {v['diff']} puan farkla galibiyet. {v['opponent_sentence']}",
        lambda v: f"Maç bitti ve kazanan sensin, {v['diff']} puan farkla. {v['opponent_sentence']}",
    ],
    "intro_loss": [
        lambda v: f"Bu maçı {v['diff']} puan farkla kaybettin. {v['opponent_sentence']}",
        lambda v: f"Sonuç bu sefer rakipten yana: {v['diff']} puan. {v['opponent_sentence']}",
        lambda v: f"Maç {v['diff']} puanlık bir kayıpla bitti. {v['opponent_sentence']}",
    ],
    "intro_draw": [
        lambda v: f"Maç berabere bitti. {v['opponent_sentence']}",
    ],
    "intro_loss_summary": [
        lambda v: f"{v['n']} hamlende toplam {v['lost']} puan kaybettin, hamle başına {v['mean']}.",
        lambda v: f"Motorun gözünden bakınca {v['n']} hamlede {v['lost']} puan bıraktın; "
                  f"hamle başına ortalama {v['mean']}.",
        lambda v: f"Tabloya bakalım: {v['n']} hamle, toplam {v['lost']} puan kayıp, "
                  f"hamle başına {v['mean']}.",
    ],
    "hook_drop": [
        lambda v: f"Ama asıl hikâye {v['move']}: o hamleden önce {v['earlier']}, sonra {v['later']}.",
        lambda v: f"Maçın kırıldığı an {v['move']}. Hamleden önce {v['earlier']}; "
                  f"hamleden sonra {v['later']}.",
        lambda v: f"Bir anı aklında tut: {v['move']}. Öncesinde {v['earlier']}, "
                  f"sonrasında {v['later']}.",
    ],
    "plan": [
        lambda v: f"Bugün {v['n']} şeye bakacağız, en pahalısından başlayarak.",
        lambda v: f"Videoda {v['n']} durak var. İlki en çok puan götüren.",
        lambda v: f"Şimdi {v['n']} başlıkta maçı açalım.",
    ],
    "run_intro": [
        lambda v: f"Hikâyenin adı {v['point']}. Motor bu noktayı {v['start']} ile {v['end']} "
                  f"arasında {v['n']} kez istedi, sen her seferinde başka yere oynadın.",
        lambda v: f"{v['point_obj_cap']} izle. {v['start']} ile {v['end']} arasında tam {v['n']} kez "
                  f"en iyi hamle oydu. Nokta boş bekledi.",
        lambda v: f"Tahtada bir nokta seni bekledi: {v['point']}. Motor onu {v['n']} kez istedi, "
                  f"{v['start']} ile {v['end']} arasında.",
    ],
    "run_cost": [
        lambda v: f"Bu tek nokta sana {v['lost']} puana mal oldu; maçtaki kaybının {v['margin']}.",
        lambda v: f"Bedeli {v['lost']} puan. Bu, bütün maçtaki kaybının {v['margin']}.",
        lambda v: f"Toplam fatura {v['lost']} puan, yani kaybının {v['margin']}.",
    ],
    "run_interrupted": [
        lambda v: f"Arada {v['k']} hamle motor başka bir yeri biraz öne koydu, ama nokta soğumadı.",
        lambda v: f"Arada {v['k']} kez motor başka yere baktı; sonra yine {v['point_to']} döndü.",
    ],
    "what_happened": [
        lambda v: f"{v['move_cap']} {v['played_to']} oynadın ve {v['lost']} puan kaybettin.",
        lambda v: f"{v['move_cap']} senin hamlen {v['played_move']}. Motorun gözünde bu {v['lost']} "
                  f"puanlık bir kayıp.",
        lambda v: f"Sıra {v['move_to']} geldi. Oynadığın {v['played_move']}, bedeli {v['lost']} puan.",
    ],
    "review": [
        lambda v: f"Aynı noktayı {v['move_at']} de kaçırdın: {v['lost']} puan daha.",
        lambda v: f"{v['move_cap_at']} de motor yine aynı yeri istedi; {v['lost']} puan daha gitti.",
    ],
    "move_score": [
        lambda v: f"Skor tahmini: önce {v['earlier']}, sonra {v['later']}.",
        lambda v: f"Hamleden önce {v['earlier']}; sonra {v['later']}.",
    ],
    "move_region": [
        lambda v: f"En çok değişen yer {v['region']}, {v['change']} puan.",
        lambda v: f"Kayıp en çok {v['region_at']} oldu: {v['change']} puanlık değişim.",
    ],
    "continuation": [
        lambda v: f"Motorun planı şuydu: {v['line']}.",
        lambda v: f"Motor burada şöyle devam ederdi: {v['line']}.",
        lambda v: f"Doğru yol şuydu: {v['line']}.",
    ],
    "continue_opponent": [
        lambda v: f"Oyunda rakip {v['answer_to']} cevap verdi.",
        lambda v: f"Senin hamlenden sonra rakip {v['answer_to']} oynadı.",
    ],
    "big_point": [
        lambda v: f"Tahtanın asıl boş yeri {v['empty']} idi, motorun istediği {v['point']} de oradaydı. "
                  f"Sen {v['yours_at']} oynadın.",
        lambda v: f"Boşluk hesabı net: en büyük boş alan {v['empty']}. Sen ise {v['yours_at']} oynadın.",
    ],
    "one_liberty": [
        lambda v: f"Bu hamleden sonra {v['dimension']} taşlık grubunun tek nefesi kaldı, "
                  f"{v['point']}. Rakip oraya oynarsa hepsi gider.",
        lambda v: f"Dikkat: {v['dimension']} taşlık grup atariye düştü. Son nefesi {v['point']}.",
    ],
    "two_liberties": [
        lambda v: f"{v['dimension']} taşlık grubun iki nefese indi; bir hamle daha ve atari.",
        lambda v: f"Grubun, {v['dimension']} taş, yalnız iki nefesle kaldı.",
    ],
    "capture": [
        lambda v: f"Rakip {v['point_to']} oynarsa {v['k']} taşını esir alıyor.",
        lambda v: f"Bu hamle rakibe fırsat verdi: {v['point']} ile {v['k']} taş alınıyor.",
    ],
    "own_area": [
        lambda v: "Bu nokta zaten senin bölgendi.",
        lambda v: "Oynadığın yer zaten sana aitti.",
    ],
    "good_move": [
        lambda v: f"Şimdi iyi bir an. {v['move_cap']} {v['played_obj']} buldun ve bu tek doğru "
                  f"hamleydi: ikinci en iyi seçenek bile {v['diff']} puan kaybettirirdi.",
        lambda v: f"Bir de alkışlık hamle var. {v['move_cap']} {v['played_move']}: motorun birinci "
                  f"tercihi. Sonraki seçenek {v['diff']} puan geride kalıyordu.",
        lambda v: f"{v['move_cap']} {v['played_obj']} oynadın ve tam isabet. Başka her hamle "
                  f"en az {v['diff']} puan götürürdü.",
    ],
    "clean_run": [
        lambda v: f"{v['start']} ile {v['end']} arasında {v['n']} hamle boyunca hiçbir hamlen bir "
                  f"puandan fazla kaybettirmedi.",
        lambda v: f"{v['n']} hamlelik temiz bir dönem var: {v['start']} ile {v['end']} arası, "
                  f"her hamle bir puanın altında.",
    ],
    "edge_line": [
        lambda v: f"Taşlarının {v['me']} birinci hatta. Motor aynı maçta birinci hatta "
                  f"{v['engine_']} oynardı.",
        lambda v: f"Kenar alışkanlığı: hamlelerinin {v['me']} en dış çizgide. Motorda bu oran "
                  f"{v['engine_']}.",
    ],
    "stage": [
        lambda v: f"En temiz evren {v['good_']}: hamle başına {v['good_mean']} puan. En zoru "
                  f"{v['bad']}, hamle başına {v['bad_mean']}.",
        lambda v: f"Evrelere bölünce: {v['good_']} en iyisi, hamle başına {v['good_mean']}; "
                  f"{v['bad']} hamle başına {v['bad_mean']} ile en pahalısı.",
    ],
    "turnaround": [
        lambda v: f"Skor maç boyunca {v['n']} kez el değiştirdi.",
        lambda v: f"Üstünlük {v['n']} kez el değiştirdi; bu maç tek yönlü değildi.",
    ],
    "trend_good": [
        lambda v: f"Önceki {v['k']} maçına göre hamle başına kaybın {v['previous']} puandan "
                  f"{v['now']} puana indi. Bu gelişme.",
        lambda v: f"Son {v['k']} maçının ortalaması hamle başına {v['previous']} puandı; bugün "
                  f"{v['now']}. Doğru yöndesin.",
    ],
    "trend_bad": [
        lambda v: f"Önceki {v['k']} maçında hamle başına {v['previous']} puan kaybediyordun; "
                  f"bugün {v['now']}. Bu maç ortalamanın altında kaldı.",
        lambda v: f"Son {v['k']} maçın ortalaması {v['previous']} puandı, bugün {v['now']}. "
                  f"Bir adım geri, olur.",
    ],
    "sp": [
        lambda v: f"Dereceli sonuç: {v['change']} SP. Rankın şimdi {v['rank']}.",
        lambda v: f"Bu maç sana {v['change']} SP getirdi; rankın {v['rank']}.",
    ],
    "placement": [
        lambda v: f"Yerleştirme sürüyor: {v['remaining']} maç kaldı.",
    ],
    "placement_done": [
        lambda v: f"Yerleştirme tamamlandı, başlangıç rankın {v['rank']}.",
    ],
    "strong": [
        lambda v: f"Güçlü yanını da söyleyelim: {v['text_']}",
        lambda v: f"Unutma, iyi olduğun yer de var: {v['text_']}",
    ],
    "single_job": [
        lambda v: f"Tek iş: {v['advice']} Bu maçta bu alışkanlık {v['lost']} puana mal oldu.",
        lambda v: f"Bir sonraki maç için tek şey: {v['advice']} Bugün bunun bedeli {v['lost']} puandı.",
    ],
}

SUGGESTION = {
    "keystone": "her hamleden önce kendine sor: tahtanın en büyük boş yeri neresi, ve "
             "hâlâ boş bekleyen bir kilit nokta var mı?",
    "edge_line": "kenara yapışma. Taşlarını üçüncü ve dördüncü hatta oyna, birinci hattı "
           "sınırı kapatırken kullan.",
    "liberty": "taş koymadan önce dokunduğun grubun nefeslerini say. İkiye düştüyse önce onu kurtar.",
    "capture": "hamleni yapmadan önce rakibin cevabına bak: bu hamleden sonra taş alabiliyor mu?",
    "own": "kendi bölgenin içine taş koyma; o hamleyi sınırını büyütmeye harca.",
}


# ---------- English tables ----------
# A SEPARATE table, not a line-by-line translation: a template's job is to read
# smoothly, and a literal translation of the Turkish sentence sounds stiff in
# English. The keys and {v} fields are THE SAME -- script() sends the same data
# for both languages. No suffixes (suffix() is the identity in English); the
# template's own preposition builds the relation.
GLOSSARY_EN = {
    "engine_": "The engine we keep mentioning is KataGo, one of the strongest open source "
             "Go AIs; every move of yours is measured through its eyes.",
    "point_loss": "Point loss means the score difference between your move and the best "
                  "move the engine found.",
    "key_spot": "If the engine keeps insisting on one point move after move, that "
                   "point stays the most valuable spot on the board until someone plays it.",
    "big_point": "The big point is the open area nobody owns yet and the one that wins "
                   "the most territory.",
    "liberty": "Liberties are the empty points next to a group of stones; if they all fill "
             "up, the group comes off the board.",
    "atari": "A group down to one liberty is in atari: the opponent can capture it with "
             "the next move.",
    "capture": "A capture is a stone taken off the board. Under Chinese rules the score comes "
            "from the stones on the board and the empty area you surround, not captures.",
    "own_area": "Playing inside your own territory does not change the score under "
                   "Chinese rules, but it throws away a move.",
    "edge_line": "The line is the distance from the edge: the first line is the outermost row. "
           "A stone there surrounds very little; the balance of territory and influence is "
           "built on the third and fourth lines.",
    "komi": "Komi is the points given to white to offset black's advantage of moving first.",
    "kyu": "Kyu is the beginner rank in Go; the smaller the number the stronger the "
           "player, and after the first kyu the dan ranks begin.",
    "sp": "SP is the skill points you win and lose in ranked games; one hundred SP is one rank.",
}

SUGGESTION_EN = {
    "keystone": "before every move, ask yourself: where is the biggest open space on the "
             "board, and is there still a key point sitting there empty?",
    "edge_line": "do not hug the edge. Play your stones on the third and fourth lines, and use "
           "the first line for closing off a border.",
    "liberty": "count the liberties of the group you are touching before you place a stone. "
             "If it is down to two, save that first.",
    "capture": "before you play, look at your opponent's answer: can they capture anything "
            "after this move?",
    "own": "do not play inside your own territory; spend that move on making your "
             "border bigger.",
}

TEMPLATES_EN = {
    "intro_win": [
        lambda v: f"You won this game by {v['diff']} points. {v['opponent_sentence']}",
        lambda v: f"The result goes your way: a win by {v['diff']} points. {v['opponent_sentence']}",
        lambda v: f"The game is over and you are the winner, by {v['diff']} points. "
                  f"{v['opponent_sentence']}",
    ],
    "intro_loss": [
        lambda v: f"You lost this game by {v['diff']} points. {v['opponent_sentence']}",
        lambda v: f"This time the result goes to your opponent: {v['diff']} points. "
                  f"{v['opponent_sentence']}",
        lambda v: f"The game ended in a {v['diff']} point loss. {v['opponent_sentence']}",
    ],
    "intro_draw": [
        lambda v: f"The game ended in a draw. {v['opponent_sentence']}",
    ],
    "intro_loss_summary": [
        lambda v: f"Across {v['n']} moves you lost {v['lost']} points in total, "
                  f"{v['mean']} per move.",
        lambda v: f"Seen through the engine's eyes you left {v['lost']} points on the "
                  f"board over {v['n']} moves; {v['mean']} per move on average.",
        lambda v: f"Here is the tally: {v['n']} moves, {v['lost']} points lost in total, "
                  f"{v['mean']} per move.",
    ],
    "hook_drop": [
        lambda v: f"But the real story is {v['move']}: before that move {v['earlier']}, "
                  f"after it {v['later']}.",
        lambda v: f"The game broke at {v['move']}. Before the move {v['earlier']}; after the "
                  f"move {v['later']}.",
        lambda v: f"Keep one moment in mind: {v['move']}. Before it {v['earlier']}, "
                  f"after it {v['later']}.",
    ],
    "plan": [
        lambda v: f"We will look at {v['n']} things today, starting with the most expensive.",
        lambda v: f"There are {v['n']} stops in this video. The first one cost you the most.",
        lambda v: f"Let us open the game up under {v['n']} headings.",
    ],
    "run_intro": [
        lambda v: f"The story is called {v['point']}. The engine asked for that point "
                  f"{v['n']} times between {v['start']} and {v['end']}, and every time you "
                  f"played somewhere else.",
        lambda v: f"Watch {v['point_obj_cap']}. Between {v['start']} and {v['end']} it was "
                  f"the best move {v['n']} times over. The point sat there empty.",
        lambda v: f"One point on the board was waiting for you: {v['point']}. The engine "
                  f"asked for it {v['n']} times, between {v['start']} and {v['end']}.",
    ],
    "run_cost": [
        lambda v: f"That single point cost you {v['lost']} points — {v['margin']} of "
                  f"everything you lost in this game.",
        lambda v: f"The bill is {v['lost']} points, which is {v['margin']} of your whole "
                  f"loss in this game.",
        lambda v: f"The total comes to {v['lost']} points, {v['margin']} of your loss.",
    ],
    "run_interrupted": [
        lambda v: f"In between, {v['k']} moves put something else slightly ahead for the "
                  f"engine, but the point never went cold.",
        lambda v: f"In between the engine looked elsewhere {v['k']} times; then it came "
                  f"back to {v['point_to']}.",
    ],
    # move_cap only at the START OF A SENTENCE: _big() capitalizes the first
    # letter, and mid-sentence it read like "On Move 3". Mid-sentence uses move_to
    # (no suffix).
    "what_happened": [
        lambda v: f"{v['move_cap']}: you played {v['played_to']} and lost "
                  f"{v['lost']} points.",
        lambda v: f"{v['move_cap']}: your move was {v['played_move']}, and in the "
                  f"engine's eyes it cost {v['lost']} points.",
        lambda v: f"Then came {v['move_to']}. You played {v['played_move']}, and it cost "
                  f"{v['lost']} points.",
    ],
    "review": [
        lambda v: f"You missed the same point on {v['move_at']} as well: {v['lost']} "
                  f"points more.",
        lambda v: f"The engine asked for the same spot again on {v['move_at']}; "
                  f"another {v['lost']} points gone.",
    ],
    "move_score": [
        lambda v: f"The score estimate: before, {v['earlier']}; after, {v['later']}.",
        lambda v: f"Before the move {v['earlier']}; after it {v['later']}.",
    ],
    "move_region": [
        lambda v: f"The area that changed most is {v['region']}, by {v['change']} points.",
        lambda v: f"Most of the loss happened in {v['region_at']}: a {v['change']} point swing.",
    ],
    "continuation": [
        lambda v: f"The engine's plan was this: {v['line']}.",
        lambda v: f"Here the engine would have carried on like this: {v['line']}.",
        lambda v: f"The right line was this: {v['line']}.",
    ],
    "continue_opponent": [
        lambda v: f"In the game your opponent answered with {v['answer_to']}.",
        lambda v: f"After your move the opponent played {v['answer_to']}.",
    ],
    "big_point": [
        lambda v: f"The real open space on the board was {v['empty']}, and the point the "
                  f"engine wanted, {v['point']}, was right there. You played in "
                  f"{v['yours_at']}.",
        lambda v: f"The space count is clear: the biggest open area is {v['empty']}. You, "
                  f"meanwhile, played in {v['yours_at']}.",
    ],
    "one_liberty": [
        lambda v: f"After this move your {v['dimension']} stone group has a single liberty "
                  f"left, {v['point']}. If your opponent plays there, all of it goes.",
        lambda v: f"Careful: your {v['dimension']} stone group is in atari. Its last liberty "
                  f"is {v['point']}.",
    ],
    "two_liberties": [
        lambda v: f"Your {v['dimension']} stone group is down to two liberties; one more move "
                  f"and it is in atari.",
        lambda v: f"Your group, {v['dimension']} stones, is left with only two liberties.",
    ],
    "capture": [
        lambda v: f"If your opponent plays {v['point_to']}, they capture {v['k']} of your stones.",
        lambda v: f"This move handed your opponent a chance: {v['point']} captures "
                  f"{v['k']} stones.",
    ],
    "own_area": [
        lambda v: "That point was already your own territory.",
        lambda v: "The spot you played already belonged to you.",
    ],
    "good_move": [
        lambda v: f"Now for a good moment. On {v['move_to']} you found "
                  f"{v['played_obj']}, and it was the only right move: even the second best "
                  f"option would have cost {v['diff']} points.",
        lambda v: f"There is a move worth applauding too. On {v['move_to']}, "
                  f"{v['played_move']} was the engine's first choice. The next option was "
                  f"{v['diff']} points behind.",
        lambda v: f"On {v['move_to']} you played {v['played_obj']} and hit it exactly. "
                  f"Every other move would have cost at least {v['diff']} points.",
    ],
    "clean_run": [
        lambda v: f"Between {v['start']} and {v['end']}, across {v['n']} moves, not one of "
                  f"your moves cost more than a point.",
        lambda v: f"There is a clean stretch of {v['n']} moves: between {v['start']} and "
                  f"{v['end']}, every move under a point.",
    ],
    "edge_line": [
        lambda v: f"{v['me']} of your stones are on the first line. In the same game the "
                  f"engine would have played {v['engine_']} there.",
        lambda v: f"An edge habit: {v['me']} of your moves are on the outermost row. For "
                  f"the engine that share is {v['engine_']}.",
    ],
    "stage": [
        lambda v: f"Your cleanest phase is {v['good_']}: {v['good_mean']} points per move. The "
                  f"hardest is {v['bad']}, at {v['bad_mean']} per move.",
        lambda v: f"Split into phases: {v['good_']} is the best, at {v['good_mean']} per move; "
                  f"{v['bad']} is the most expensive, at {v['bad_mean']} per move.",
    ],
    "turnaround": [
        lambda v: f"The score changed hands {v['n']} times over the game.",
        lambda v: f"The lead changed hands {v['n']} times; this game was not one sided.",
    ],
    "trend_good": [
        lambda v: f"Against your previous {v['k']} games your loss per move came down from "
                  f"{v['previous']} points to {v['now']}. That is progress.",
        lambda v: f"Your last {v['k']} games averaged {v['previous']} points per move; today "
                  f"it is {v['now']}. You are heading the right way.",
    ],
    "trend_bad": [
        lambda v: f"Over your previous {v['k']} games you were losing {v['previous']} points "
                  f"per move; today it is {v['now']}. This game came in below your average.",
        lambda v: f"Your last {v['k']} games averaged {v['previous']} points, today "
                  f"{v['now']}. One step back, it happens.",
    ],
    "sp": [
        lambda v: f"Ranked result: {v['change']} SP. Your rank is now {v['rank']}.",
        lambda v: f"This game brought you {v['change']} SP; your rank is {v['rank']}.",
    ],
    "placement": [
        lambda v: f"Placement is still running: {v['remaining']} games to go.",
    ],
    "placement_done": [
        lambda v: f"Placement is complete, and your starting rank is {v['rank']}.",
    ],
    "strong": [
        lambda v: f"Let us name your strength as well: {v['text_']}",
        lambda v: f"Remember, there is something you are good at too: {v['text_']}",
    ],
    "single_job": [
        lambda v: f"One job: {v['advice']} In this game that habit cost {v['lost']} points.",
        lambda v: f"One thing for the next game: {v['advice']} Today it cost you "
                  f"{v['lost']} points.",
    ],
}


def _template():
    return TEMPLATES_EN if i18n.selected() == "en" else TEMPLATES


def _glossary():
    return GLOSSARY_EN if i18n.selected() == "en" else GLOSSARY


def _suggestion():
    return SUGGESTION_EN if i18n.selected() == "en" else SUGGESTION


class Narrator:
    """Variant choice + term memory. One per game."""

    def __init__(self, seed):
        self.seed = seed
        self.counter = {}
        self.narrated = set()

    def say(self, kind, **v):
        exists = _template()[kind]
        i = self.counter.get(kind, 0)
        self.counter[kind] = i + 1
        # If the same kind comes up again, the next variant: never the same template twice in a row.
        choice = (zlib.crc32(f"{self.seed}:{kind}".encode()) + i) % len(exists)
        return exists[choice](v)

    def term(self, key_):
        """The definition if the term comes up for the first time, otherwise an empty string."""
        if key_ in self.narrated:
            return ""
        self.narrated.add(key_)
        return _glossary()[key_]


# ---------- events ----------

def _percent(x):
    """0.24 -> 'yüzde yirmi dört'; round ratios like a quarter read like a person would say them."""
    # The English form has no "of it": the template builds its own relation,
    # either "X of your stones" or "-- X of everything you lost".
    for ratio, tr, en in ((0.25, "dörtte biri", "a quarter"),
                         (1 / 3, "üçte biri", "a third"),
                         (0.5, "yarısı", "half"),
                         (2 / 3, "üçte ikisi", "two thirds"),
                         (0.75, "dörtte üçü", "three quarters")):
        if abs(x - ratio) < 0.02:
            return i18n.t(tr, en)
    return i18n.t(f"yüzde {number_text(round(100 * x))}",
                 f"{number_text(round(100 * x))} percent")


def stages(records):
    """[(name, [record...])] -- opening / middle game / endgame, in thirds."""
    k = scoring._ordered(records)
    n = len(k)
    return [(name, k[lo:hi]) for name, lo, hi in
            ((i18n.t("açılış", "the opening"), 0, n // 3),
             (i18n.t("orta oyun", "the middle game"), n // 3, 2 * n // 3),
             (i18n.t("bitiriş", "the endgame"), 2 * n // 3, n))
            if k[lo:hi]]


def _rotations(k, threshold=3.0):
    state_, n = 0, 0
    for r in k:
        x = r.get("next_score_lead", 0.0)
        new = 1 if x > threshold else -1 if x < -threshold else state_
        if state_ and new != state_:
            n += 1
        state_ = new
    return n


def _clean_run(k, threshold=1.0, at_least=6):
    en, start = None, 0
    for i in range(len(k) + 1):
        if i == len(k) or k[i].get("points_lost", 0.0) >= threshold:
            if i - start >= at_least and (not en or i - start > en[2]):
                en = (k[start]["move_no"], k[i - 1]["move_no"], i - start)
            start = i + 1
    return en


def _good_move(k):
    """Played = the engine's first choice AND the second option >= 3p behind. The largest gap."""
    candidate = [(r["alternatives"][1]["diff"], r) for r in k
            if scoring.category(r) == "great"]
    return max(candidate, key=lambda t: t[0]) if candidate else None


def _findings(moves, size, r):
    """Findings as structures (sentences in old records are parsed in scoring)."""
    return [b for b in (scoring.finding_struct(x) for x in scoring.findings_full(moves, size, r)) if b]


def events(moves, size, records):
    """The dict of measured events. Independent of the narration -- the testable core."""
    k = scoring._ordered(records)
    n = len(k)
    top = sum(r.get("points_lost", 0.0) for r in k)
    sr = sorted(scoring.runs(k), key=lambda s: -s["lost"])
    in_run = {id(r) for s in sr[:2] for r in s["records"]}
    points = {s["move"] for s in sr[:2]}
    # A second expensive move asking for the same best move does not get its own
    # section; it is added to the first as "you missed the same point at X too".
    # Moves asking for the run's point are skipped.
    expensive, review = [], {}
    for r in sorted(k, key=lambda r: -r.get("points_lost", 0.0)):
        if id(r) in in_run or r.get("points_lost", 0.0) < 5:
            continue
        ei = r.get("best_move")
        es = next((x for x in expensive if x.get("best_move") == ei), None)
        if es:
            review.setdefault(id(es), []).append(r)
        elif ei not in points and len(expensive) < 3:
            expensive.append(r)
    game_ = [r for r in k if r.get("move") != "pass"]      # passes are not clean play

    me, engine_ = scoring.line_distribution(k, size)
    nb, nm = sum(me.values()), sum(engine_.values())
    line_ = None
    if nb >= 8 and nm:
        b1, m1 = me.get(1, 0) / nb, engine_.get(1, 0) / nm
        if me.get(1, 0) >= 3 and b1 >= 0.15 and b1 >= 2 * m1:
            line_ = (b1, m1)

    # One job: the habit with the highest measured cost.
    cost = {"keystone": sum(s["lost"] for s in sr)}
    for r in k:
        kp = r.get("points_lost", 0.0)
        h, he = scoring.line_of(r.get("move"), size), scoring.line_of(r.get("best_move"), size)
        if h and he and h <= 2 and he >= 3:
            cost["edge_line"] = cost.get("edge_line", 0.0) + kp
        for b in _findings(moves, size, r):
            key_ = {"one_liberty": "liberty", "two_liberties": "liberty", "capture": "capture",
                       "own_area": "own"}.get(b["kind"])
            if key_:
                cost[key_] = cost.get(key_, 0.0) + kp
    # Run loss overlaps with line loss (same moves); both are real, the larger one wins.
    single_job = max(cost.items(), key=lambda t: t[1]) if any(cost.values()) else None

    ev = [(name, sum(r.get("points_lost", 0.0) for r in g) / len(g)) for name, g in stages(game_)]
    return {
        "n": n, "lost": top,
        "drop": max(k, key=lambda r: r.get("points_lost", 0.0)) if k else None,
        "runs": sr[:2], "expensive": [(r, review.get(id(r), [])) for r in expensive],
        "good_": _good_move(game_), "clean": _clean_run(game_),
        "edge_line": line_, "stages": ev, "turnaround": _rotations(k),
        "single_job": single_job,
    }


# ---------- script ----------

def _move(n):
    return i18n.t(f"{ordinal_text(n)} hamle", f"move {number_text(n)}")


def _big(s):
    return s[:1].upper() + s[1:]


def _line_text(resume, n=4):
    who = [i18n.t("sen", "you"), i18n.t("rakip", "the opponent")]
    return ", ".join(f"{who[i % 2]} {coord_text(mv)}" for i, mv in enumerate(resume[:n]))


def script(name, size, moves, records, result_=None, previous=None):
    """Sections -> utterances -> scene. result: store.Game.result; previous: the
    game_summary list of earlier games of the same size (oldest first), for the trend."""
    o = events(moves, size, records)
    a = Narrator(name)
    k = scoring._ordered(records)
    sections, rid = [], [0]
    speaker = ["speaker_a"]

    def utterance(text_, scene, who=None):
        who = who or speaker[0]
        speaker[0] = "speaker_b" if who == "speaker_a" else "speaker_a"
        rid[0] += 1
        return {"id": rid[0], "speaker": who,
                "text_": sentence_start(_without_digits(" ".join(text_.split()))), "scene": scene}

    def section(title, subtitle, utterances):
        sections.append({"title": title, "subtitle": subtitle, "utterances": utterances})

    def rings(r, good_=True):
        h = ([[r["move"], "red", i18n.t("oynadın", "you played")]]
             if r.get("move") not in (None, "pass") else [])
        if good_ and r.get("best_move") not in (None, "pass", r.get("move")):
            h.append([r["best_move"], "green", i18n.t("olmalıydı", "should have been")])
        return h

    # --- intro ---
    result_ = result_ or {}
    S = result_.get("won")
    diff = result_.get("score")
    if diff is None and k:
        ends = sorted(r.get("next_score_lead", 0.0) for r in k[-5:])
        diff = ends[len(ends) // 2]
    if S is None and diff is not None:
        S = 1.0 if diff > 0 else 0.0 if diff < 0 else 0.5
    opponent = result_.get("opponent")
    opponent_sentence = (i18n.t(f"Rakibin {rank_text(opponent)} seviyesindeki bottu.",
                         f"Your opponent was a bot at {rank_text(opponent)} level.")
                   if opponent else "")
    v = {"diff": points_text(diff or 0), "opponent_sentence": opponent_sentence}
    kind = "intro_win" if S == 1 else "intro_loss" if S == 0 else "intro_draw"
    r1 = a.say(kind, **v) + " " + a.say(
        "intro_loss_summary", n=number_text(o["n"]), lost=points_text(o["lost"]),
        mean=points_text(o["lost"] / max(o["n"], 1)))
    r1 += " " + a.term("engine_") + " " + a.term("point_loss")
    intro = [utterance(r1, {"kind": "score", "mark": None})]
    d = o["drop"]
    if d and d.get("points_lost", 0) >= 5:
        earlier, later = score_swing(d.get("prev_score_lead", 0), d.get("next_score_lead", 0))
        intro.append(utterance(a.say(
            "hook_drop", move=_move(d["move_no"]), earlier=earlier, later=later),
            {"kind": "score", "mark": d["move_no"]}))
    stop_ = len(o["runs"]) + len(o["expensive"]) + (1 if o["edge_line"] else 0)
    if stop_:
        intro[-1]["text_"] += " " + sentence_start(a.say("plan", n=number_text(stop_)))
    section(i18n.t("Maç özeti", "Game summary"), "", intro)

    # --- runs ---
    for s in o["runs"]:
        point = coord_text(s["move"])
        first_ = s["records"][0]
        rr = []
        text_ = a.say("run_intro", point=point, point_obj_cap=_big(suffix(point, "i")),
                        n=number_text(s["n"]), start=_move(s["start"]),
                        end=_move(s["end"]))
        text_ += " " + a.term("key_spot")
        rr.append(utterance(text_, {"kind": "run", "point": s["move"],
                                 "move_nos": [x["move_no"] for x in s["records"]]}))
        text_ = a.say("run_cost", lost=points_text(s["lost"]),
                        margin=_percent(s["lost"] / max(o["lost"], 1)))
        if s["interrupted"]:
            text_ += " " + a.say("run_interrupted", k=number_text(len(s["interrupted"])),
                                   point_to=suffix(point, "e"))
        en = max(s["records"], key=lambda r: r.get("points_lost", 0.0))
        for g in _findings(moves, size, en):
            if g["kind"] == "big_point":
                text_ += " " + a.say("big_point", empty=region_name_spoken(g["empty"]),
                                       point=coord_text(g["point"]),
                                       yours_at=suffix(region_name_spoken(g["yours"]), "de"))
                text_ += " " + a.term("big_point")
                break
        rr.append(utterance(text_, {"kind": "board", "move_no": en["move_no"] - 1,
                                 "rings": rings(en)}))
        if en.get("katago_line"):
            rr.append(utterance(
                a.say("what_happened", move=_move(en["move_no"]),
                        move_cap=_big(suffix(_move(en["move_no"]), "de")),
                        played_move=coord_text(en["move"]),
                        move_to=suffix(_move(en["move_no"]), "e"),
                        played_to=suffix(coord_text(en["move"]), "e"),
                        lost=points_text(en["points_lost"]))
                + " " + a.say("continuation", line=_line_text(en["katago_line"])),
                {"kind": "continuation", "move_no": en["move_no"] - 1, "rings": rings(en),
                 "continuation": en["katago_line"][:4]}))
        section(i18n.t(f"Kilit nokta: {s['move']}", f"Key point: {s['move']}"),
              i18n.t(f"{s['n']} kez · ", f"{s['n']} times · ")
              + f"{s['start']}–{s['end']} · {s['lost']:.0f}p", rr)

    # --- expensive single moves ---
    for r, reviews in o["expensive"]:
        h = _move(r["move_no"])
        oy = coord_text(r["move"])
        rr = []
        text_ = a.say("what_happened", move_to=suffix(h, "e"), move_cap=_big(suffix(h, "de")),
                        played_move=oy, played_to=suffix(oy, "e"), lost=points_text(r["points_lost"]))
        earlier, later = score_swing(r.get("prev_score_lead", 0), r.get("next_score_lead", 0))
        text_ += " " + a.say("move_score", earlier=earlier, later=later)
        for x in reviews[:1]:
            hx = suffix(_move(x["move_no"]), "de")
            text_ += " " + a.say("review", move_at=hx, move_cap_at=_big(hx),
                                   lost=points_text(x["points_lost"]))
        if r.get("most_changed_region") and r.get("region_change_points", 0) >= 3:
            bt = region_name_spoken(r["most_changed_region"])
            text_ += " " + a.say("move_region", region=bt, region_at=suffix(bt, "de"),
                                   change=points_text(r["region_change_points"]))
        rr.append(utterance(text_, {"kind": "board", "move_no": r["move_no"] - 1,
                                 "rings": rings(r)}))
        # Why: measured board findings first, then the engine's continuation.
        reason = []
        for g in _findings(moves, size, r):
            kind = g["kind"]
            if kind == "one_liberty":
                reason.append(a.say("one_liberty", dimension=number_text(g["dimension"]),
                                     point=coord_text(g["point"])))
                reason.append(a.term("liberty"))
                reason.append(a.term("atari"))
            elif kind == "two_liberties":
                reason.append(a.say("two_liberties", dimension=number_text(g["dimension"])))
                reason.append(a.term("liberty"))
            elif kind == "capture":
                reason.append(a.say("capture", point=coord_text(g["point"]),
                                     point_to=suffix(coord_text(g["point"]), "e"),
                                     k=number_text(g["num"])))
                reason.append(a.term("capture"))
            elif kind == "own_area":
                reason.append(a.say("own_area"))
                reason.append(a.term("own_area"))
            elif kind == "big_point":
                reason.append(a.say("big_point", empty=region_name_spoken(g["empty"]),
                                     point=coord_text(g["point"]),
                                     yours_at=suffix(region_name_spoken(g["yours"]), "de")))
                reason.append(a.term("big_point"))
        if r.get("katago_line"):
            reason.append(a.say("continuation", line=_line_text(r["katago_line"])))
        if r.get("opponent_reply") not in (None, "pass"):
            reason.append(a.say("continue_opponent",
                                 answer_to=suffix(coord_text(r["opponent_reply"]), "e")))
        if reason:
            rr.append(utterance(" ".join(x for x in reason if x),
                             {"kind": "continuation", "move_no": r["move_no"] - 1,
                              "rings": rings(r), "continuation": (r.get("katago_line") or [])[:4]}))
        section(f"{r['move_no']}. {r['move']}",
              f"-{r['points_lost']:.1f}p · "
              + i18n.t(f"en iyi {r.get('best_move', '?')}",
                      f"best {r.get('best_move', '?')}"), rr)

    # --- a good moment ---
    if o["good_"]:
        fk, r = o["good_"]
        h = _move(r["move_no"])
        oy = coord_text(r["move"])
        section(i18n.t("İyi hamle", "Good move"),
              f"{r['move_no']}. {r['move']} · "
              + i18n.t("tek doğru hamle", "the only right move"), [utterance(
                  a.say("good_move", move_cap=_big(suffix(h, "de")),
                          move_to=suffix(h, "e"), played_move=oy,
                          played_obj=suffix(oy, "i"), diff=points_text(fk)),
                  {"kind": "board", "move_no": r["move_no"],
                   "rings": [[r["move"], "green",
                                 i18n.t("tam isabet", "spot on")]]})])

    # --- habits and stages ---
    rr = []
    if o["edge_line"]:
        b1, m1 = o["edge_line"]
        rr.append(utterance(a.say("edge_line", me=_percent(b1), engine_=_percent(m1)) + " " + a.term("edge_line"),
                         {"kind": "edge_line"}))
    if len(o["stages"]) == 3:
        good_ = min(o["stages"], key=lambda t: t[1])
        bad = max(o["stages"], key=lambda t: t[1])
        text_ = a.say("stage", good_=good_[0], good_mean=points_text(good_[1]),
                        bad=bad[0], bad_mean=points_text(bad[1]))
        if o["turnaround"] >= 2:
            text_ += " " + a.say("turnaround", n=number_text(o["turnaround"]))
        rr.append(utterance(text_, {"kind": "score", "mark": None}))
    if previous and len(previous) >= 3 and o["n"]:
        end = previous[-5:]
        old = sum(x["mean_loss"] for x in end) / len(end)
        now = o["lost"] / o["n"]
        if abs(now - old) >= 0.3:
            rr.append(utterance(a.say("trend_good" if now < old else "trend_bad",
                                     k=number_text(len(end)), previous=points_text(old),
                                     now=points_text(now)),
                             {"kind": "score", "mark": None}))
    if rr:
        section(i18n.t("Alışkanlıklar", "Habits"), "", rr)

    # --- ranked ---
    sp = result_.get("sp") or {}
    if sp.get("kind") == "sp":
        rank = sp["current"][0]
        change = sp["sp_change"]
        section(i18n.t("Dereceli", "Ranked"), f"{change:+.1f} SP · {rank}", [utterance(
            a.say("sp", change=(i18n.t("artı ", "plus ") if change >= 0
                                   else i18n.t("eksi ", "minus ")) + points_text(change),
                    rank=rank_text(rank)) + " " + a.term("sp") + " " + a.term("kyu"),
            {"kind": "card", "big": f"{change:+.1f} SP", "rows": [f"rank: {rank}"]})])
    elif sp.get("kind") == "placement":
        remaining = i18n.t(f"{sp['remaining']} maç kaldı", f"{sp['remaining']} games to go")
        section(i18n.t("Yerleştirme", "Placement"), remaining, [utterance(
            a.say("placement", remaining=number_text(sp["remaining"])),
            {"kind": "card", "big": i18n.t("Yerleştirme", "Placement"),
             "rows": [remaining]})])
    elif sp.get("kind") == "placement_done":
        section(i18n.t("Yerleştirme", "Placement"), sp.get("label", ""), [utterance(
            a.say("placement_done", rank=rank_text(sp["label"])) + " " + a.term("kyu"),
            {"kind": "card", "big": sp["label"],
             "rows": [i18n.t("başlangıç rankın", "your starting rank")]})])

    # --- closing ---
    strong = []
    if len(o["stages"]) == 3:
        good_ = min(o["stages"], key=lambda t: t[1])
        strong.append(i18n.t(f"{good_[0]} bu maçın en temiz evresiydi.",
                           f"{good_[0]} was the cleanest phase of this game."))
    if o["clean"]:
        start, end, n = o["clean"]
        strong.append(a.say("clean_run", start=_move(start), end=_move(end), n=number_text(n)))
    rr = []
    if strong:
        rr.append(utterance(a.say("strong", text_=_big(" ".join(strong))),
                         {"kind": "card", "big": i18n.t("Güçlü yanın", "Your strength"),
                          "rows": []}))
    if o["single_job"]:
        key_, cost = o["single_job"]
        rr.append(utterance(a.say("single_job", advice=_suggestion()[key_], lost=points_text(cost)),
                         {"kind": "card", "big": i18n.t("Tek iş", "One job"),
                          "rows": [_big(_suggestion()[key_])]}))
    if rr:
        section(i18n.t("Kapanış", "Closing"), "", rr)
    # "lang": the language it was made in. video.py picks the voice FROM THIS --
    # the script stays on disk, and the UI language may change later.
    return {"game": name, "size": size, "version": 2, "lang": i18n.selected(),
            "sections": sections}


def demo():
    """Language helpers + script skeleton. No engine."""
    assert number_text(0) == "sıfır" and number_text(11) == "on bir"
    assert number_text(174) == "yüz yetmiş dört" and number_text(1905) == "bin dokuz yüz beş"
    assert number_text(2000) == "iki bin" and number_text(100) == "yüz"
    assert points_text(11.4) == "on bir buçuk" and points_text(11.2) == "on bir"
    assert points_text(0.4) == "yarım" and points_text(-39.2) == "otuz dokuz"
    assert ordinal_text(1) == "birinci" and ordinal_text(2) == "ikinci" and ordinal_text(3) == "üçüncü"
    assert ordinal_text(4) == "dördüncü" and ordinal_text(6) == "altıncı" and ordinal_text(10) == "onuncu"
    assert ordinal_text(51) == "elli birinci" and ordinal_text(90) == "doksanıncı"
    assert suffix(coord_text("D11"), "i") == "D on biri"
    assert suffix(coord_text("C3"), "i") == "C üçü"
    assert suffix(coord_text("E4"), "e") == "E dörde" and suffix(coord_text("E4"), "i") == "E dördü"
    assert suffix(coord_text("A6"), "e") == "A altıya" and suffix(coord_text("J2"), "i") == "J ikiyi"
    assert suffix("sağ üst köşe", "de") == "sağ üst köşede" and suffix("üç", "de") == "üçte"
    assert suffix("kırk", "den") == "kırktan" and suffix("yedi", "in") == "yedinin"
    assert suffix(rank_text("20k"), "e") == "yirmi kyuya" and rank_text("1d") == "bir dan"
    assert _without_digits("3 taş") == "üç taş"
    def rec(no, move, best_, lost, **x):
        return dict({"move_no": no, "move": move, "best_move": best_,
                     "points_lost": lost, "points": 5.0, "prev_score_lead": 0.0,
                     "next_score_lead": -lost, "katago_line": [best_, "E5", "C3"]}, **x)
    k = [rec(1, "E5", "E5", 0.0, alternatives=[{"move": "E5", "diff": 0}, {"move": "C3", "diff": 4.0}]),
         rec(3, "A1", "D5", 8.0), rec(5, "B1", "D5", 6.0), rec(7, "C1", "D5", 4.0),
         rec(9, "A2", "G7", 12.0), rec(11, "F4", "F4", 0.2)]
    mv = [["B" if i % 2 == 0 else "W", "pass"] for i in range(11)]
    you = script("demo", 9, mv, k, {"won": 0.0, "score": -12.5, "opponent": "15k"})
    texts = [r["text_"] for b in you["sections"] for r in b["utterances"]]
    assert not any(re.search(r"\d", x) for x in texts), "no digits may reach the voice"
    everything = " ".join(texts)
    assert "D beş" in everything and "üç kez" in everything, everything
    assert "on iki buçuk puan" in everything, everything
    assert everything.count("Nefes, bir taş") <= 1, "a term must be defined once"
    assert "tek doğru" in everything or "birinci tercihi" in everything or "tam isabet" in everything, everything
    assert you == script("demo", 9, mv, k, {"won": 0.0, "score": -12.5, "opponent": "15k"}), \
        "the same game must give the same text"
    kinds = {r["scene"]["kind"] for b in you["sections"] for r in b["utterances"]}
    assert {"score", "run", "continuation", "card"} <= kinds, kinds

    # --- English layer: same data, en tables ---
    # Digits are NOT checked: in English digits go through on purpose (en-US
    # edge-tts reads them right). Turkish letters leaking in is an error, though.
    assert i18n.selected() == "tr", "the demo must start in Turkish"
    i18n.set_lang("en")
    try:
        assert number_text(0) == "0" and number_text(1905) == "1905"
        assert points_text(11.4) == "11.5" and points_text(11.2) == "11"
        assert points_text(0.4) == "0.5" and points_text(-39.2) == "39"
        assert ordinal_text(1) == "1st" and ordinal_text(2) == "2nd" and ordinal_text(3) == "3rd"
        assert ordinal_text(4) == "4th" and ordinal_text(11) == "11th" and ordinal_text(22) == "22nd"
        assert ordinal_text(13) == "13th" and ordinal_text(101) == "101st"
        assert suffix("move 3", "de") == "move 3", "no case suffixes in English"
        assert coord_text("D11") == "D 11" and coord_text("pass") == "pass"
        assert rank_text("20k") == "20 kyu" and rank_text("1d") == "1 dan"
        you_en = script("demo", 9, mv, k,
                         {"won": 0.0, "score": -12.5, "opponent": "15k"})
        m_en = " ".join(r["text_"] for b in you_en["sections"] for r in b["utterances"])
        assert you_en["lang"] == "en", you_en.get("lang")
        assert "D 5" in m_en and "3 times" in m_en, m_en
        assert "12.5 points" in m_en, m_en
        assert m_en.count("Liberties are the empty") <= 1, "a term must be defined once"
        leaked = [c for c in "çğışĞİŞÇ" if c in m_en]
        assert not leaked, f"Turkish leaked into the English text ({leaked}): {m_en}"
        titles = " ".join(b["title"] for b in you_en["sections"])
        assert "Game summary" in titles, titles
    finally:
        i18n.set_lang("tr")          # the demo does not leave the language switched
    print("narration ok (tr + en)")


if __name__ == "__main__":
    demo()
