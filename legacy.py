#!/usr/bin/env python3
"""One-time import of data saved under the project's former name, go-hoca.

go-hoca kept games in a `maclar/` folder, with Turkish file names and Turkish
JSON keys. GoMentor keeps them in `games/`, in English. On first start, when
the new folder does not exist yet and an old one does, everything is copied
and converted. The old folder is never modified, so nothing can be lost.
"""
import json, os, re, shutil

import bundle

LEGACY_KEYS = {'ad': 'name', 'agir': 'heavy', 'ahmet': 'speaker_b', 'ahsap': 'wood',
    'alt': 'bottom', 'alternatifler': 'alternatives', 'altyazi': 'subtitle',
    'analiz': 'analysis', 'aralik': 'spacing', 'arasi': 'between', 'araya': 'interrupted',
    'arka': 'bg', 'arsiv': 'archive', 'arsiv_serit': 'archive_strip', 'ata': 'parent',
    'ayarlar': 'settings', 'ayrinti': 'more', 'bas': 'start', 'baska': 'other',
    'baslik': 'title', 'bekliyor': 'waiting', 'belirsizlik': 'uncertainty', 'ben': 'me',
    'beyaz': 'white', 'bilgi': 'info', 'birincil': 'primary', 'blunder_oran': 'blunder_ratio',
    'bolge': 'region', 'bolge_de': 'region_at',
    'bolgedeki_degisim_puan': 'region_change_points', 'bolum': 'section',
    'bolumler': 'sections', 'bos': 'empty', 'boyut': 'dimension', 'bozuk': 'broken',
    'bulgular': 'findings', 'buyuk': 'big', 'buyuk_nokta': 'big_point', 'canli': 'live',
    'canli_serit': 'live_strip', 'cevap_e': 'answer_to', 'cizgi': 'line', 'cozuldu': 'solved',
    'deger': 'value', 'degisim': 'change', 'ders': 'lesson', 'devam': 'continuation',
    'devam_rakip': 'continue_opponent', 'dil': 'lang', 'doku': 'grain', 'doldu': 'expired',
    'dolgu': 'fill', 'donus': 'turnaround', 'dosya': 'file', 'dugme': 'button',
    'dugme_ust': 'button_hover', 'durum': 'state', 'dusus': 'drop', 'emel': 'speaker_a',
    'en_cok_degisen_bolge': 'most_changed_region', 'en_iyi_hamle': 'best_move',
    'esir': 'capture', 'eski': 'old', 'etiket': 'label', 'evre': 'stage', 'evreler': 'stages',
    'fark': 'diff', 'geri_alindi': 'retracted', 'giris': 'intro',
    'giris_berabere': 'intro_draw', 'giris_kayip': 'intro_loss',
    'giris_kayip_ozet': 'intro_loss_summary', 'giris_kazanc': 'intro_win', 'golge': 'shadow',
    'guclu': 'strong', 'halkalar': 'rings', 'ham_deger': 'raw_value',
    'ham_etiket': 'raw_label', 'hamle': 'move', 'hamle_bolge': 'move_region',
    'hamle_buyuk': 'move_cap', 'hamle_buyuk_de': 'move_cap_at', 'hamle_de': 'move_at',
    'hamle_e': 'move_to', 'hamle_ne_oldu': 'what_happened', 'hamle_no': 'move_no',
    'hamle_sayisi': 'move_count', 'hamle_skor': 'move_score', 'hamleler': 'move_nos',
    'hat': 'edge_line', 'hatalar': 'mistakes', 'iki': 'duo', 'iki_nefes': 'two_liberties',
    'iki_pas': 'two_passes', 'ikili': 'binary', 'ikili+kalite': 'binary+quality',
    'ikili-arama': 'binary-search', 'is': 'job', 'isaret': 'mark', 'iyi': 'good_',
    'iyi_hamle': 'good_move', 'iyi_oran': 'good_ratio', 'iyi_ort': 'good_mean',
    'kagit': 'paper', 'kalan': 'remaining', 'kalite': 'quality', 'kalite_mac': 'quality_games',
    'kanca_dusus': 'hook_drop', 'karanlik': 'dark', 'karsilama': 'welcome', 'kart': 'card',
    'katago-log': 'katago-logs', 'katago-veri': 'katago-data', 'katago_devami': 'katago_line',
    'kayip': 'lost', 'kayip_puan': 'points_lost', 'kayitlar': 'records', 'kaynak': 'source',
    'kazandi': 'won', 'kenar': 'edge', 'kendi': 'own', 'kendi_bolge': 'own_area',
    'kilit': 'keystone', 'kilit_nokta': 'key_spot', 'kilitli': 'locked', 'kirmizi': 'red',
    'klasik': 'classic', 'kok': 'root', 'kontrast': 'contrast', 'konusan': 'speaker',
    'kotu': 'bad', 'kotu_ort': 'bad_mean', 'kutu': 'box', 'mac': 'game',
    'mac_sonu': 'game_end', 'maclar': 'games', 'merkez': 'center', 'metin': 'text_',
    'metinler': 'texts', 'motor': 'engine_', 'nefes': 'liberty', 'nokta': 'point',
    'nokta_e': 'point_to', 'nokta_i_buyuk': 'point_obj_cap', 'notr': 'neutral',
    'olcum': 'measurement', 'olu': 'dead', 'once': 'earlier', 'once-sonra': 'before-after',
    'onceki': 'previous', 'onceki_skor_farki': 'prev_score_lead', 'oneri': 'advice',
    'oran': 'ratio', 'ort': 'mean', 'ort_kayip': 'mean_loss', 'ort_puan': 'mean_points',
    'oyna': 'play', 'oynanan': 'played_move', 'oynanan_e': 'played_to',
    'oynanan_i': 'played_obj', 'oynayan': 'mover', 'pahali': 'expensive', 'pas': 'pass_',
    'pay': 'margin', 'pes': 'resign', 'problemler': 'problems', 'provizyonel': 'provisional',
    'puan': 'points', 'puan_kaybi': 'point_loss', 'rakip': 'opponent',
    'rakip_cevabi': 'opponent_reply', 'rakip_cumle': 'opponent_sentence',
    'rank_atladi': 'rank_up', 'rapor': 'report', 'renk': 'color', 'renkler': 'colors',
    'replikler': 'utterances', 'sag': 'right', 'sag alt': 'bottom_right',
    'sag ust': 'top_right', 'sahiplik': 'ownership', 'sahne': 'scene', 'sari': 'yellow',
    'satirlar': 'rows', 'sayi': 'num', 'sayim': 'count', 'sayım': 'counted', 'secim': 'choice',
    'sen': 'you', 'senin': 'yours', 'senin_de': 'yours_at',
    'senin_seviyen_ne_oynar': 'your_level_plays',
    'senin_seviyenin_beklenen_kaybi': 'your_level_expected_loss', 'seri': 'run',
    'seri_araya': 'run_interrupted', 'seri_bedel': 'run_cost', 'seri_giris': 'run_intro',
    'seriler': 'runs', 'ses': 'sound', 'ses_duzey': 'volume', 'seviye_egrisi': 'level_curve',
    'seviye_puan': 'level_points', 'seviye_tahmini': 'level_estimate', 'simdi': 'now',
    'simdiki': 'current', 'sinirda': 'at_limit', 'sirada': 'next_up', 'siyah': 'black',
    'skor': 'score', 'sol': 'left', 'sol alt': 'bottom_left', 'sol ust': 'top_left',
    'soluk': 'faint', 'son': 'end', 'sonra': 'later', 'sonraki': 'next',
    'sonraki_rakip': 'next_opponent', 'sonraki_skor_farki': 'next_score_lead',
    'sonuc': 'result', 'sp_degisimi': 'sp_change', 'sure': 'clock', 'sure_acik': 'clock_on',
    'sure_dk': 'clock_min', 'surum': 'version', 'tahmin': 'estimate', 'tahta': 'board',
    'tas': 'stone', 'tas_kenar': 'stone_edge', 'tek-arama': 'single-search',
    'tek_is': 'single_job', 'tek_nefes': 'one_liberty', 'tekrar': 'review', 'tema': 'theme',
    'tema_dugmeleri': 'theme_buttons', 'temel': 'basics', 'temiz': 'clean',
    'temiz_seri': 'clean_run', 'tip': 'kind', 'toplam': 'total', 'trend_iyi': 'trend_good',
    'trend_kotu': 'trend_bad', 'ust': 'top', 'ust_seviye_ne_oynar': 'next_level_plays',
    'uyari': 'warning', 'var': 'ok', 'veri': 'data', 'vurgu': 'accent',
    'vurgu_ust': 'accent_hover', 'vurgu_yazi': 'accent_text', 'yaklasik': 'approximate',
    'yaklaşık sayım': 'approximate_count', 'yan': 'beside', 'yan_yazi': 'side_text',
    'yazi': 'ink', 'yeni': 'new', 'yerlesme': 'placement', 'yerlesme_bitti': 'placement_done',
    'yesil': 'green', 'yok': 'missing', 'yontem': 'method'}

