#!/usr/bin/env python3
"""GoMentor - command line.

Layers:
  goban.py    rules + coordinates + SGF    (pure, no dependencies)
  engine.py   KataGo client + config       (what changes for a web version)
  scoring.py  grading + level estimate     (the core, pure functions)
  store.py    game records                 (files today, a DB tomorrow)
  ui.py       PySide6 UI
  coach.py    this file: CLI and tests

Usage:
  python coach.py --selftest                       fast, no engine
  python coach.py --enginetest                     runs the engine, slow
  python coach.py --import-sgf game.sgf --color W  import an outside SGF and grade it
  python coach.py --brief ranked_20260907-2312      make the analysis brief (for an LLM)
  python coach.py                                  play in the terminal
"""
import argparse, datetime, json, math, os, shutil, sys, time

import goban, legacy, store
from goban import Board, from_gtp, to_gtp, to_sgf, replay, sgf_dump, sgf_load
from engine import Config, Engine
import scoring
from scoring import grade, detection, analyze_position, selfplay, teaching_position, \
    level_estimate, expected_loss, rank_value, value_rank, game_level


def report(f):
    sv = f.get("level_estimate")
    print(f"\n  {f['move']} -> {scoring.category(f)} ({f['points_lost']}p lost)"
          + (f"   ~{sv['label']} level" if sv else ""))
    if f["points_lost"] >= 0.7:
        print(f"  best     : {f['best_move']}    line: {' '.join(f['katago_line'])}")
        for b in f.get("findings", []):
            print("  * " + scoring.finding_text(b))
        print(f"  your lvl : {f['your_level_plays']}")
        print(f"  ust sv   : {f['next_level_plays']}")
    print(f"  score lead: {f['prev_score_lead']:+.1f} -> {f['next_score_lead']:+.1f}")


