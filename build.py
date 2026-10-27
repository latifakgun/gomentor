#!/usr/bin/env python3
"""Builds the Windows .exe (PyInstaller, one folder) and gathers what goes next to it.

  python build.py                   app + katago + existing games
  python build.py --empty           empty data folder (a build to give away)
  python build.py --license-audit   notice audit only (no build)

The build runs from a CLEAN venv:
    python -m venv .venv-build
    .venv-build/Scripts/python -m pip install pillow edge-tts==7.3.1 pyinstaller PySide6==6.9.2

Anaconda packages (Pillow, aiohttp) keep their DLLs under `Library/bin`;
PyInstaller does not look there, so the package would not even start ("DLL
load failed while importing _imaging"). pip wheels carry the DLL INSIDE the
package.

The PySide6 version is PINNED (6.9.2): 6.11.2 does not load at all on the
development machine ("DLL load failed while importing QtCore"). We know what
works and pin to it.

Why NOT one file (--onefile): the package is ~530 MB and the one-file build
unpacks all of it to a temp folder on every start. The one-folder build
starts instantly.

Why katago is not handed to PyInstaller: re-collecting 369 MB on every build
takes minutes. Copying it after the build takes seconds.
"""
import os
import re
import shutil
import subprocess
import sys
import time

import engine, bundle

GAME = sys.platform == "darwin"
ROOT = os.path.dirname(os.path.abspath(__file__))
# The build runs from a CLEAN venv (reason above) -- locally and in CI alike.
# Assuming "the runner's python is already a clean pip python" was WRONG:
# GitHub's Windows image ships with pipx and its dependencies preinstalled,
# and since the license audit asks the interpreter for its distribution list,
# it took them for part of the project and stopped the packaging.
_VENV_SUB = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
VENV = os.path.join(ROOT, ".venv-build", *_VENV_SUB)
if not os.path.isfile(VENV):
    VENV = sys.executable
# On macOS PyInstaller produces an `.app` BUNDLE; bundled assets go NEXT TO
# the executable, i.e. under Contents/MacOS -- which is where `bundle.ROOT`
# points (the folder of sys.executable).
DIST = os.path.join(ROOT, "dist", "GoMentor.app" if GAME else "GoMentor")
CONTENTS = os.path.join(DIST, "Contents", "MacOS") if GAME else DIST
KATAGO = engine.katago_root()          # same search order as the running app
# Output files in the KataGo folder must not enter the package -- EXCEPT
# KataGoData: that is the OpenCL tuning cache, not output. Without it the
# package spent 3+ minutes tuning the GPU on first start (13 s with the
# cache). A user on the same GPU skips that wait entirely; a different GPU
# still tunes (the file name carries the GPU), which cannot be avoided.
KATAGO_SKIP = shutil.ignore_patterns("logs", "analysis_logs", "gtp_logs",
                                     "*.log", "*.tmp")

# ---------- license notice ----------
# Every third-party component that enters the package MUST have an entry in
# THIRD-PARTY-NOTICES.md (Qt, Qt's FFmpeg DLLs and edge-tts are LGPL; the
# rest are permissive, but every one of them requires the copyright notice).
# The audit is tied to the build: when a new pip dependency is added, no
# package can be made until its notice is written. In a file like this
# "we'll write it later" turns into "we never wrote it".
LICENSE_TEXT = os.path.join(ROOT, "LICENSE")
NOTICE = os.path.join(ROOT, "THIRD-PARTY-NOTICES.md")
LICENSE_DIR = os.path.join(ROOT, "LICENSES")
# Build tools only, not in the package. PyInstaller is GPLv2+, but its
# bootloader exception allows distributing the packaged app under any license.
# macholib is installed ONLY on macOS (PyInstaller fixes dylib paths with it;
# not needed at run time) -- its dependency altgraph is already here. Since the
# audit had only ever run on Windows this was missed and stopped the macOS job
# at the gate.
BUILD_ONLY = {"pyinstaller", "pyinstaller-hooks-contrib", "altgraph", "pefile",
                  "pywin32-ctypes", "pywin32_ctypes", "pip", "setuptools", "wheel",
                  "packaging", "macholib"}
# Parts that pip does not show but that enter the package.
NATIVE = ("FFmpeg", "KataGo", "PySide6", "shiboken6", "Python")


def _venv_packages():
    """Distribution names in the build venv (the libraries that enter the package)."""
    r = subprocess.run(
        [VENV, "-c", "import importlib.metadata as m;"
                     "print('\\n'.join(sorted({d.metadata['Name'] or '' "
                     "for d in m.distributions()})))"],
        capture_output=True, text=True)
    return [x.strip() for x in r.stdout.splitlines() if x.strip()]