FOLDERS = {"rapor": "report", "tema": "theme", "ses": "sound"}
SUFFIXES = {"_senaryo.json": "_script.json", "_analiz.json": "_analysis.json",
            "_analiz.mp4": "_analysis.mp4", "_orgu.md": "_patterns.md",
            "_hata.log": "_error.log", "_dal.json": "_branches.json",
            "profil.json": "profile.json", "tekrar.json": "reviews.json"}
# Review cards point at bundled problems by path.
TSUMEGO = {"temel/01-duz-uclu-oldur": "basics/01-straight-three-kill",
           "temel/02-duz-uclu-yasat": "basics/02-straight-three-live",
           "temel/03-kosede-egri-uclu-oldur": "basics/03-corner-bent-three-kill",
           "temel/04-kosede-egri-uclu-yasat": "basics/04-corner-bent-three-live",
           "3 · zor şekiller/": "3 · hard shapes/"}
_GAME = re.compile(r"\b(mac|ders|iki)_(?=\d{8}-\d{4})")
_PREFIX = {"mac": "game", "ders": "lesson", "iki": "duo"}


def _text(s):
    if s in LEGACY_KEYS:
        return LEGACY_KEYS[s]
    s = _GAME.sub(lambda m: _PREFIX[m[1]] + "_", s)
    for a, b in TSUMEGO.items():
        s = s.replace(a, b)
    return s


