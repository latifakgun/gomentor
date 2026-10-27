#!/usr/bin/env python3
"""The ONLY difference between running frozen (.exe) and running from source lives here.

From source, everything is in the project folder and `__file__` points to the
right place. Packaged with PyInstaller, the code goes under `_internal/`, while
the data folder, katago and ffmpeg sit NEXT TO the .exe. `__file__` does not
look there.

Three modules (store, engine, video) need to know this. Rather than copy two
lines into each, it lives in one place: if packaging behavior changes, this is
the file that changes.
"""
import os
import shutil
import sys

FROZEN = getattr(sys, "frozen", False)

#: folder holding the .exe (frozen) or the project folder (source)
ROOT = os.path.dirname(os.path.abspath(sys.executable if FROZEN else __file__))


def beside(*piece):
    """Path next to the app: games/, katago/ ..."""
    return os.path.join(ROOT, *piece)


def tool(path_, name):
    """External tool: the given path first, then PATH.

    `katago.exe` is bundled into the Windows package; ffmpeg is in no package,
    the user installs it (winget / brew) and it is found on PATH. On macOS
    KataGo also comes from Homebrew -- KataGo has NO official macOS binary
    (releases are Windows/Linux only) and brew's binary links against dylibs
    under /opt/homebrew, so it cannot be bundled.

    The weights are platform independent and ship in the package. So on a Mac
    the binary comes from one place and the weights from another; this function
    hides that split from the user entirely.

    If nothing is found, the given path is returned -- so the error message
    names the missing file.
    """
    if os.path.isfile(path_):
        return path_
    return shutil.which(name) or path_


def _writable(d):
    """Can the folder really be written to -- found out by TRYING, not by checking permissions.

    On Windows ACLs lie and `os.access` is unreliable. The only honest test is
    to try writing.
    """
    try:
        os.makedirs(d, exist_ok=True)
        attempt = os.path.join(d, ".write_test")
        with open(attempt, "w") as f:
            f.write("x")
        os.remove(attempt)
        return True
    except OSError:
        return False


def user_data():
    """Where the operating system wants per-user data."""
    if sys.platform == "win32":
        root_ = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        root_ = os.path.expanduser("~/Library/Application Support")
    else:
        root_ = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(root_, "GoMentor")


def data_root():
    """Writable data root. games/, KataGo logs and the tuning cache go here.

    The installer forced this: it puts the app under `C:/Program Files` or
    `/Applications`, and the user CANNOT write there. Writing next to the app is
    right for a portable folder and a disaster for an installed one:
      - games cannot be saved,
      - KataGo cannot write its tuning cache and spends 3 minutes tuning on
        EVERY start.

    The rule is simple and involves no guessing: next to the app if we can
    write there, otherwise the user folder.

    macOS exception: frozen, the app is an `.app` BUNDLE and writing inside it
    breaks convention -- while it sits in Downloads it is technically writable,
    but writing there is wrong. On macOS it is always the user folder.
    """
    if FROZEN and sys.platform == "darwin":
        return user_data()
    return ROOT if _writable(ROOT) else user_data()


def engine_data_root():
    """Root for what KataGo WRITES (logs + tuning cache).

    Separate from data_root() because the constraint differs: Python writes the
    games and a path with emoji is fine for it; KataGo writes the logs and opens
    the path as ANSI on the C++ side. In a folder with emoji KataGo dies with
    "Error creating directory". So everything the engine writes goes somewhere
    ANSI-safe.
    """
    k = data_root()
    return k if not non_ansi(k) else user_data()


def non_ansi(path_=None):
    """Returns the path if it cannot be converted to the Windows ANSI code page, else None.

    KataGo is a C++ program and opens file paths as ANSI. If the app sits in a
    folder with emoji (for example `🎯 Projects/`), KataGo dies saying
    "Could not open file ... does not exist" -- while the file is right there.
    Because the Python side works fine, the error shows up somewhere else
    entirely. It happened once; it should not happen silently again.
    """
    if os.name != "nt":
        return None
    path_ = path_ or ROOT
    try:
        path_.encode("mbcs")
    except UnicodeEncodeError:
        return path_
    return None


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(f"frozen: {FROZEN}")
    print(f"root  : {ROOT}")
    print(f"data  : {data_root()}")
    print(f"user folder: {user_data()}")
    for name in ("games", "katago", "ffmpeg.exe"):
        print(f"  {name:12} {'ok' if os.path.exists(beside(name)) else 'missing'}  {beside(name)}")