def license_audit():
    """Returns missing/empty notices. Empty list = clean."""
    problem_ = []
    for y in (LICENSE_TEXT, NOTICE):
        if not os.path.isfile(y):
            problem_.append(f"missing: {os.path.basename(y)}")
    if problem_:
        return problem_
    text_ = open(NOTICE, encoding="utf-8").read()

    for name in _venv_packages():
        if name.lower().replace("_", "-") in BUILD_ONLY:
            continue
        if name.lower() not in text_.lower():
            problem_.append(f"not named in the notice: {name}")
    for name in NATIVE:
        if name.lower() not in text_.lower():
            problem_.append(f"not named in the notice: {name}")

    # Does every text file the notice points to exist, and is there a file left in
    # LICENSES/ that the notice never mentions (i.e. no longer needed, or
    # forgotten) -- both directions, or the file quietly goes stale.
    mentioned = set(re.findall(r"LICENSES/([\w.+-]+\.txt)", text_))
    for f in sorted(mentioned):
        y = os.path.join(LICENSE_DIR, f)
        if not os.path.isfile(y):
            problem_.append(f"notice present, text missing: LICENSES/{f}")
        elif os.path.getsize(y) < 200:
            problem_.append(f"license text too short (empty?): LICENSES/{f}")
    for f in sorted(os.listdir(LICENSE_DIR) if os.path.isdir(LICENSE_DIR) else []):
        if f.endswith(".txt") and f not in mentioned:
            problem_.append(f"text present, not mentioned in the notice: LICENSES/{f}")
    return problem_


def copy_licenses():
    """LICENSE + notice + LICENSES/ into the package root. They have to sit next
    to the distributed binary; being in the repository is not enough."""
    shutil.copy2(LICENSE_TEXT, os.path.join(CONTENTS, "LICENSE.txt"))
    shutil.copy2(NOTICE, os.path.join(CONTENTS, "THIRD-PARTY-NOTICES.md"))
    goal = os.path.join(CONTENTS, "LICENSES")
    shutil.rmtree(goal, ignore_errors=True)
    shutil.copytree(LICENSE_DIR, goal)

# Only these. Handing over the WHOLE folder was tried and measured: the
# `webview` plugin dragged in the 195 MB Qt6WebEngineCore.dll, the others
# Quick/Qml/Multimedia/Pdf, and the package went from 535 to 817 MB.
# PyInstaller collects the plugins' binary dependencies too; a desktop widget
# app uses none of them.
QT_PLUGINS = ("platforms",            # REQUIRED: without qwindows.dll it does not even open
              "styles",               # native look
              "imageformats",         # png/jpg
              "iconengines",
              "platforminputcontexts",
              "multimedia")           # in-app video player (ffmpeg backend)