def convert(x):
    """JSON value with go-hoca keys -> the same value with GoMentor keys."""
    if isinstance(x, dict):
        return {_text(k): convert(v) for k, v in x.items()}
    if isinstance(x, list):
        return [convert(v) for v in x]
    return _text(x) if isinstance(x, str) else x


def file_name(f):
    for a, b in SUFFIXES.items():
        if f.endswith(a):
            f = f[:-len(a)] + b
            break
    return _GAME.sub(lambda m: _PREFIX[m[1]] + "_", f)


def _copy(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if dst.endswith(".jsonl"):
        with open(src, encoding="utf-8") as f, open(dst, "w", encoding="utf-8") as g:
            for line in f:
                if line.strip():
                    g.write(json.dumps(convert(json.loads(line)), ensure_ascii=False) + "\n")
    elif dst.endswith(".json"):
        with open(src, encoding="utf-8") as f:
            data = convert(json.load(f))
        with open(dst, "w", encoding="utf-8") as g:
            json.dump(data, g, ensure_ascii=False, indent=1)
    else:
        shutil.copy2(src, dst)


def import_games(old, new):
    """Copies and converts the `maclar/` tree. Returns the number of files."""
    n = 0
    for here, dirs, files in os.walk(old):
        dirs[:] = [d for d in dirs if not d.endswith("_kare")]   # leftover video frames
        rel = os.path.relpath(here, old)
        parts = [] if rel == "." else [FOLDERS.get(p, p) for p in rel.split(os.sep)]
        for f in files:
            dst = os.path.join(new, *parts, file_name(f))
            try:
                _copy(os.path.join(here, f), dst)
                n += 1
            except (OSError, ValueError):
                shutil.copy2(os.path.join(here, f), dst)   # unreadable JSON: keep it as is
    return n


def old_roots():
    """Where go-hoca may have written: beside the app (portable / source) or
    in the per-user folder."""
    return [bundle.ROOT, os.path.join(os.path.dirname(bundle.user_data()), "go-hoca")]


def _has_data(games_root):
    # Not just "exists": the packaged app creates games/log/ before anything else.
    try:
        return any(f.endswith(".sgf") or f == "profile.json" for f in os.listdir(games_root))
    except OSError:
        return False


def migrate(games_root):
    """Imports go-hoca data once. Does nothing when `games_root` already has data."""
    if _has_data(games_root) or os.environ.get("GOMENTOR_DATA"):
        return 0
    n = 0
    for root in old_roots():
        old = os.path.join(root, "maclar")
        if os.path.isdir(old):
            n = import_games(old, games_root)
            break
    engine_data = os.path.join(bundle.engine_data_root(), "katago-data")
    for root in old_roots():
        old = os.path.join(root, "katago-veri")
        if os.path.isdir(old) and not os.path.exists(engine_data):
            shutil.copytree(old, engine_data)   # saves KataGo a 3-minute re-tune
            break
    return n


def demo():
    import tempfile
    old, new = tempfile.mkdtemp(), os.path.join(tempfile.mkdtemp(), "games")
    with open(os.path.join(old, "ders_20260908-185347.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps({"hamle_no": 3, "kayip_puan": 1.5, "bulgular": [
            {"tip": "buyuk_nokta", "bos": "sol ust", "nokta": "D4"}]}) + "\n")
    with open(os.path.join(old, "ders_20260908-185347.sgf"), "w", encoding="utf-8") as f:
        f.write("(;GM[1]SZ[9])")
    with open(os.path.join(old, "tekrar.json"), "w", encoding="utf-8") as f:
        json.dump({"tsumego#temel/01-duz-uclu-oldur": {"kutu": 1, "sonraki": "2026-10-01"}}, f)
    os.makedirs(os.path.join(old, "rapor"))
    open(os.path.join(old, "rapor", "_orgu.md"), "w").close()
    assert import_games(old, new) == 4
    with open(os.path.join(new, "lesson_20260908-185347.jsonl"), encoding="utf-8") as f:
        r = json.loads(f.readline())
    assert r == {"move_no": 3, "points_lost": 1.5, "findings": [
        {"kind": "big_point", "empty": "top_left", "point": "D4"}]}, r
    with open(os.path.join(new, "reviews.json"), encoding="utf-8") as f:
        assert json.load(f) == {"tsumego#basics/01-straight-three-kill": {"box": 1, "next": "2026-10-01"}}
    assert os.path.exists(os.path.join(new, "report", "_patterns.md"))
    assert os.path.exists(os.path.join(new, "lesson_20260908-185347.sgf"))
    return "legacy ok"


if __name__ == "__main__":
    print(demo())