def play_cli(my_rank, bot_rank, next_rank):
    eng = Engine()
    size = eng.cfg.size
    m = store.Game(name=store.new_name("game"), size=size, komi=eng.cfg.komi)
    b = Board(size)
    print(f"{size}x{size}, komi {eng.cfg.komi}. You are Black, bot {bot_rank}.")
    if size not in scoring.KALIBRE_BOY:            # console: ASCII, so cp1254 does not choke
        print(f"WARNING: {size}x{size} is not calibrated -- rank/level estimate is approximate, "
              f"point loss (p) is exact.")
    print("Move: 'E5' | 'pass' | 'q' to quit\n")
    while True:
        print(b.ascii())
        try:
            raw = input("\nhamlen> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if raw.lower() in ("q", "quit", "exit"):
            break
        try:
            idx = from_gtp(raw, size)
        except ValueError:
            print("  invalid coordinate (example: E5)")
            continue
        if not b.play(idx, 1):
            print("  illegal move")
            continue
        m.moves.append(["B", "pass" if idx is None else to_gtp(idx, size)])
        print("  ...analyzing")
        f = grade(eng, list(m.moves), "B", my_rank, next_rank)
        store.add_record(m, {"move_no": len(m.moves), **f})
        store.save(m)
        report(f)

        for mv in eng.human_sample(list(m.moves), f"rank_{bot_rank}"):
            if mv != "pass" and b.play(from_gtp(mv, size), -1):
                m.moves.append(["W", mv])
                break
        else:
            b.play(None, -1)
            m.moves.append(["W", "pass"])
        store.save(m)
        print(f"\nbot: {m.moves[-1][1]}\n" + "-" * 46)
        if goban.game_over(m.moves):
            print("Game over (two passes).")
            break
    print(f"\nsaved: games/{m.name}.sgf")


def import_cli(path, color, my_rank="15k", next_rank="9k"):
    """Imports an outside SGF into the archive and grades the user's moves."""
    import time, sgf_import
    with open(path, encoding="utf-8", errors="replace") as f:
        m = sgf_import.create(f.read(), color)
    eng = Engine()
    n, t0 = len(sgf_import.missing(m)), time.time()
    print(f"{m.name}: {m.size}x{m.size}, {len(m.moves)} rows "
          f"({goban.setup_count(m.moves)} setup stones), grading {n} {color} moves")
    sgf_import.grade_moves(eng, m, my_rank, next_rank,
                     lambda i, n: print(f"  {i + 1}/{n}", end=chr(13), flush=True))
    k = m.valid_records
    print(f"{chr(10)}{time.time() - t0:.0f} s · {len(k)} moves · total loss "
          f"{sum(r['points_lost'] for r in k):.1f}p · result {m.result or '-'}")
    eng.close()


def _uitest_cleanup():
    """Test games that the clock writes permanently. Also called at the start: the
    leftovers of a run that crashed halfway became the "newest game" on the
    next run and broke the archive test (measured: the "fast forward" failure)."""
    for name in ("game_uitest_clock", "duo_uitest_clock"):
        for ext in (".sgf", ".jsonl"):
            y = store._path(name, ext)
            if os.path.exists(y):
                os.remove(y)


def uitest():
    """Builds the UI without showing a window and runs the archive flows.

    --selftest does NOT IMPORT the UI. This project once stayed broken for
    exactly that reason: the tests were green while the app did not even open
    because of a syntax error.
    Runs without an engine: GOMENTOR_KATAGO_DIR is set to an invalid path and
    KataGo is not expected.
    """
    os.environ.setdefault("GOMENTOR_KATAGO_DIR", os.path.join(store.ROOT, "_no_engine"))
    _uitest_cleanup()
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    import ui
    app = QApplication.instance() or QApplication([])
    w = ui.App("15k", "15k", "9k", 3)
    # The language is PINNED. The text assertions below are in Turkish, but with
    # no saved preference the language comes from the operating system (ui:
    # `store.setting("lang") or i18n.system_lang()`). The CI runner is English --
    # the test passed on the development machine and failed in CI. The English
    # paths run separately in the 'language picker' block.
    import i18n as _lang
    w.lang_box.setCurrentIndex(list(_lang.LANGUAGES).index("tr"))
    w.mode_select("archive")
    assert w.mode == "archive", w.mode
    assert w.game_list.count() == len(w.saved), "game list did not fill"

    # --- board size: picker -> cfg -> board, and the lock during a ranked game ---
    w.mode_select("game")
    assert not w.setup_box.isHidden() and w.stack.currentIndex() == w.page["play"]
    w.size_box.setCurrentText("13x13")
    assert w.cfg.size == 13, w.cfg.size
    w.next_task()
    assert w.canvas.size == 13, "the board did not pick up the size change"
    w.mode_select("ranked")
    w.game.mode = "ranked"                      # motorsuz: next_task kosmadi, oyunu elle kur
    w.game.moves, w.phase = [["B", "D4"]], "your_turn"
    w.size_box.setCurrentText("19x19")
    assert w.cfg.size == 13, "size changed during an unfinished ranked game"
    assert w.size_box.currentText() == "13x13", w.size_box.currentText()
    w.kind_select("game")                          # unfinished ranked game: cannot switch to free
    assert w.b_ranked.isChecked() and w.mode == "ranked", "unfinished ranked game was abandoned"
    w.mode_select("settings")
    w.mode_select("ranked")                       # going to settings and back does not kill the game
    assert w.game.moves == [["B", "D4"]] and w.phase == "your_turn", "the game did not carry on"
    w.phase = "done"                          # free once the game is over
    w.size_box.setCurrentText("9x9")
    assert w.cfg.size == 9, w.cfg.size
    w.mode_select("archive")

    # --- live game: looking at a past move (watching), turn info, strip ---
    w.mode_select("game")
    w.game = store.Game(name="game_uitest_watch", moves=[["B", "E5"], ["W", "C3"], ["B", "G7"]])
    w.board, w.phase = w.game.board_at(), "your_turn"
    w.refresh()
    assert "○ Beyaz — bot" in w.turn.text() and "3 hamle" in w.turn.text(), w.turn.text()
    assert len(w.live_strip.nodes) == 4 and all(d[4] is None for d in w.live_strip.nodes)
    w.live_select(None, 1)
    assert w.watch == 1 and not w.b_live.isHidden() and w.canvas.plain
    assert w.canvas.board.g[goban.from_gtp("C3", 9)] == 0, "the watching board is not in the past"
    w.click(goban.from_gtp("D4", 9))
    assert len(w.game.moves) == 3 and not w.b_pass.isEnabled(), "a move was played while watching"
    w.navigate(1)
    assert w.watch == 2
    w.navigate(1)
    assert w.watch is None and w.b_live.isHidden() and w.b_pass.isEnabled()
    w.navigate(-10 ** 6)
    assert w.watch == 0
    w.back_to_live()
    assert w.watch is None and not w.canvas.plain

    # --- free game: quiet on a normal move, a card on a blunder ---
    f = {"move": "G7", "mover": "B", "points_lost": 0.4, "best_move": "G7",
         "alternatives": [], "findings": [], "your_level_plays": [],
         "next_level_plays": [], "prev_score_lead": 1.0, "next_score_lead": 0.6,
         "region_change_points": 0.0, "most_changed_region": "center",
         "katago_line": [], "level_estimate": None,
         "your_level_expected_loss": None, "opponent_reply": "C3"}
    import tempfile
    real_root = store.ROOT
    played = []
    w._bot_move = lambda: played.append(1)
    try:
        store.ROOT = tempfile.mkdtemp()
        w.say("intro")
        w._graded(dict(f))
        assert played and "intro" in w.out.toPlainText(), "did not stay quiet on a normal move"
        w._graded(dict(f, move="A1", points_lost=9.5, best_move="E5",
                       alternatives=[{"move": "E5", "diff": 0.0, "continuation": ["E5", "D4"]}]))
        text_ = w.out.toPlainText()
        assert len(played) == 1 and "Blunder" in text_ and "E5" in text_, text_
        assert w.b_retry.isEnabled() and w.b_go.isEnabled(), "buttons disabled on the blunder card"
        assert ("E5", ui.C_BEST, "x", "") in w.canvas.overlays
        w._card_link(ui.QUrl("more"))
        assert "Skor: +1.0" in w.out.toPlainText(), "details did not open"
    finally:
        store.ROOT = real_root
        del w._bot_move

    # --- screens: analysis (board by subtab), settings (theme preview) ---
    w.mode_select("analysis")
    for k in (1, 2, 0):
        w.analysis_tabs.setCurrentIndex(k)
        assert w.left.isHidden() == (k > 0), f"analysis {k}: board visibility"
    w.mode_select("settings")
    assert w.canvas.board.g[goban.from_gtp("E5", 9)] == 1, "theme preview was not drawn"
    w.click(goban.from_gtp("A1", 9))          # the board does not play on the settings screen

    # --- NO separate windows: features on a page with "← Back", questions in the panel ---
    import store as st
    real_profile = st.PROFILE
    try:
        st.PROFILE = os.path.join(tempfile.mkdtemp(), "profile.json")
        w._show_page("attempt", "# baslik")
        assert w.content.currentIndex() == 1 and w._open_page is not None
        w.close_page()
        assert w.content.currentIndex() == 0 and w._open_page is None
        w.welcome()
        assert st.setting("welcome") and w.content.currentIndex() == 1
        page = w._open_page
        page.buttons_.itemAt(1).widget().click()          # "I know Go"
        assert w.content.currentIndex() == 0 and w.mode == "ranked", w.mode
        w.rules_tour(later=lambda: w.mode_select("game"))
        kind_ = w._open_page
        for _ in range(len(ui.RulesTour.PAGES)):
            kind_.b_forward.click()
        assert w.content.currentIndex() == 0 and w.mode == "game", "the page did not close after the tour"
        w.game = store.Game(name="game_uitest_resign", moves=[["B", "E5"]])
        w.phase = "your_turn"
        w.resign_game()
        assert not w.play_choice.isHidden() and w.phase == "your_turn", "resign confirmation was not asked in the panel"
        w.play_choice.row_.itemAt(1).widget().click()      # Cancel
        assert w.play_choice.isHidden() and w.phase == "your_turn"
        w.mode_select("settings")
        w._sound_on = True
        w.sound_box.setChecked(False)
        assert "Kapalı" in w.sound_box.text() and st.setting("sound") is False
        w.sound_box.setChecked(True)
        assert "Açık" in w.sound_box.text() and st.setting("sound") is True
    finally:
        st.PROFILE = real_profile
    import time
    if w._sfx:
        # The WAV was found (otherwise _sfx would be None) and the source resolved --
        # that part is checked everywhere. BUT isLoaded() needs a real audio OUTPUT;
        # the CI runner has no device and QSoundEffect never loads. Hardware is not
        # this test's concern, the asset path is.
        from PySide6.QtMultimedia import QMediaDevices
        assert w._sfx.source().isValid(), "stone sound source was not set"
        if QMediaDevices.audioOutputs():
            for _ in range(50):
                app.processEvents()
                if w._sfx.isLoaded():
                    break
                time.sleep(0.02)
            assert w._sfx.isLoaded(), "stone sound did not load"
    is_open = [t for t in app.topLevelWidgets() if t is not w and t.isVisible()]
    assert not is_open, f"a separate window opened: {is_open}"
    w.mode_select("archive")
    w.mode_select("archive")

    # --- end of a live game: two passes -> count -> three-valued result -> ranked SP ---
    # No engine: the count stays approximate and the estimate decides the winner.
    # run() is made synchronous; the ranked path that writes files is pointed at a
    # TEMPORARY folder.
    import tempfile
    real = (store.ROOT, store.PROFILE)
    w.run = lambda fn, cb: cb(fn())
    try:
        store.ROOT = tempfile.mkdtemp()
        store.PROFILE = os.path.join(store.ROOT, "profile.json")
        for i, (tab_, lead, label) in enumerate(((1, 2.0, "Kazandın"),
                                                   (2, 0.0, "Berabere"),
                                                   (2, -3.0, "Kaybettin"))):
            w.mode_select(("lesson", "game", "ranked", "archive")[tab_])
            w.game = store.Game(name=f"{w.mode}_uitest{i}", mode=w.mode,
                              moves=[["B", "pass"], ["W", "pass"]])
            store.save(w.game)
            w.opponent_value = scoring.WEAKEST
            w.facts = {"next_score_lead": lead}
            w._finish()
            text_ = w.out.toPlainText()
            assert label in text_ and "alan sayımı" in text_, text_
            assert w.phase == "done", w.phase
        stop = store.rank_state(9)
        assert [x[1] for x in stop["placement"]] == [0.5, 0.0], stop

        # --- two-player game: two people at one computer, one board ---
        real_eng, real_game = w.eng, w.game     # what follows expects the last ranked game
        w.eng = object()                          # engine "ready": so moves can be played
        try:
            w.mode_select("duo")
            assert w.mode == "duo" and w.game.mode == "duo", w.mode
            assert w.opponent_line.isHidden() and not w.setup_box.isHidden(), "setup row"
            assert w.phase == "your_turn" and "Karşılıklı" in w.head.text(), w.head.text()
            w.click(goban.from_gtp("E5", 9))
            assert w.game.moves == [["B", "E5"]], w.game.moves
            w.click(goban.from_gtp("C3", 9))      # the same click plays White, no bot
            assert w.game.moves[-1] == ["W", "C3"], w.game.moves
            assert w.board.g[goban.from_gtp("C3", 9)] == -1, "white stone not placed"
            assert "oyuncu 1" in w.turn.text(), w.turn.text()
            assert not (w.b_hint.isEnabled() or w.b_retry.isEnabled()), "hint/undo enabled"
            assert w.b_resign.isEnabled(), "resign disabled"
            w.do_pass()
            w.do_pass()                           # two passes -> count
            text_ = w.out.toPlainText()
            assert w.phase == "done" and "alan sayımı" in text_, text_
            # Two people are playing: "You won"/"(you)"/"(bot)" would be wrong here.
            assert "Siyah:" in text_ and "(sen)" not in text_ and "Kazandın" not in text_, text_
            w.game = store.Game(name="duo_uitest_resign", mode="duo", moves=[["B", "E5"]])
            w.phase = "your_turn"
            w.resign_game(True)                        # White to move: White is the one resigning
            assert "Siyah kazandı — pes" in w.out.toPlainText(), w.out.toPlainText()
        finally:
            w.eng, w.game = real_eng, real_game
        # language picker: the preference is saved, t() returns the new language and
        # the UI is REBORN -- labels already built get translated too.
        import i18n
        # The hardest case: a language change during a live RANKED game -- the game must not die.
        w.mode_select("ranked")
        w.game.mode, w.game.moves, w.phase = "ranked", [["B", "D4"]], "your_turn"
        w.lang_box.setCurrentIndex(list(i18n.LANGUAGES).index("en"))
        assert i18n.selected() == "en" and store.setting("lang") == "en", store.setting("lang")
        app.processEvents()                       # the singleShot(0) refresh
        assert w.screen_buttons["play"].text().endswith("Play"), w.screen_buttons["play"].text()
        assert w.mode == "ranked", w.mode           # the ranked kind was kept
        assert w.game.moves == [["B", "D4"]] and w.phase == "your_turn", "the language change killed the game"

        # English DYNAMIC text paths: widget setup passed above; this block runs the
        # archive/analysis/quiz texts in English. A missing {key} or an untranslated
        # branch blows up here, not in front of a user.
        w.phase = "done"
        w.mode_select("archive")
        if w.saved:
            w.game_list.setCurrentRow(0)
            w.game_select()
            w.show_move()
            w.navigate(1)
            w.quiz()
            w.write_brief()
            # No Turkish leaking into the English screen. Only Turkish-specific letters are
            # searched for: game names and coordinates are ASCII, there are no proper names.
            text_ = w.archive_out.toPlainText()
            leaked = [c for c in "çğışĞİŞÇ" if c in text_]
            assert not leaked, f"Turkish leaked into the English archive screen {leaked}: {text_[:300]}"
        w.mode_select("analysis")
        w.analysis_tabs.setCurrentIndex(1)          # pattern report: English markdown
        w.analysis_tabs.setCurrentIndex(2)          # progress: trend panel
        w.analysis_tabs.setCurrentIndex(0)
        w.mode_select("archive")
        w.game.mode, w.game.moves, w.phase = "ranked", [["B", "D4"]], "your_turn"
        w.mode_select("ranked")
        w.lang_box.setCurrentIndex(list(i18n.LANGUAGES).index("tr"))
        app.processEvents()
        assert i18n.selected() == "tr" and store.setting("lang") == "tr"
        assert w.screen_buttons["play"].text().endswith("Oyna"), w.screen_buttons["play"].text()
        w.phase = "done"
        # Licenses window: is the notice file really found and opened (this blows up
        # if it falls out of the package or its path changes).
        lic = w.licenses().toPlainText()
        assert w.page_title.text() in ("Lisanslar", "Licenses"), w.page_title.text()
        assert "GPL" in lic and "ffmpeg" in lic.lower(), lic[:200]
        assert "bulunamadı" not in lic and "not found" not in lic, "could not read the notice file"
        w.close_page()

        import theme
        w.theme_select("dark")
        assert theme.active() is theme.THEMES["dark"] and store.setting("theme") == "dark"
        assert w.theme_buttons["dark"].isChecked() and not w.theme_buttons["wood"].isChecked()
        assert theme.THEMES["dark"]["bg"] in app.styleSheet(), "theme stylesheet was not applied"
        w.theme_select(theme.DEFAULT)
        assert store.load_game(w.game.name).result["won"] == 0.0
    finally:
        store.ROOT, store.PROFILE = real
        del w.run
        w.mode_select("archive")

    # --- analysis video: made in the background, "generating" shows, it plays
    # inside the app when done; the result reaches its owner even if the game/tab changes ---
    import threading, time, video
    real_all, release = video.make_all, threading.Event()
    def fake(name, llm=False, progress_=None):
        progress_(0, 2)
        release.wait(5)                           # so the "generating" state can be observed
        if name == "broken":
            raise RuntimeError("no ffmpeg")
        return os.path.join(store.ROOT, "_missing.mp4")
    def wait_for(condition):
        end = time.time() + 5
        while not condition() and time.time() < end:
            w.pump()
            app.processEvents()
        assert condition(), "video generation did not finish"
    video.make_all = fake
    try:
        w.video_start("vid")
        w.gen += 1                            # game/tab changed: the result must still arrive
        wait_for(lambda: "1/2" in w.video_state.text())
        assert "üretiliyor" in w.video_state.text(), w.video_state.text()
        w.video_start("other")                # one generation at a time
        assert w._video_game == "vid" and "Önce bitsin" in w.video_state.text()
        d = w._video_view
        if d:
            assert d.stack.currentWidget() is d.state_ and "1/2" in d.state_.text()
            assert not d.b_restart.isEnabled(), "regenerate enabled while generating"
        release.set()
        wait_for(lambda: w._video_game is None)
        if d:
            assert d.stack.currentWidget() is d.screen_, "did not switch to the player when done"
            assert d.player_.source().toLocalFile().endswith("_missing.mp4")
            d.hide()                          # the file must be released (Windows lock)
            assert d.player_.source().isEmpty()
        release.clear()
        w.video_start("broken")
        release.set()
        wait_for(lambda: w._video_game is None)
        assert "üretilemedi" in w.video_state.text(), w.video_state.text()
    finally:
        video.make_all = real_all
        release.set()
        if w._video_view:
            w._video_view.hide()

    # --- SGF import: written to the archive, graded in the background, color and
    # handicap show correctly in the archive, an interrupted one resumes (no engine: fake grade) ---
    import sgf_import
    real_root, real_grade_, real_eng = store.ROOT, sgf_import.grade_moves, w.eng
    real_tsumego = os.environ.get("GOMENTOR_TSUMEGO")
    cut = []
    def fake_grade(eng, m, my_rank, next_rank, progress_=None):
        for i, n in enumerate(sgf_import.missing(m)):
            if cut and i == cut[0]:
                raise RuntimeError("engine down")
            progress_(i, 1)
            store.add_record(m, {"move_no": n, "move": m.moves[n - 1][1],
                                 "mover": m.moves[n - 1][0],
                                 "points": 9.0, "level_points": 9.0, "points_lost": 0.5,
                                 "best_move": m.moves[n - 1][1], "findings": []})
        return m
    sgf = ("(;GM[1]FF[4]SZ[9]KM[0.5]PB[rakip]PW[ben]RE[W+2.5]AB[gc][cg]"
           ";W[ee];B[dc];W[ce];B[];W[tt])")
    try:
        store.ROOT = tempfile.mkdtemp()
        os.environ["GOMENTOR_TSUMEGO"] = tempfile.mkdtemp()   # isolate the blunder cards
        sgf_import.grade_moves = fake_grade
        w.mode_select("archive")
        w.sgf_start(text_=sgf, color="W")
        wait_for(lambda: w._sgf_name is None)
        assert "içe aktarıldı, 2 hamle" in w.sgf_status.text(), w.sgf_status.text()
        assert w.archive and w.archive.mode == "sgf" and w.archive.color == "W", "the new game was not selected"
        assert w.game_list.currentItem().text().startswith("İçe aktarılan")
        assert "Kazandın · SGF: W+2.5 (sen beyaz)" in w.archive_out.toPlainText()
        w.archive_i = 1; w.show_move()
        assert "Handikap taşı" in w.archive_out.toPlainText()
        assert w.canvas.board.g[goban.from_gtp("G7", 9)] == 1, "handicap stone not on the board"
        w.archive_i = 4; w.show_move()                    # B D7: the opponent, graded too
        assert "Mükemmel · 0.5p kayıp" in w.archive_out.toPlainText(), w.archive_out.toPlainText()
        w.archive_i = 3; w.show_move()                    # W E5: mine, graded
        assert "0.5p kayıp" in w.archive_out.toPlainText(), w.archive_out.toPlainText()
        assert len(w.archive.opponent_records) == 1, "the opponent's move was not graded"
        w.step_moves(10 ** 6)                                    # end of game: two-column table
        assert "Rakip" in w.archive_out.toPlainText() and "Blunder" in w.archive_out.toPlainText()

        cut.append(1)                                      # the engine goes down on the second move
        w.sgf_start(text_=sgf, color="W")
        wait_for(lambda: w._sgf_name is None)
        assert "puanlama durdu" in w.sgf_status.text(), w.sgf_status.text()
        unfinished = next(name for name, *_ in store.list_all() if name != w.archive.name)
        w.game_list.setCurrentRow([t[0] for t in w.saved].index(unfinished))
        w.step_moves(10 ** 6)
        assert "2 hamle puanlanmadı" in w.archive_out.toPlainText(), w.archive_out.toPlainText()
        cut.clear()
        w.eng = object()                                   # engine "ready"; fake grade
        w.import_sgf()                                  # selected unfinished game -> resume
        wait_for(lambda: w._sgf_name is None)
        assert not sgf_import.missing(store.load_game(unfinished)), "the unfinished one was not resumed"
        assert len(store.load_game(unfinished).valid_records) == 2, "resuming graded again"

        # --- blunder badge -> opens before the blunder -> branch -> grading -> fixed ---
        import scoring as sc
        real_grade = sc.grade
        sc.grade = lambda eng, moves, mover, *a, **k: {
            "move": moves[-1][1], "mover": mover, "points_lost": 0.4,
            "best_move": "C7", "alternatives": [], "findings": []}
        try:
            m = store.Game(name="game_uitest_blunder", size=9,
                          moves=[["B", "E5"], ["W", "C3"], ["B", "A1"], ["W", "G7"]])
            store.save(m)
            for n, loss in ((1, 0.0), (3, 12.0)):
                store.add_record(m, {"move_no": n, "move": m.moves[n - 1][1], "mover": "B",
                                     "points_lost": loss, "best_move": "C7",
                                     "alternatives": [], "findings": []})
            w.archive_fill()
            row_ = [t[0] for t in w.saved].index(m.name)
            assert "🔴 1 blunder" in w.game_list.item(row_).text(), w.game_list.item(row_).text()
            w.game_list.setCurrentRow(row_)
            assert (w.archive_branch, w.archive_i) == (None, 2), "did not open before the blunder"
            assert "BLUNDER" in w.archive_out.toPlainText(), w.archive_out.toPlainText()
            w.archive_i = 3; w.show_move()
            assert "C7" not in w.archive_out.toPlainText() and not w.canvas.overlays, \
                "the answer to an unfixed blunder must not show"
            w.archive_i = 2; w.show_move()
            w.click(goban.from_gtp("C3", 9))                   # occupied point: illegal
            assert w.archive_branch is None and not w.archive.branches
            w.click(goban.from_gtp("D4", 9))
            assert (w.archive_branch, w.archive_i) == (0, 3), (w.archive_branch, w.archive_i)
            wait_for(lambda: w._branch_job is None)
            assert "Düzeltildi" in w.archive_out.toPlainText(), w.archive_out.toPlainText()
            assert "🔴" not in w.game_list.item(row_).text(), "the badge did not go away"
            w.click(goban.from_gtp("F6", 9))                   # the user plays the opponent too
            assert w.archive.branches[0]["moves"][-1] == ["W", "F6"], w.archive.branches
            y = store.load_game(m.name)
            assert y.unfixed == [] and len(y.branches[0]["moves"]) == 2, "the branch was not saved"
            assert y.moves == m.moves and len(y.valid_records) == 2, "the branch mixed into the main line"
            strip = w.archive_strip
            assert len(strip.nodes) == 1 + 4 + 2, len(strip.nodes)
            assert {d[4] for d in strip.nodes if d[6] is None and d[3] == 3} == {"blunder"}
            w.step_moves(-2)                                         # before the branch's fork: the main line
            assert (w.archive_branch, w.archive_i) == (None, 2), (w.archive_branch, w.archive_i)
            w.strip_select(0, 4)
            assert w.canvas.board.g[goban.from_gtp("F6", 9)] == -1

            # --- mistake review: a fixed blunder becomes a card due tomorrow ---
            import datetime
            today = datetime.date.today()
            card = f"{m.name}#3"
            assert store.review_state()[card] == {"box": 0, "next": (
                today + datetime.timedelta(days=1)).isoformat()}, store.review_state()
            w.mode_select("analysis")
            w.analysis_tabs.setCurrentIndex(0)
            assert w.review is None and "Bugün tekrar yok" in w.review_out.toPlainText()
            w._today = lambda: today + datetime.timedelta(days=1)
            w.load_review()
            assert w.review and w.review[0] == card, w.review_out.toPlainText()
            assert w.canvas.board.g[goban.from_gtp("A1", 9)] == 0, "the review board is not before the blunder"
            w.click(goban.from_gtp("C3", 9))                   # occupied: illegal, does not count as an answer
            assert w.review[3] is None
            w.click(goban.from_gtp("D4", 9))
            wait_for(lambda: w.review and isinstance(w.review[3], dict))
            assert "✓" in w.review_out.toPlainText(), w.review_out.toPlainText()
            assert store.review_state()[card]["box"] == 1, store.review_state()
            assert w.b_review.isEnabled()
            assert store.leitner(4, True) == (4, 30) and store.leitner(3, False) == (0, 1)
            w._today = datetime.date.today

            # --- Problems screen: sets IN ORDER, mistakes as a separate source ---
            import bundle, tempfile, tsumego
            os.environ["GOMENTOR_TSUMEGO"] = os.path.join(bundle.ROOT, "tsumego")
            w.screen_select("problems")
            assert w.mode == "problems", w.mode
            kinds = [t for t, _ in w.prob_rows]
            assert "set" in kinds, w.prob_rows
            # A blunder card due in 3 days: the "Your mistakes" row is not clickable today.
            assert w.prob_rows[0] == (None, None), w.prob_rows[0]

            w.prob_select(kinds.index("set"))
            card = w.prob[0]
            pid = card[len(tsumego.PREFIX):]
            assert tsumego.next_up(w.prob_set) == pid, (pid, w.prob_set)
            state_ = [d for _, d in tsumego.set_problems(w.prob_set)]
            assert state_[0] == "next_up" and set(state_[1:]) <= {"locked"}, state_
            tm, tno, tsolution = tsumego.load_problem(pid)
            tb = tm.board_at(tno - 1)
            assert w.canvas.board.g == tb.g, "the problem position was not set up"

            w.click(next(i for i, v in enumerate(tb.g) if v))      # occupied point
            assert w.prob[3] is None, "an illegal move counted as an answer"
            wrong = next(i for i, v in enumerate(tb.g) if not v
                          and goban.to_gtp(i, tm.size) != tsolution
                          and tb.copy().play(i, goban.color(tm.color)))
            w.click(wrong)
            assert w.prob[3] is False, w.prob_out.toPlainText()
            assert tsolution in w.prob_out.toPlainText(), "a wrong answer must show the solution"
            assert tsumego.next_up(w.prob_set) == pid, "a wrong answer unlocked the next one"
            w.prob_next()
            assert w.prob[0] == card, "a wrongly answered problem must come again"

            w.click(from_gtp(tsolution, tm.size))
            assert w.prob[3] is True, w.prob_out.toPlainText()
            assert w.canvas.board.g[from_gtp(tsolution, tm.size)] == goban.color(tm.color),                 "the answer stone was not put on the board (a stone, not a triangle)"
            assert tsumego.next_up(w.prob_set) != pid, "a right answer did not unlock the next one"
            icon = [w.prob_list.item(i).text()[0] for i in range(1, w.prob_list.count())]
            assert icon[0] == "✓" and icon[1] == "▶", icon

            # Import: a new set from a folder, the list refreshes.
            os.environ["GOMENTOR_TSUMEGO"] = tempfile.mkdtemp()
            w.prob_set = None
            w._import_problems([os.path.join(bundle.ROOT, "tsumego", "basics")], "attempt")
            assert any(t == "set" and d == "attempt" for t, d in w.prob_rows), w.prob_rows
            assert "4 problem eklendi" in w.prob_out.toPlainText(), w.prob_out.toPlainText()
            w.mode_select("archive")
        finally:
            sc.grade = real_grade
    finally:
        store.ROOT, sgf_import.grade_moves, w.eng = real_root, real_grade_, real_eng
        os.environ.pop("GOMENTOR_TSUMEGO", None)
        if real_tsumego:
            os.environ["GOMENTOR_TSUMEGO"] = real_tsumego
        w.archive = None
        w.archive_fill()

    if not w.saved:
        print("no saved games, quiz flow skipped")
        return

    w.game_list.setCurrentRow(0)
    assert len(w.archive_strip.nodes) == 1 + sum(len(d["moves"]) for d in w.archive.branches) \
        + len(w.archive.moves) - goban.setup_count(w.archive.moves), "move strip incomplete"
    assert w.canvas.size == w.archive.size, "the board widget did not take the game's size"
    w.step_moves(-10 ** 6)
    assert w.archive_i == 0, "basa sarma"
    w.step_moves(3)
    assert w.archive_i == 3, "fast forward"

    w.quiz()
    if w.questions:
        r = w.questions[0]
        assert w.archive_i == r["move_no"] - 1, "the quiz board is on the wrong move"
        assert w.archive_branch is None, "the quiz must be on the main line"
        w.click(goban.from_gtp(r["best_move"], w.archive.size))
        assert "✔" in w.archive_out.toPlainText(), "the right answer was not recognized"
        w.click(goban.from_gtp(r["move"], w.archive.size))
        assert r["move"] in w.archive_out.toPlainText(), "the played move was not recognized"
        n = len(w.questions)
        w.quiz()
        assert w.question_i == min(1, n - 1), "did not move on to the next question"
    text_, _ = w.pattern_text()                 # show() opens a window, the test skips it
    assert "Hata örgüsü" in text_, "no pattern report was made"
    w.write_brief()

    # --- end of game: count and (if ranked) SP lines at the last position ---
    finished = next((i for i, (name, *_) in enumerate(w.saved)
                  if goban.game_over(store.load_game(name).moves)), None)
    if finished is not None:
        w.game_list.setCurrentRow(-1)          # if already selected Qt emits no signal
        w.game_list.setCurrentRow(finished)
        w.step_moves(10 ** 6)                       # with a blunder the game opens before it
        text_ = w.archive_out.toPlainText()
        assert "Maç sonu" in text_ and "alan sayımı" in text_ and "komi" in text_, text_
        if w.archive.mode == "ranked":
            assert "Dereceli" in text_, text_
        if w.archive.moves[-1] == ["B", "pass"]:
            w.step_moves(10 ** 6)
            assert "Oyunu bitiren pas" in w.archive_out.toPlainText(), "a pass was taken for an opponent move"

    # --- chart window ---
    # Pick a game with graded moves: the top of the list may be a lesson/empty.
    import charts
    full = next((i for i, (name, *_) in enumerate(w.saved)
                 if store.load_game(name).valid_records), None)
    if full is None:
        print("no graded games, chart flow skipped")
        return
    w.game_list.setCurrentRow(full)
    d = charts.ChartView(w, w.archive, skip=w.skip_charts,
                               all_games=w._all_games())
    k = w.archive.valid_records
    assert d.list_.count() == 1, "the list must open with the empty hint"

    # Histogram: clicking a bucket must fill the list, clicking the same bucket
    # again must clear the selection.
    columns = d.findChildren(charts.Column)
    assert columns, "histogram was not drawn"
    hist = columns[0]
    hist.resize(600, 190)                     # hit-testing needs the real width
    full_bucket = next(i for i, x in enumerate(d._buckets) if x)
    x0, _, gen, _ = hist._area()
    px = x0 + (full_bucket + 0.5) * gen / len(hist.labels)
    assert hist._bucket(px) == full_bucket, (hist._bucket(px), full_bucket)
    d._bucket_selected(full_bucket)
    assert d.list_.count() == len(d._buckets[full_bucket]) + 1, "bucket list incomplete"
    d._bucket_selected(None)
    assert d.list_.count() == 1, "the list must empty when the selection clears"
    assert sum(len(x) for x in d._buckets) == len(k), "the buckets must cover every move"

    # Line: a click must find the nearest data point. findChildren's order is NOT
    # creation order (the game and trend tabs mix), so each chart is checked
    # against its own data.
    lines_ = d.findChildren(charts.Line)
    assert lines_, "line chart was not drawn"
    for c in lines_:
        c.resize(600, 195)
        x = c.runs[0]["data"][0][0]
        assert c._nearest(c._px(x)) == x, (c._nearest(c._px(x)), x)
        assert c._nearest(-9999) is None, "a far click must not invent a point"

    # A click must take the archive to that move (chart -> board).
    goal = k[len(k) // 2]["move_no"]
    d._to_move(goal)
    assert w.archive_i == goal, (w.archive_i, goal)
    assert w.archive_branch is None, "the chart must go to the main line"
    assert d.list_.count() == 2, "a single move should have been listed"

    # Heatmap: clicking a played point must give that point's moves.
    if d._map:
        idx = max(d._map, key=lambda i: d._map[i]["lost"])
        d._point(idx)
        assert d.list_.count() == len(d._map[idx]["records"]) + 1
        empty = next((i for i in range(w.archive.size ** 2) if i not in d._map), None)
        if empty is not None:
            d._point(empty)                     # an unplayed point must not crash
            assert d.list_.count() == 1

    # Clicking a row in the list must take the archive along (chart -> list -> board).
    d._bucket_selected(full_bucket)
    it = d.list_.item(1)
    d._row(it)
    assert w.archive_i == it.data(Qt.UserRole), "the list click did not move the board"

    # --- time control: setting -> clock on a new game -> loss when it runs out ---
    w.mode_select("settings")
    w.time_box.setChecked(True)
    w.time_min_box.setCurrentText("5")
    assert store.setting("clock_on") and store.setting("clock_min") == 5, "the time setting was not saved"
    w.mode_select("game")
    w.next_task()
    # Fixed name: since a timeout goes through the resign path, the game IS
    # written to disk. With a generated name every run would leave a game in the
    # archive, and the next run's archive tests (which pick the newest game) would
    # find it.
    w.game.name = "game_uitest_clock"
    assert w.game.meta["clock"]["remaining"] == {"B": 300.0, "W": 300.0}, w.game.meta

    # The clock does not run until the first stone; it only runs on your turn, on the play screen.
    w.phase, w.screen_ = "your_turn", "play"
    w._clock_mark = time.monotonic() - 2
    w._clock_tick()
    assert w.game.meta["clock"]["remaining"]["B"] == 300.0, "the clock ran before the first stone"
    w.game.moves = [["B", "E5"], ["W", "E4"]]     # first stone placed, Black to move
    w._clock_mark = time.monotonic() - 2            # as if thinking for 2 seconds
    w._clock_tick()
    assert 297 < w.game.meta["clock"]["remaining"]["B"] < 299, w.game.meta["clock"]
    assert "4:5" in w.clock.text(), w.clock.text()
    w.screen_ = "settings"                          # another screen: the clock must stop
    w._clock_tick()
    remaining = w.game.meta["clock"]["remaining"]["B"]
    assert w._clock_mark is None, "the clock did not stop on another screen"
    w._clock_tick()
    assert w.game.meta["clock"]["remaining"]["B"] == remaining, "a stopped clock kept running"

    # Running out of time is a loss -- the SAME path as resigning (how='clock').
    w.screen_, w.phase = "play", "your_turn"
    w.game.meta["clock"]["remaining"]["B"] = 0.5
    w._clock_mark = time.monotonic() - 1
    w._clock_tick()
    end = time.time() + 5                        # the "had time not run out" count, in the background
    while w.phase != "done" and time.time() < end:
        w.pump()
        app.processEvents()
    assert w.phase == "done", w.phase
    assert "Süre bitmeseydi puan durumu:" in w.out.toPlainText(), w.out.toPlainText()
    assert w.game.meta["result"] == "clock", w.game.meta
    assert "doldu" in w.status.text(), w.status.text()   # Status capitalizes the first letter

    # Two-player game: the clock of the side TO MOVE runs, the other one stops.
    w.mode_select("duo")
    w.next_task()
    w.game.name = "duo_uitest_clock"
    w.game.moves = [["B", "D4"]]                  # White to move
    w.phase, w.screen_ = "your_turn", "play"
    w._clock_mark = time.monotonic() - 2
    w._clock_tick()
    k = w.game.meta["clock"]["remaining"]
    assert k["B"] == 300.0 and k["W"] < 299, k

    w.mode_select("settings")                         # with the setting off, no clock may be attached
    w.time_box.setChecked(False)
    w.time_min_box.setCurrentText("20")
    w.mode_select("game")
    w.next_task()
    assert "clock" not in w.game.meta, w.game.meta
    _uitest_cleanup()

    print(f"uitest ok ({len(w.questions)}-question quiz, patterns, brief, "
          f"charts: {len(lines_)} lines + {len(columns)} columns)")


def make_pattern():
    """Writes the mistake pattern report over all saved games. Returns: the file path."""
    games = []
    for name, *_ in store.list_all():
        m = store.load_game(name)
        k = m.valid_records
        if k:
            games.append((name, m.size, m.moves, k))
    text_ = scoring.pattern(games)
    path_ = store.sub("report", "_patterns.md")
    with open(path_, "w", encoding="utf-8") as f:
        f.write(text_)
    return path_, text_


def scan_big_points():
    """Measures the big_point thresholds over all saved games.

    scoring.OPEN_THRESHOLD / OPEN_RATIO were chosen from this output. Rerun it
    when the board size changes or another player's games pile up -- no
    constant in this project is made up.
    """
    import statistics as st
    K = []
    for name, *_ in store.list_all():
        m = store.load_game(name)
        for r in m.valid_records:
            ei, oy = r.get("best_move"), r.get("move")
            if not ei or ei == "pass" or not oy or oy == "pass":
                continue
            try:
                pi = scoring.region_of(goban.from_gtp(oy, m.size), m.size)
                ii = scoring.region_of(goban.from_gtp(ei, m.size), m.size)
            except ValueError:
                continue
            sk = scoring.open_scores(goban.replay(m.moves[:r["move_no"] - 1], m.size))
            K.append((pi != ii, sk[ii], sk[ii] / max(sk[pi], 0.1), r["points_lost"], name))
    if not K:
        print("no saved games")
        return
    print(f"n={len(K)} moves, {len(store.list_all())} games. "
          f"base loss {st.mean(k[3] for k in K):.2f}p")
    print()
    print(f"{'threshold':>5}{'ratio':>6}{'fires':>6}{'%':>5}{'fires':>9}{'silent':>9}{'separation':>7}")
    for threshold in (3, 6, 9, 12, 15):
        for ratio in (1.3, 1.6, 2.0, 3.0):
            a = [k for k in K if k[0] and k[1] >= threshold and k[2] >= ratio]
            b = [k for k in K if not (k[0] and k[1] >= threshold and k[2] >= ratio)]
            if len(a) < 5:
                continue
            ma, mb = st.mean(x[3] for x in a), st.mean(x[3] for x in b)
            mark = "  <-- secili" if (threshold, ratio) == (scoring.OPEN_THRESHOLD,
                                                        scoring.OPEN_RATIO) else ""
            print(f"{threshold:>5}{ratio:>6}{len(a):>6}{100*len(a)/len(K):>4.0f}%"
                  f"{ma:>8.2f}p{mb:>8.2f}p{ma/mb:>6.1f}x{mark}")


def make_brief(name):
    """Writes a saved game's brief. Returns: the file path."""
    m = store.load_game(name)
    k = m.valid_records
    text_ = scoring.brief(m.name, m.mode, m.size, k, game_level(k))
    path_ = store.sub("brief", f"{name}_brief.md")
    with open(path_, "w", encoding="utf-8") as f:
        f.write(text_)
    return path_


# ---------- tests ----------
def selftest():
    """No engine, fast. Rules + coordinates + SGF + level scale + detection."""
    b = Board()
    for m, c in [("E5", 1), ("E4", -1), ("D4", 1), ("F4", 1), ("E3", 1)]:
        assert b.play(from_gtp(m), c), m
    assert b.g[from_gtp("E4")] == 0, "the surrounded stone should have been captured"
    assert b.play(from_gtp("E5"), -1) is False, "dolu nokta"

    b2 = Board()
    for m in ("D5", "F5", "E6", "E4"):
        assert b2.play(from_gtp(m), 1)
    assert b2.play(from_gtp("E5"), -1) is False, "suicide is illegal"
    assert b2.g[from_gtp("D5")] == 1, "a failed move must not touch the board"

    b3 = Board()                                   # classic ko
    for m in ("D5", "E6", "E4"):
        b3.g[from_gtp(m)] = 1
    for m in ("E5", "F6", "F4", "G5"):
        b3.g[from_gtp(m)] = -1
    assert b3.play(from_gtp("F5"), 1), "F5 should have been legal"
    assert b3.g[from_gtp("E5")] == 0, "E5 should have been captured"
    assert b3.ko == from_gtp("E5"), "ko should have been set up"
    assert b3.play(from_gtp("E5"), -1) is False, "ko ban"

    # two eyes = life; filling one eye = death
    black = ["B9", "A8", "B8", "B7", "B6", "A6"]
    white = ["C9", "C8", "C7", "C6", "B5", "A5"]
    seq = [m for pair in zip([["B", x] for x in black], [["W", x] for x in white])
           for m in pair]
    is_live = replay(seq)
    _, libs = is_live.group(from_gtp("B9"))
    assert libs == {from_gtp("A9"), from_gtp("A7")}, f"should have been two eyes: {libs}"
    assert is_live.play(from_gtp("A9"), -1) is False, "playing into a two-eyed group is suicide"

    seq = seq + [["B", "A9"]]
    dead = replay(seq)
    _, libs = dead.group(from_gtp("A9"))
    assert libs == {from_gtp("A7")}, f"the single liberty should have been A7: {libs}"
    finding = detection(seq, "B", "A7")
    kinds = lambda bs: [b["kind"] for b in bs]
    assert "own_area" in kinds(finding), finding
    assert {"kind": "one_liberty", "dimension": 7, "point": "A7"} in finding, finding
    assert "one_liberty" in kinds(detection(seq, "B", "J1")), "the threat is independent of the answer"
    assert {"kind": "capture", "point": "A7", "num": 7} in finding, "A7 must capture"
    assert detection([["B", "pass"]], "B", "A9") == []

    # Tsumego: is the solution of every bundled problem the ONE right move.
    # Building shapes by hand is very error-prone -- when the end of the eye space
    # is closed with the wrong color the problem silently breaks. That is why it
    # is verified by exhaustive search (tsumego.verify).
    import tsumego
    problems = tsumego.list_all()
    assert problems, f"no tsumego problems found: {tsumego.folders()}"
    for pid, _ in problems:
        ok, reason = tsumego.verify(pid)
        assert ok, f"tsumego {pid}: {reason}"
    m, no, solution = tsumego.load_problem(problems[0][0])
    assert m.mode == "tsumego" and m.color in ("B", "W"), m
    assert no == len(m.moves) + 1, "move_no must be one more than the number of setup stones"
    assert m.board_at(no - 1).g[from_gtp(solution, m.size)] == 0, "the solution point must be empty"

    # A set is solved IN ORDER; a right answer unlocks the next one, a WRONG answer does not.
    import tempfile
    today, old_root, old_env = datetime.date.today(), store.ROOT, os.environ.get("GOMENTOR_TSUMEGO")
    try:
        store.ROOT = tempfile.mkdtemp()                 # a clean reviews.json
        set0 = tsumego.sets()[0][0]
        state_ = [d for _, d in tsumego.set_problems(set0)]
        assert state_[0] == "next_up" and set(state_[1:]) <= {"locked"}, state_
        first_ = tsumego.next_up(set0)
        store.apply_review(tsumego.PREFIX + first_, True, today)
        state_ = [d for _, d in tsumego.set_problems(set0)]
        assert state_[:2] == ["solved", "next_up"], state_
        second = tsumego.next_up(set0)
        store.apply_review(tsumego.PREFIX + second, False, today)
        assert tsumego.next_up(set0) == second, "a wrong answer must not unlock the next one"
        assert tsumego.reviews(today + datetime.timedelta(days=1)) ==             [(tsumego.PREFIX + second, (today + datetime.timedelta(days=1)).isoformat())]

        # Import: a broken file is NOT LEFT in the set.
        source = tempfile.mkdtemp()
        shutil.copy(dict(tsumego.list_all())[first_], os.path.join(source, "iyi.sgf"))
        with open(os.path.join(source, "bozuk.sgf"), "w", encoding="utf-8") as f:
            f.write("(;GM[1]FF[4]SZ[9])")             # no setup stones
        os.environ["GOMENTOR_TSUMEGO"] = tempfile.mkdtemp()
        name, appended, skipped = tsumego.import_set(source, "attempt")
        assert (name, appended, skipped) == ("attempt", 1, 1), (name, appended, skipped)
        assert [p for p, _ in tsumego.list_all()] == ["attempt/iyi"], tsumego.list_all()

        # Do not take your own game record for a problem: a handicap SGF has AB too.
        game_sgf = "(;GM[1]FF[4]SZ[9]KM[7.5]AB[cc][gg]" +             "".join(f";{'BW'[i % 2]}[{chr(97 + i % 9)}{chr(97 + i // 9)}]" for i in range(50)) + ")"
        source2 = tempfile.mkdtemp()
        with open(os.path.join(source2, "macim.sgf"), "w", encoding="utf-8") as f:
            f.write(game_sgf)
        assert tsumego.import_set(source2, "gameset") == ("gameset", 0, 1), "a game record counted as a problem"
    finally:
        store.ROOT = old_root
        os.environ.pop("GOMENTOR_TSUMEGO", None)
        if old_env:
            os.environ["GOMENTOR_TSUMEGO"] = old_env

    # Findings are stored as structures, the text is made in the selected language.
    import i18n, json as _json
    assert _json.loads(_json.dumps(finding)) == finding, "must be writable to jsonl"
    try:
        assert i18n.set_lang("en") == "en" and i18n.t("evet", "yes") == "yes"
        en = [scoring.finding_text(b) for b in finding]
        assert any("ONE liberty (A7)" in x for x in en) and any("7 stones by playing A7" in x
                                                                 for x in en), en
        i18n.set_lang("xx")
        assert i18n.selected() == "tr", "an unknown language must fall back to Turkish"
        tr = [scoring.finding_text(b) for b in finding]
        assert any("7 taşlık grubunun TEK nefesi kaldı (A7)" in x for x in tr), tr
    finally:
        i18n.set_lang("tr")
    assert i18n.system_lang() in i18n.LANGUAGES
    # Old records carry Turkish SENTENCES: they must parse back into the same structure; the file is not rewritten.
    for b in finding:
        assert scoring.finding_struct(scoring.finding_text(b)) == b, b
    old_bn = ("Tahtanın asıl boş yeri sol ust bölgesiydi ve motorun istediği D11 "
               "orada. Sen sol bölgesinde oynadın.")
    assert scoring.finding_struct(old_bn) == {"kind": "big_point", "empty": "top_left",
                                           "point": "D11", "yours": "left"}
    assert scoring.habits_of(old_bn) == scoring.habits_of(scoring.finding_struct(old_bn))
    assert scoring.finding_struct("tanınmayan eski cümle") is None
    assert scoring.finding_text("tanınmayan eski cümle") == "tanınmayan eski cümle"
    record = {"move_no": 1, "move": "E5", "best_move": "E5", "findings": [old_bn]}
    assert scoring.findings_full([], 9, record) == [scoring.finding_struct(old_bn)], "old sentence normalized"

    # SGF round trip
    mv = [["B", "E5"], ["W", "C3"], ["B", "pass"], ["W", "A9"]]
    assert sgf_load(sgf_dump(mv)) == mv
    # Outside SGF: a fake move and an escape inside a comment, variations (main
    # line = first subtree), handicap, the old way of writing pass, an FF3 long
    # property name.
    outer = r"""(;FF[4]GM[1]SZ[19]KM[0.5]HA[2]PB[a]PW[b\]c]RE[W+3.5]RU[Japanese]
      AB[pd][dp]C[yorum ;B[aa] ve \] parantez];W[qp]C[ok];B[dd]
      (;W[fc];B[tt](;W[])(;W[aa]))(;W[cc]))"""
    o = goban.sgf_read(outer)
    assert o["moves"] == [["AB", "Q16"], ["AB", "D4"], ["W", "R4"], ["B", "D16"],
                          ["W", "F17"], ["B", "pass"], ["W", "pass"]], o["moves"]
    assert o["info"]["PW"] == "b]c" and o["komi"] == 0.5 and goban.setup_count(o["moves"]) == 2
    assert goban.sgf_rule(o["info"]) == "japanese" and goban.sgf_rule({}) == "chinese"
    assert goban.move_count(o["moves"]) == 5 and goban.display_no(o["moves"], 3) == 1
    assert goban.replay(o["moves"], 19).g[from_gtp("Q16", 19)] == 1, "setup stone not on the board"
    o2 = goban.sgf_read(sgf_dump(o["moves"], 19, 0.5, info=o["info"], you="W"))
    assert (o2["moves"], o2["you"], o2["info"]) == (o["moves"], "W", o["info"]), "round trip"
    branched_sgf = sgf_dump(mv, branches=[{"parent": None, "root": 1, "moves": [["W", "E4"], ["B", "D4"]]}])
    assert "(;W[ef];B[df])" in branched_sgf and sgf_load(branched_sgf) == mv, "varyasyonlu SGF"
    assert goban.sgf_read("(;SZ[9]AddBlack[ee];White[cc])")["moves"] == [["AB", "E5"], ["W", "C7"]]
    for bad in ("", "(;SZ[9];B[zz])", "(;SZ[9];B[aa];AB[cc])", "(;SZ[9]AE[aa])"):
        try:
            goban.sgf_read(bad)
            raise AssertionError(f"should have been rejected: {bad}")
        except ValueError:
            pass
    assert goban.sgf_result("W+3.5") == ("W", 3.5) and goban.sgf_result("B+R") == ("B", None)
    assert goban.sgf_result("0") == (None, 0.0) and goban.sgf_result("Void") == (None, None)
    assert scoring.captures([["AB", "E5"], ["W", "D5"]], 9) == {"B": 0, "W": 0}, "setup counted as a capture"
    assert to_sgf(0) == "aa" and to_sgf(from_gtp("J1")) == "ii"
    assert to_gtp(from_gtp("J1")) == "J1" and to_gtp(0) == "A9"
    assert goban.game_over([["B", "pass"], ["W", "pass"]])
    assert not goban.game_over([["B", "pass"], ["W", "C3"]])

    # replay(upto=) -- replaying old games is built on this
    partial = replay(mv, upto=1)
    assert partial.g[from_gtp("E5")] == 1 and partial.g[from_gtp("C3")] == 0

    # the level scale must be continuous and monotonic
    assert rank_value("20k") == -19 and rank_value("1k") == 0 and rank_value("1d") == 1
    assert rank_value("15k") < rank_value("10k") < rank_value("1k") < rank_value("3d")
    assert value_rank(-14) == "15k" and value_rank(0) == "1k" and value_rank(2) == "2d"
    assert value_rank(rank_value("8k")) == "8k"

    # expected loss: if the distribution piles onto the good move, the loss must be small
    mi = [{"move": "A", "scoreLead": 10.0}, {"move": "B", "scoreLead": 6.0}]
    assert abs(expected_loss(mi, [("A", 1.0)], 1) - 0.0) < 1e-9
    assert abs(expected_loss(mi, [("B", 1.0)], 1) - 4.0) < 1e-9
    assert abs(expected_loss(mi, [("A", .5), ("B", .5)], 1) - 2.0) < 1e-9
    # a move the engine did not look at is charged as much as the worst move it did look at
    assert abs(expected_loss(mi, [("ZZ", 1.0)], 1) - 4.0) < 1e-9

    # level estimate: better than 70 and worse than 90 means ~80
    curve = [("10k", 4.0), ("6k", 2.0)]              # 10k=-9, 6k=-5
    t = level_estimate(3.0, curve)                    # exactly in the middle
    # raw_value: the plain interpolation on the ladder (what this test is about)
    assert abs(t["raw_value"] - (-7.0)) < 1e-6, t    # halfway between -9 and -5
    assert level_estimate(9.9, curve)["at_limit"] == "bottom"
    assert level_estimate(0.0, curve)["at_limit"] == "top"
    assert abs(level_estimate(4.0, curve)["raw_value"] - (-9)) < 1e-6
    # value: the player rank corrected with the MEASURED offset (calibration run).
    # The tool came out systematically +4.3 ranks inflated against the ladder; the
    # offset closes that gap.
    assert abs(t["value"] - (t["raw_value"] - scoring.LEVEL_OFFSET)) < 1e-6, t
    assert t["value"] < t["raw_value"], "the correction must go toward WEAKER"
    # separation threshold: if the ladder is squeezed, NO estimate (opening positions)
    narrow = [("20k", 0.98), ("15k", 0.98), ("10k", 1.09), ("6k", 0.79),
           ("2k", 0.49), ("1d", 0.39)]              # a real measured curve
    assert level_estimate(0.4, narrow) is None, "no estimate on a narrow ladder"
    assert level_estimate(3.0, curve) is not None, "an estimate on a wide ladder"

    # a retracted move must stay out of the statistics: the marker cancels the
    # record BEFORE IT. Dropping only the markers had broken a real game.
    m = store.Game(name="t", size=9)
    m.records = [{"move_no": 1, "move": "C1", "level_points": 0.0},
                  {"move_no": 1, "retracted": True},
                  {"move_no": 1, "move": "J5", "level_points": 10.0}]
    assert [k["move"] for k in m.valid_records] == ["J5"], m.valid_records
    assert m.record_of(1)["move"] == "J5"
    # the end-of-game row must stay out of the statistics and be read as the result
    m.records.append({"game_end": {"result": "resign"}})
    assert [k["move"] for k in m.valid_records] == ["J5"], m.valid_records
    assert m.result == {"result": "resign"}

    # ---- move category ----
    cat = lambda loss, move="C3", en="D4", bottom2=0.0: scoring.category(
        {"points_lost": loss, "move": move, "best_move": en,
         "alternatives": [{"diff": 0.0}, {"diff": bottom2}]})
    assert cat(0.0, "D4", "D4", 3.0) == "great", "the only right move"
    assert cat(0.0, "D4", "D4", 2.9) == "excellent" and cat(0.2) == "excellent"
    assert [cat(x) for x in (0.3, 0.9, 1.0, 2.9, 3.0, 5.9, 6.0, 20)] == \
        ["good", "good", "inaccuracy", "inaccuracy", "mistake", "mistake",
         "blunder", "blunder"]
    assert scoring.category(None) is None and scoring.category({"move": "C3"}) is None
    assert sum(scoring.category_count([{"points_lost": 7}, {}]).values()) == 1

    # ---- the opponent's records (import) stay out of the user's statistics ----
    m = store.Game(name="t", size=9, color="W",
                  moves=[["B", "E5"], ["W", "C3"], ["B", "G7"]])
    m.records = [{"move_no": 1, "mover": "B", "points_lost": 9.0},
                  {"move_no": 2, "mover": "W", "points_lost": 7.0, "move": "C3"},
                  {"move_no": 3, "points_lost": 0.1}]      # old record: no mover
    assert [k["move_no"] for k in m.valid_records] == [2, 3], m.valid_records
    assert [k["move_no"] for k in m.opponent_records] == [1]
    assert m.record_of(1)["mover"] == "B", "record_of must find both colors"

    # ---- variation tree + blunder fixing ----
    assert goban.turn([]) == "B" and goban.turn([["AB", "C3"]]) == "W"
    assert goban.turn([["B", "E5"], ["W", "pass"]]) == "B"
    m.color = "W"
    assert m.unfixed == [2], m.unfixed
    assert m.branch_play(None, 1, ["W", "C3"]) == (None, 2, False), "a main-line move does not open a branch"
    assert m.branch_play(None, 1, ["W", "D4"]) == (0, 2, True)
    assert m.branch_play(None, 1, ["W", "D4"]) == (0, 2, False), "same move, same branch"
    assert m.branch_play(0, 2, ["B", "F6"]) == (0, 3, True), "a branch grows at its end"
    assert m.branch_play(0, 2, ["B", "F4"]) == (1, 3, True), "a new branch from the middle of a branch"
    assert m.line_moves(1) == [["B", "E5"], ["W", "D4"], ["B", "F4"]], m.line_moves(1)
    assert m.owner(1, 2) == 0 and m.owner(1, 1) is None and m.owner(1, 3) == 1
    assert m.board_at(3, 1).g[goban.from_gtp("F4")] == 1
    assert len(m.moves) == 3 and len(m.valid_records) == 2, "a branch must not touch the main line"
    m.branches[0]["records"].append({"move_no": 2, "points_lost": 8.0})
    assert m.unfixed == [2], "blunder'la duzeltme olmaz"
    m.branches[0]["records"].append({"move_no": 2, "points_lost": 0.5})
    assert m.unfixed == [], "a non-blunder attempt fixes it"

    # ---- game name on screen: "Ranked 17" (the file name does not change) ----
    import tempfile
    old_root = store.ROOT
    try:
        store.ROOT = tempfile.mkdtemp()
        for name in ("ranked_20260901-100000", "ranked_20260902-100000", "game_20260901-090000"):
            open(os.path.join(store.ROOT, name + ".sgf"), "w").close()
        assert store.display_name("ranked_20260902-100000").endswith(" 2")
        assert store.display_name("game_20260901-090000").endswith(" 1")
        assert store.display_name("ranked_20260903-100000").endswith(" 3"), "a new game gets the next number"
    finally:
        store.ROOT = old_root

    # ---- end of game: Chinese-rules area count ----
    # Column D black, column F white, E empty (dame). A-C belong to Black, G-J to White.
    tb = Board(9)
    for r in range(1, 10):
        assert tb.play(from_gtp(f"D{r}"), 1) and tb.play(from_gtp(f"F{r}"), -1)
    sy = scoring.area_count(tb, None, 7.0)
    assert sy["black"]["total"] == 36 and sy["white"]["total"] == 36, sy
    assert sy["neutral"] == 9 and sy["diff"] == -7.0, sy
    # A dead white stone in Black's area: with ownership it counts for Black (26
    # empty + 1 dead, so the total does not change); flood fill cannot remove it
    # and the region becomes nobody's.
    tb.play(from_gtp("B5"), -1)
    owner = [1.0 if i % 9 < 4 else -1.0 if i % 9 > 4 else 0.0 for i in range(81)]
    sy = scoring.area_count(tb, owner, 7.0)
    assert sy["black"] == {"stone": 9, "region": 26, "dead": 1, "total": 36}, sy
    assert sy["neutral"] == 9 and sy["method"] == "ownership", sy
    assert scoring.area_count(tb, None, 7.0)["neutral"] == 9 + 26, "flood fill does not remove dead stones"
    # Capture: Black surrounds E5 on four sides and takes it.
    hm = [["B", "D5"], ["W", "E5"], ["B", "F5"], ["W", "A1"],
          ["B", "E4"], ["W", "A2"], ["B", "E6"]]
    assert scoring.captures(hm) == {"B": 1, "W": 0}, scoring.captures(hm)
    assert "komi 7" in scoring.rules(7.0)
    assert any("36 − 43 = -7" in x for x in scoring.count_lines(sy, -6.4)), "count line"

    # ---- ranked games: SP / rank ----
    # Elo symmetry: against an equal opponent a win is +K/2, a loss -K/2.
    assert abs(scoring.sp_change(True, 0) - scoring.K / 2) < 0.01
    assert abs(scoring.sp_change(False, 0) + scoring.K / 2) < 0.01
    # Beating a stronger opponent is worth more, losing to them costs less.
    assert scoring.sp_change(True, 4) > scoring.sp_change(True, 0)
    assert scoring.sp_change(False, 4) > scoring.sp_change(False, 0)
    assert scoring.sp_change(True, -4) < scoring.sp_change(True, 0)
    # The D constant was NOT made up: it must fit the measured win rates.
    # 360 bot-vs-bot games: a 5-rung gap -> the stronger side wins 73%, 4 rungs ->
    # 70%, 20 rungs -> ~97% (calibration run).
    for rung, measured in ((5, 0.73), (4, 0.70), (20, 0.97)):
        t = 1 - scoring.win_prob(rung)          # the stronger side's chance to win
        assert abs(t - measured) < 0.06, f"{rung} basamak: {t:.2f} vs {measured}"

    # Example: at 15k / 0 SP, gaining 110 SP must give 14k / 10 SP.
    v = scoring.rank_value("15k")
    new, sp = divmod(v * scoring.SP_RANK + 0 + 110, scoring.SP_RANK)
    assert scoring.value_rank(new) == "14k" and sp == 10, (new, sp)

    # Rank changes must work in both directions and stop at the floor.
    d, s, _ = scoring.apply_sp(scoring.rank_value("15k"), 95.0, True, v)
    assert scoring.value_rank(d) == "14k" and 0 <= s < 100, (d, s)
    d, s, _ = scoring.apply_sp(scoring.rank_value("15k"), 2.0, False, v)
    assert scoring.value_rank(d) == "16k", d
    d, s, _ = scoring.apply_sp(scoring.WEAKEST, 0.0, False, scoring.WEAKEST)
    assert d == scoring.WEAKEST and s == 0.0, "the 9x9 floor is 30k"
    d, s, _ = scoring.apply_sp(scoring.HUMAN_FLOOR, 0.0, False, scoring.HUMAN_FLOOR)
    assert d < scoring.HUMAN_FLOOR, "someone losing at 20k must be able to go lower now"
    tab = {b: scoring.value_rank(scoring.weakest(b)) for b in (9, 13, 19)}
    assert tab == {9: "30k", 13: "26k", 19: "23k"}, tab
    d, s, _ = scoring.apply_sp(scoring.weakest(19), 0.0, False, scoring.weakest(19),
                             size=19)
    assert d == scoring.weakest(19), "must not fall below the size's floor"
    assert scoring.crop(scoring.WEAKEST, size=13) == scoring.weakest(13)

    # A bot below 20k: the 20k profile + temperature; the weaker it gets the higher
    # the temperature, but not below the measured floor (the label is clipped to
    # that floor too).
    assert scoring.bot_setting(scoring.rank_value("15k"), 9, {"pass_": True}) == (
        "rank_15k", {"pass_": True}), "20k and up unchanged"
    for size_ in (9, 13, 19):
        floor = scoring.weakest(size_)
        temps = []
        for value in range(scoring.HUMAN_FLOOR - 1, floor - 1, -1):
            profile_, ay = scoring.bot_setting(value, size_, {"pass_": True})
            assert profile_ == "rank_20k" and ay["tail"] == 1.0 and ay["pass_"], ay
            temps.append(ay["temp"])
        assert temps == sorted(temps) and len(set(temps)) == len(temps), (size_, temps)
        assert 1.0 < temps[0] and temps[-1] <= scoring.WEAK_MEASURED[size_][-1][0]
        assert scoring.bot_setting(floor - 3, size_)[1]["temp"] == temps[-1]

    # Provisional K: a game weighs more while new, back to normal when it expires.
    assert scoring.k_factor(0) == scoring.K_PROVISIONAL
    assert scoring.k_factor(scoring.PROVISIONAL_GAMES) == scoring.K
    p0 = scoring.sp_change(True, 0, scoring.k_factor(0))
    p1 = scoring.sp_change(True, 0, scoring.k_factor(scoring.PROVISIONAL_GAMES))
    assert p0 > p1 * 2, (p0, p1)
    # The same game must shift the rank more while provisional.
    v15 = scoring.rank_value("15k")
    a = scoring.apply_sp(v15, 0.0, True, v15, 0)[0] * 100 + scoring.apply_sp(v15, 0.0, True, v15, 0)[1]
    b = scoring.apply_sp(v15, 0.0, True, v15, 99)[0] * 100 + scoring.apply_sp(v15, 0.0, True, v15, 99)[1]
    assert a > b, (a, b)

    # A streak grows the OPPONENT, not the SP; without a streak, an equal opponent.
    assert scoring.opponent_choice(v, 0) == v and scoring.opponent_choice(v, 1) == v
    assert scoring.opponent_choice(v, 4) > scoring.opponent_choice(v, 2) > v

    # Placement: the first game is always 20k, then a binary search.
    first_ = scoring.PLACEMENT_START
    assert scoring.placement_opponent([]) == first_ == scoring.rank_value("20k")
    won = scoring.placement_opponent([(first_, True)])
    assert won > first_, "must go up after beating 20k"
    # Losing to 20k does not lock you to the floor: the search goes down between 20k and the size's floor.
    for size_ in (9, 13, 19):
        lost = scoring.placement_opponent([(first_, False)], size_)
        assert scoring.weakest(size_) < lost < first_, (size_, scoring.value_rank(lost))
    assert scoring.placement_opponent([(scoring.WEAKEST, False)]) == scoring.WEAKEST
    # After a loss the ceiling drops: the same point must not be offered again.
    assert scoring.placement_opponent([(first_, True),
                                   (won, False)]) < won
    spot = [(first_, True), (won, False)]
    s = scoring.placement_result(spot)
    assert s["uncertainty"] == scoring.PLACEMENT_UNCERTAINTY and len(s["spacing"]) == 2
    assert s["source"] == "binary-search"

    # With the move-quality signal added the uncertainty must DROP and the result
    # must shift toward quality (measured: single-game spread 3.16r, binary search 4.0r).
    stay = [scoring.rank_value("12k")] * 3
    s2 = scoring.placement_result(spot, stay)
    assert s2["uncertainty"] < s["uncertainty"], (s["uncertainty"], s2["uncertainty"])
    assert s2["source"] == "binary+quality" and s2["quality_games"] == 3
    assert abs(s2["value"] - stay[0]) < abs(s["value"] - stay[0]), "must shift toward quality"
    # Without quality (no game produced an estimate) it must fall back to the old behavior.
    assert scoring.placement_result(spot, [None, None, None])["source"] == "binary-search"

    # --- the game result is three-valued: a draw is NOT a loss, the count decides the winner ---
    assert goban.KOMI == 7.5, "Chinese-rules komi has a half point"
    # star points: the standard layout. Corners + tengen on 9/13, nine points on 19.
    year = lambda n: [goban.to_gtp(i, n) for i in goban.star_points(n)]
    assert year(9) == ["C7", "G7", "C3", "G3", "E5"], year(9)
    assert year(13) == ["D10", "K10", "D4", "K4", "G7"], year(13)
    assert year(19) == ["D16", "K16", "Q16", "D10", "K10", "Q10",
                       "D4", "K4", "Q4"], year(19)
    assert goban.star_points(5) == [], "no star points on a tiny board"

    # calibration by size: the measured D, and a warning on an uncalibrated size
    assert scoring.d_const(9) == scoring.D and scoring.d_const(13) == 730
    assert scoring.d_const(19) == 460 and scoring.d_const(15) == scoring.D,         "an unmeasured size falls back to 9x9"
    assert scoring.win_prob(5, 730) < scoring.win_prob(5, 1090), "narrower D, less chance for the weaker side"
    assert scoring.apply_sp(-9, 0, 1, -4, 99, 730)[2] >         scoring.apply_sp(-9, 0, 1, -4, 99, 1090)[2], "D does not carry over into SP"
    assert scoring.calibration_note(9) == "", "9x9 is calibrated, no warning"
    assert "D=730" in scoring.calibration_note(13) and "KAPALI" in scoring.calibration_note(13)
    assert "D=460" in scoring.calibration_note(19) and "çözmüyor" in scoring.calibration_note(19)

    # The KataGo path is not pinned to one person; missing files are reported before the engine starts
    import engine, tempfile
    old_env = os.environ.pop("GOMENTOR_KATAGO_DIR", None)
    try:
        os.environ["GOMENTOR_KATAGO_DIR"] = "X"
        assert engine.katago_root() == "X", "the environment variable takes precedence"
        del os.environ["GOMENTOR_KATAGO_DIR"]
        assert os.path.basename(engine.katago_root()) == "katago"
        empty = tempfile.mkdtemp()
        c = Config(katago=os.path.join(empty, "yok.exe"), model=os.path.join(empty, "m"),
                   human=os.path.join(empty, "h"), cfg=__file__)
        assert engine.missing_files(c) == [c.katago, c.model, c.human], engine.missing_files(c)
        assert c.human not in engine.missing_files(c, human=False)
        try:
            Engine(c)
            assert False, "the engine must not start with missing files"
        except engine.EngineError as e:
            assert "yok.exe" in str(e), e
    finally:
        os.environ.pop("GOMENTOR_KATAGO_DIR", None)
        if old_env is not None:
            os.environ["GOMENTOR_KATAGO_DIR"] = old_env
    assert "PB[Player]" in goban.sgf_dump([], 9), "the default player name must be generic, not a person"

    # video: template text (language, suffixes, finding format) and scene drawing -- no audio/ffmpeg
    import narration, video
    narration.demo()
    video.demo()
    import theme
    print(theme.demo())
    print(legacy.demo())
    b = detection([["W", "B1"], ["B", "A1"]], "B", "A2", 9)
    assert {"kind": "one_liberty", "dimension": 1, "point": "A2"} in b and         {"kind": "capture", "point": "A2", "num": 1} in b, b

    assert goban.sgf_komi(goban.sgf_dump([], 9)) == 7.5
    assert goban.sgf_komi("(;GM[1]SZ[9]KM[7.0])") == 7.0, "old games use their own komi"
    owner = {"method": "ownership", "diff": 0.5}
    assert scoring.game_result(owner, -0.3) == (1.0, "counted", 0.5), "the count must override the estimate"
    assert scoring.game_result({"method": "approximate", "diff": 20}, -3.0)[:2] == (0.0, "estimate"), \
        "a count with dead stones not removed must not decide the winner"
    assert scoring.game_result({"method": "ownership", "diff": 0}, 5.0)[0] == 0.5
    assert scoring.game_result(owner, 9.0, resign=True)[0] == 0.0
    assert scoring.game_result(None, None) == (None, None, None)
    assert "Berabere" in scoring.result_sentence(*scoring.game_result(
        {"method": "ownership", "diff": 0}))
    assert "Beyaz (bot) 3.0" in scoring.result_sentence(*scoring.game_result(None, -3.0))
    assert "pes" in scoring.result_sentence(*scoring.game_result(resign=True))
    assert scoring.result_label(True) == "Kazandın", "old bool records must be readable"
    assert scoring.sp_change(0.5, 0) == 0.0, "a draw against an equal opponent does not change SP"
    assert scoring.sp_change(0.5, 4) > 0 > scoring.sp_change(0.5, -4)
    d4 = scoring.WEAKEST + 4
    assert scoring.placement_opponent([(d4, 0.5)]) == d4, "a draw pins the binary search to that point"
    # Streak: a draw neither extends nor resets it. The REAL profile is not touched.
    import tempfile
    real_profile = store.PROFILE
    try:
        store.PROFILE = os.path.join(tempfile.mkdtemp(), "profile.json")
        for S, run_ in ((1.0, 1), (0.5, 1), (1.0, 2), (0.0, 0)):
            stop, _ = store.apply_rank_result(9, S, scoring.WEAKEST)
            assert stop["streak"] == run_, (S, stop["streak"], run_)
    finally:
        store.PROFILE = real_profile

    # --- resolving the data folder (the installer made this necessary) ---
    import bundle
    # An unwritable path = under a FILE. System32 used to be used, but the CI runner
    # is an ADMINISTRATOR: it came out writable and the test failed. Creating a
    # folder under a file is impossible regardless of operating system or rights.
    _sandbox = tempfile.mkdtemp()
    _file = os.path.join(_sandbox, "file")
    open(_file, "w").close()
    forbidden = os.path.join(_file, "gomentor-nowrite")
    assert not bundle._writable(forbidden), "an unwritable folder was seen as writable"
    real = bundle.ROOT
    try:
        bundle.ROOT = forbidden                      # pretend to be "installed in Program Files"
        assert bundle.data_root() == bundle.user_data(), "an installed copy must fall back to the user folder"
    finally:
        bundle.ROOT = real
        shutil.rmtree(_sandbox, ignore_errors=True)
    assert bundle.data_root() == real, "a portable copy must write next to the app"
    # KataGo opens paths as ANSI on the C++ side: where the engine writes must be ANSI-safe
    assert bundle.non_ansi(bundle.engine_data_root()) is None, "the engine root is not ANSI-safe"

    # --- resolving external tools: next to the package -> PATH ---
    # On macOS katago and ffmpeg will come from Homebrew, the weights from the package.
    import shutil as _sh
    exists = os.path.join(os.environ.get("SystemRoot", "/"), "System32", "cmd.exe")
    if os.path.isfile(exists):
        assert bundle.tool(exists, "katago") == exists, "the binary next to the app must be used"
    none_ = os.path.join(bundle.ROOT, "_no_such_file")
    if _sh.which("ffmpeg"):
        assert bundle.tool(none_, "ffmpeg") == _sh.which("ffmpeg"), "must fall back to PATH"
    assert bundle.tool(none_, "_no_such_program_12345") == none_,         "with none found the given path must come back (for the error message)"

    # --- chart data: pure math, without starting Qt ---
    # charts.py only draws; the correctness of the numbers is tested HERE.
    records = [
        {"move_no": 1, "move": "E5", "points_lost": 0.0,
         "best_move": "F6", "next_score_lead": -0.2},
        {"move_no": 3, "move": "A1", "points_lost": 8.0,
         "best_move": "D5", "next_score_lead": -8.0},
        {"move_no": 5, "move": "B1", "points_lost": 6.0,
         "best_move": "D5", "next_score_lead": -14.0},
        {"move_no": 7, "move": "C1", "points_lost": 4.0,
         "best_move": "D5", "next_score_lead": -18.0},
        {"move_no": 9, "move": "pass", "points_lost": 0.0,
         "best_move": "pass", "next_score_lead": -18.0},
    ]
    bucket_ = scoring.distribution(records)
    assert len(bucket_) == 6, "distribution: one bucket per category"
    assert [len(x) for x in bucket_] == [2, 0, 0, 0, 1, 2], [len(x) for x in bucket_]
    assert bucket_[0][0]["move"] == "E5", "a move with no loss is in the Excellent bucket"
    assert bucket_[5][0]["move"] == "A1", "the bucket must hold the move itself"

    sr = scoring.runs(records)
    assert len(sr) == 1 and sr[0]["move"] == "D5", sr
    assert sr[0]["n"] == 3 and (sr[0]["start"], sr[0]["end"]) == (3, 7), sr[0]
    assert abs(sr[0]["lost"] - 18.0) < 1e-6, sr[0]["lost"]
    # 'pass' must not count toward a run, and the threshold must hold
    assert scoring.runs(records, at_least=4) == [], "no run above the threshold"
    played = [dict(r) for r in records]
    played[3]["move"] = "D5"                    # the asked-for move was played on move 7
    assert scoring.runs(played) == [], "the played move must not extend the run"
    br = scoring.brief("test", "ranked", 9, records)
    assert "**D5**: 3 kez (3–7) · 18.0p · oynadıkların: A1, B1, C1" in br, br
    # Bridge: an interruption (another best move) and a move with no loss neither split the run nor count.
    def rec(no, move, best_, lost):
        return {"move_no": no, "move": move, "best_move": best_,
                "points_lost": lost, "points": 5.0}
    bridge = [rec(1, "A1", "C3", 5.0), rec(3, "A2", "C3", 5.0), rec(5, "A3", "M9", 1.0),
             rec(7, "A4", "C3", 0.0), rec(9, "A5", "C3", 5.0)]
    s = scoring.runs(bridge)
    assert len(s) == 1 and s[0]["n"] == 3 and s[0]["interrupted"] == [5, 7], s
    assert abs(s[0]["lost"] - 15.0) < 1e-6, s[0]["lost"]
    far = bridge[:2] + [rec(5, "A3", "M9", 1), rec(7, "B1", "M9", 1),
                        rec(9, "B2", "M9", 1), rec(11, "A5", "C3", 5.0)]
    assert [x["n"] for x in scoring.runs(far, at_least=1) if x["move"] == "C3"] == [2, 1], \
        "an interruption longer than RUN_GAP must split the run"
    assert br.index("Kaçırılan kilit") < br.index("En pahalı"), "runs come before single moves"

    assert scoring.line_of("A1") == 1 and scoring.line_of("E5") == 5
    assert scoring.line_of("B2") == 2 and scoring.line_of("pass") is None
    me, engine_ = scoring.line_distribution(records)
    assert me[1] == 3 and me[5] == 1, me      # A1 B1 C1 on the edge, E5 tengen
    assert engine_ == {4: 4}, engine_                # F6 and D5 three times -- all on the 4th line
    assert sum(me.values()) == 4, "pass must not count"

    heat = scoring.heatmap(records)
    assert from_gtp("A1") in heat and abs(heat[from_gtp("A1")]["lost"] - 8.0) < 1e-6
    assert from_gtp("E5") in heat, "a move with no loss must be on the map too"
    assert sum(len(v["records"]) for v in heat.values()) == 4, "pass must not enter the map"

    sandbox = scoring.cumulative_loss(records)
    assert sandbox[0] == (1, 0.0) and sandbox[-1][0] == 9, sandbox
    assert abs(sandbox[-1][1] - 18.0) < 1e-6, sandbox[-1]
    assert all(b >= a for (_, a), (_, b) in zip(sandbox, sandbox[1:])), "cumulative cannot decrease"

    sv = scoring.level_series(records)
    assert len(sv) == len(records) and all(d is None for _, _, d in sv), \
        "without level_estimate the value must stay None"
    sv2 = scoring.level_series([{"move_no": 1, "level_estimate": {"label": "10k"}}])
    assert sv2[0][1] == "10k" and sv2[0][2] == scoring.rank_value("10k")

    self_ = scoring.game_summary("test", records)
    assert self_["n"] == 5 and self_["heavy"] == 3, self_      # 8.0 6.0 4.0 >= 3p
    assert abs(self_["lost"] - 18.0) < 1e-6 and abs(self_["mean_loss"] - 3.6) < 1e-6
    assert self_["good_ratio"] == 40 and self_["blunder_ratio"] == 40 and "mean_points" not in self_, self_
    assert scoring.game_summary("empty", []) is None
    tr = scoring.game_trend([("new", records), ("old", records)])
    assert [o["name"] for o in tr] == ["old", "new"], "the trend must go oldest to newest"
    assert scoring.game_trend([("empty", [])]) == [], "a game without records must not enter the trend"

    print("selftest ok")


def enginetest():
    """Really runs the engine."""
    import threading as th
    eng = Engine()
    errs, oks = [], []

    def w(i):
        try:
            if i % 3 == 0:
                grade(eng, [["B", "E5"], ["W", "C3"], ["B", "G7"]], "B", "15k", "9k")
            elif i % 3 == 1:
                eng.human_top([["B", "E5"]], "rank_15k")
            else:
                analyze_position(eng, [["B", "E5"], ["W", "C3"]], visits=100)
            oks.append(i)
        except Exception as e:
            errs.append(f"{i}: {type(e).__name__}: {e}")

    ts = [th.Thread(target=w, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs, f"concurrency broken: {errs}"
    assert len(oks) == 8
    print("enginetest ok (8/8 concurrent queries)")

    # The engine's OWN best move must lose ~0.
    base = [["B", "E5"], ["W", "C3"], ["B", "G7"], ["W", "G3"]]
    _, mi = analyze_position(eng, base)
    best_ = mi[0]["move"]
    f = grade(eng, base + [["B", best_]], "B", "15k", "9k")
    assert f["measurement"] == "single-search", f["measurement"]
    assert f["points_lost"] <= 0.2, f"best move {best_}: {f['points_lost']}p"
    assert "points" not in f and scoring.category(f) in ("excellent", "great"), f
    print(f"measurement test ok ({best_}: {scoring.category(f)}, -{f['points_lost']}p)")

    # The `before-after` fallback path: play a move the engine did not look at and
    # verify the measurement against an INDEPENDENT source -- a separate search
    # that spends full visits on that move via allowMoves. If the two disagree,
    # the fallback is broken.
    bad = next(m["move"] for m in reversed(mi) if m["move"] != best_)
    f2 = grade(eng, base + [["B", bad]], "B", "15k", "9k")
    if f2["measurement"] == "before-after":
        r = eng.query(moves=base, analyzeTurns=[len(base)], maxVisits=eng.cfg.visits,
                      allowMoves=[{"player": "B", "moves": [bad], "untilDepth": 1}])[0]
        zm = sorted(r["moveInfos"], key=lambda m: m["order"])[0]
        independent = max(0.0, mi[0]["scoreLead"] - zm["scoreLead"])
        diff = abs(independent - f2["points_lost"])
        assert diff <= 1.5, (f"{bad}: before-after {f2['points_lost']}p vs "
                             f"independent {independent:.2f}p (diff {diff:.2f}p)")
        print(f"fallback ok ({bad}: before-after {f2['points_lost']}p ~ "
              f"independent {independent:.1f}p, {zm['visits']}v)")
    else:
        print(f"fallback test skipped ({bad} in the tree, {f2['measurement']})")

    # level curve: the stronger level's expected loss must be smaller than the weaker one's
    curve = dict(f["level_curve"])
    assert len(curve) >= 3, curve
    assert curve["20k"] > curve["1d"], f"ladder inverted: {curve}"
    print(f"level curve ok: {f['level_curve']}")
    # An estimate MAY NOT be produced: if the ladder's ends do not separate by
    # MIN_SEPARATION, the position does not measure level and `level_estimate`
    # returns None. That is correct behavior -- the test cannot treat it as a failure.
    t = f["level_estimate"]
    print(f"estimate: ~{t['label']}" if t else
          "estimate: none (narrow ladder -- the position does not tell levels apart)"
          + f"  ·  category {scoring.category(f)}")

    # --- the size comes from the query, not the engine setting: 13x13 to an engine set to 9x9 ---
    assert eng.cfg.size != 13
    r, mi13 = analyze_position(eng, [["B", "K10"]], visits=20, size=13)
    assert len(r["ownership"]) == 169 and mi13, "the 13x13 query went out as 9x9"

    # --- handicap + white user: SGF import end to end ---
    # Setup stones must go to the query as initialStones and the turn number must
    # not shift. If it shifted, the engine's own best move would not lose 0.
    import tempfile, time, sgf_import
    setup_ = [["AB", "G7"], ["AB", "C3"]]
    game_, b = list(setup_), replay(setup_, 9)
    for t in range(24):                              # with handicap White starts
        col = "W" if t % 2 == 0 else "B"
        for mv in eng.human_sample(game_, "rank_10k", k=6):
            if mv != "pass" and b.play(from_gtp(mv, 9), 1 if col == "B" else -1):
                game_.append([col, mv])
                break
        else:
            break
    b0 = analyze_position(eng, setup_ + game_[2:3], visits=20)[0]["ownership"]
    b1 = analyze_position(eng, game_[2:3], visits=20)[0]["ownership"]
    assert b0 != b1, "the setup stones did not reach the engine"
    _, mi = analyze_position(eng, game_[:9])           # White to move
    f = grade(eng, game_[:9] + [["W", mi[0]["move"]]], "W", "15k", "9k")
    assert f["points_lost"] <= 0.3, f"best move in the handicap position {f['points_lost']}p"

    real = store.ROOT
    store.ROOT = tempfile.mkdtemp()
    try:
        text_ = goban.sgf_dump(game_, 9, 0.5, info={"PB": "opponent", "PW": "me", "RE": "W+2.5"})
        m = sgf_import.create(text_, "W")
        t0 = time.time()
        sgf_import.grade_moves(eng, m, "15k", "9k")
        white = [n for n, (c, mv) in enumerate(game_, 1) if c == "W" and mv != "pass"]
        m2 = store.load_game(m.name)
        assert [r["move_no"] for r in m2.valid_records] == white
        assert all(r["mover"] == "W" for r in m2.valid_records)
        assert m2.color == "W" and m2.moves == game_ and m2.komi == 0.5
        assert m2.result["won"] == 1.0 and m2.result["score"] == 2.5, m2.result
        assert not sgf_import.missing(m2), "nothing unfinished, resume must be empty"
        print(f"import ok (2 handicap, white, {len(white)} moves, "
              f"{time.time() - t0:.1f} s = {(time.time() - t0) / len(white):.2f} s/move)")
    finally:
        store.ROOT = real


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--rank", default="15k")
    ap.add_argument("--bot", default="15k")
    ap.add_argument("--next", default="9k")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--enginetest", action="store_true", help="runs the engine, slow")
    ap.add_argument("--import-sgf", metavar="SGF", help="import an outside SGF into the archive and grade it")
    ap.add_argument("--color", choices=("B", "W"), default="B",
                    help="--import-sgf: your color")
    ap.add_argument("--uitest", action="store_true",
                    help="build the UI without a window and run the archive flows")
    ap.add_argument("--pattern", action="store_true",
                    help="cross-game mistake pattern (all saved games)")
    ap.add_argument("--big-points", action="store_true",
                    help="measure the big-point thresholds over the saved games")
    ap.add_argument("--brief", metavar="GAME_NAME",
                    help="make the analysis brief (no engine, from saved data)")
    ap.add_argument("--reset-rank", metavar="SIZE", type=int, nargs="?", const=9,
                    help="reset the rank/SP state of that board size (placement starts over)")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # a folder name with emoji broke cp1254
    legacy.migrate(store.ROOT)   # data saved under the old name
    store.arrange()          # move the old flat layout into subfolders
    ap.add_argument("--size", type=int, choices=(9, 13, 19),
                    help="board size (default 9)")
    a = ap.parse_args()
    if a.size:
        os.environ["GOMENTOR_SIZE"] = str(a.size)    # Config.env() reads it from here
    if a.selftest:
        selftest()
    elif a.enginetest:
        enginetest()
    elif a.import_sgf:
        import_cli(a.import_sgf, a.color, a.rank, a.next)
    elif a.uitest:
        uitest()
    elif a.pattern:
        path_, text_ = make_pattern()
        print(text_)
        print(path_)
    elif a.big_points:
        scan_big_points()
    elif a.brief:
        print(make_brief(a.brief))
    elif a.reset_rank:
        size_ = a.reset_rank
        p = store.load_profile()
        old = p.pop(f"{size_}x{size_}", None)
        store.save_profile(p)
        print(f"{size_}x{size_} rank state reset. Before: {old}")
        print("Game records were NOT deleted -- only the profile was reset.")
    else:
        play_cli(a.rank, a.bot, a.next)