def qt_plugins():
    """(source, target) pairs: the PySide6 plugins.

    PyInstaller's PySide6 hook does NOT collect these on Windows; the package
    would not even open, saying "Could not find the Qt platform plugin windows".

    The wheel layout differs by platform: `PySide6/plugins` on Windows,
    `PySide6/Qt/plugins` on macOS/Linux. Both are checked, and the target path
    is derived from the source -- Qt looks for plugins where it expects them.

    The path is asked of the BUILD VENV; the running python's PySide6 may be a
    different version.
    """
    code = ("import PySide6,os;d=os.path.dirname(PySide6.__file__);"
           "p=next((x for x in (os.path.join(d,'plugins'),"
           "os.path.join(d,'Qt','plugins')) if os.path.isdir(x)),'');"
           "print(d);print(p)")
    r = subprocess.run([VENV, "-c", code], capture_output=True, text=True,
                       encoding="utf-8", check=True,
                       # The child python tries to write the path as cp1254 and fails.
                       env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    row_ = [x.strip() for x in r.stdout.splitlines() if x.strip()]
    if len(row_) < 2 or not row_[1]:
        return []
    pkg, root_ = row_
    upper = os.path.relpath(root_, os.path.dirname(pkg))   # PySide6/plugins or PySide6/Qt/plugins
    return [(os.path.join(root_, name), os.path.join(upper, name))
            for name in QT_PLUGINS if os.path.isdir(os.path.join(root_, name))]


def measure(path_):
    if os.path.isfile(path_):
        return os.path.getsize(path_)
    return sum(os.path.getsize(os.path.join(k, f))
               for k, _, files in os.walk(path_) for f in files)


def mb(n):
    return f"{n / 1024 / 1024:.0f} MB"


def make_dmg():
    """Makes a distributable .dmg from the `.app`.

    `hdiutil` is macOS's own tool -- no dependency like create-dmg. An
    /Applications shortcut goes inside: that is how installing works on a Mac,
    the user drags the app across. The Windows "choose install location"
    screen has no macOS counterpart, and none is needed.
    """
    version = os.environ.get("GOMENTOR_VERSION", "0.1.0")
    scene = os.path.join(ROOT, "dist", "_dmg")
    output = os.path.join(ROOT, "dist", f"GoMentor-{version}.dmg")
    shutil.rmtree(scene, ignore_errors=True)
    os.makedirs(scene)
    shutil.copytree(DIST, os.path.join(scene, "GoMentor.app"), symlinks=True)
    os.symlink("/Applications", os.path.join(scene, "Applications"))
    if os.path.exists(output):
        os.remove(output)
    subprocess.run(["hdiutil", "create", "-volname", "GoMentor",
                    "-srcfolder", scene, "-ov", "-format", "UDZO", output],
                   check=True)
    shutil.rmtree(scene, ignore_errors=True)
    return f"  {'DMG':14} {mb(measure(output)):>8}  {output}"


def main():
    empty = "--empty" in sys.argv
    t0 = time.time()

    # License audit FIRST: saying "notice missing" at the end of a 10-minute
    # build is pointless.
    problem_ = license_audit()
    if problem_:
        sys.exit("License notice incomplete — update THIRD-PARTY-NOTICES.md:"
                 + os.linesep + os.linesep.join("  - " + s for s in problem_))

    if not os.path.isdir(KATAGO):
        sys.exit(f"no katago folder: {KATAGO}  (set it with GOMENTOR_KATAGO_DIR)")
    # Building with Anaconda's python is DLL hell (see above). A clean python or
    # venv is required; in CI the runner is clean already.
    if VENV == sys.executable and "anaconda" in sys.prefix.lower():
        sys.exit("Does not build with Anaconda's python — run the two commands at the top of this file.")

    added = qt_plugins()
    if not added:
        # On Windows we MEASURED what happens without these: the package does not
        # even open. On macOS PyInstaller's hook may handle it -- we have no proof,
        # so there we warn and carry on, on Windows we stop.
        if GAME:
            print("WARNING: Qt plugins not found, relying on PyInstaller's hook")
        else:
            sys.exit("PySide6 plugins not found (is PySide6 installed in the venv?)")

    print(f"1/4  PyInstaller... ({len(added)} Qt plugin folders)")
    command = [VENV, "-m", "PyInstaller", "--noconfirm", "--onedir",
             # Console OFF. The price: frozen, ui.py ties stdout/stderr to
             # games/log/app.log, otherwise a startup error vanishes without a trace.
             "--windowed", "--name", "GoMentor",
             "--collect-submodules", "edge_tts",
             "--exclude-module", "tkinter"]      # no longer used at all
    if GAME:
        command += ["--osx-bundle-identifier", "com.latifakgun.gomentor"]
    added = list(added) + [(os.path.join(ROOT, "sounds"), "sounds"),        # stone sounds + their license
                       (os.path.join(ROOT, "tsumego"), "tsumego")]  # basic problem set
    for source, goal in added:
        command += ["--add-data", source + os.pathsep + goal]
    command.append(os.path.join(ROOT, "ui.py"))
    subprocess.run(command, check=True, cwd=ROOT)

    # PyInstaller DELETES and rebuilds dist/GoMentor on every build, so katago
    # has to be copied again every time. There is no "just compile the code"
    # shortcut; the copy takes half a minute anyway.
    goal_katago = os.path.join(CONTENTS, "katago")
    if GAME:
        # On macOS the BINARY does not go into the package: KataGo has no official
        # macOS release and brew's binary depends on /opt/homebrew dylibs. The user
        # runs `brew install katago ffmpeg` and the app finds it on PATH. Weights and
        # cfg are platform independent, so they go into the package.
        print("2/3  weights + cfg (NO binary, it will come from brew)...")
        os.makedirs(goal_katago, exist_ok=True)
        for f in os.listdir(KATAGO):
            if f.endswith(".bin.gz") or f.endswith(".cfg"):
                shutil.copy2(os.path.join(KATAGO, f), os.path.join(goal_katago, f))
        print("3/3  data folder skipped (nothing is written inside the .app)")
    else:
        print("2/3  katago...")
        shutil.copytree(KATAGO, goal_katago, ignore=KATAGO_SKIP)

        # ffmpeg.exe is NOT in the package (2026-10-24): the gyan.dev build statically
        # embeds ~50 libraries and its build script is not published, so the full
        # source GPLv3 asks for could not be provided. The user installs it with
        # winget; only the analysis video needs it, and video.has_ffmpeg() says so.
        print("3/3  data folder...")
        data = os.path.join(DIST, "games")
        shutil.rmtree(data, ignore_errors=True)
        source = os.path.join(ROOT, "games")
        if not empty and os.path.isdir(source):
            shutil.copytree(source, data)
        else:
            os.makedirs(data, exist_ok=True)

    copy_licenses()

    print()
    for name in ("GoMentor.exe", "_internal", "katago", "games"):
        y = os.path.join(CONTENTS, name)
        print(f"  {name:14} {mb(measure(y)) if os.path.exists(y) else '-':>8}")
    print(f"  {'TOTAL':14} {mb(measure(DIST)):>8}")
    if GAME:
        print(make_dmg())
    print(f"{os.linesep}{time.time() - t0:.0f} s — {DIST}")
    if bundle.non_ansi(DIST):
        print(os.linesep + "WARNING: this path cannot be converted to ANSI, KataGo cannot open it."
              + os.linesep
              + "  copy dist/GoMentor to an ASCII folder and run it from there.")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--license-audit" in sys.argv:
        s = license_audit()
        print(os.linesep.join("  - " + x for x in s) if s else "license audit ok")
        sys.exit(1 if s else 0)
    main()
