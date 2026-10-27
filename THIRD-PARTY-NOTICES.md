# Third-party notices

GoMentor's own source code is MIT licensed (see `LICENSE`). The **distributed
application** additionally ships the components below, each under its own
license. Full texts are in `LICENSES/`.

Two of these licenses place obligations on whoever redistributes the app, so
they are called out first.

---

## FFmpeg libraries inside PySide6 — GNU LGPL v2.1 or later

**Bundled as:** `avcodec-61.dll`, `avformat-61.dll`, `avutil-59.dll`,
`swresample-5.dll`, `swscale-8.dll` under `_internal/PySide6/`, loaded by Qt
Multimedia's FFmpeg backend (`plugins/multimedia/ffmpegmediaplugin.dll`) for
the in-app video player. They come unmodified inside the `PySide6_Addons`
6.9.2 wheel; GoMentor does not build or pick them.
**Build:** FFmpeg `7.1.1`, shared, built by The Qt Company *without*
`--enable-gpl` / `--enable-nonfree` (`libavutil license: LGPL version 2.1 or later`).
**Modified:** no.
**License text:** LGPL-2.1-or-later; the "or later" option is taken, so the
texts are `LICENSES/LGPL-3.0.txt` and the GPL it extends, `LICENSES/GPL-3.0.txt`.
**Source:** `https://github.com/FFmpeg/FFmpeg` tag `n7.1.1`; Qt's build
configuration is described in the Qt Multimedia documentation
(`https://doc.qt.io/qt-6/qtmultimedia-attribution-ffmpeg.html`).
They are separate, replaceable DLLs, like the rest of Qt.

### The `ffmpeg.exe` program is NOT bundled

The analysis video runs the `ffmpeg` program as a separate process
(`subprocess.run` in `video.py`, only to read a duration and to mux frames
with audio). Up to v0.2.3 the Windows package shipped gyan.dev's static GPLv3
build next to the app. From v0.3.0 it does not: that build statically links
about fifty libraries and its build scripts are not published, so the full
Corresponding Source GPLv3 asks for could not be offered. Users install FFmpeg
themselves (`winget install Gyan.FFmpeg`, or `brew install ffmpeg` on macOS)
and GoMentor finds it on `PATH`. Nothing else in the app needs it.

## Qt / PySide6 / shiboken6 6.9.2 — GNU LGPL v3

Distributed as four packages, all 6.9.2 and all under the same licence:
`PySide6`, `PySide6_Essentials`, `PySide6_Addons` and `shiboken6`.

**Bundled as:** `Qt6*.dll`, `PySide6/`, `shiboken6/` under `_internal/`.
**Modified:** no.
**License text:** `LICENSES/LGPL-3.0.txt` (LGPLv3 extends `GPL-3.0.txt`;
Qt is also available under a separate commercial license, not used here).
**Source:** `https://download.qt.io/official_releases/QtForPython/` (PySide6
6.9.2) and `https://download.qt.io/official_releases/qt/6.9/6.9.2/single/`
(Qt 6.9.2).

The LGPL is satisfied here because the Qt libraries ship as **separate,
replaceable DLLs** in `_internal/`: a user may drop in their own build of the
same Qt version. GoMentor's own code stays MIT, which permits the modification
and reverse engineering that LGPLv3 §4 requires of the combined work.

## edge-tts 7.3.1 — GNU LGPL v3

**Bundled as:** pure Python inside the executable's archive.
**Modified:** no. Imported as a library (`import edge_tts` in `video.py`).
**License text:** `LICENSES/edge-tts.txt` (LGPLv3, plus MIT for one file,
`srt_composer.py`). Full LGPLv3 text: `LICENSES/LGPL-3.0.txt`.
**Source:** `https://github.com/rany2/edge-tts` release `7.3.1` (also on PyPI).

---

## KataGo v1.18.1 and its neural networks — MIT

**Bundled as:** `katago/` next to the application (`katago.exe`,
`kata1.bin.gz`, `humanv0.bin.gz`, configs and their support DLLs).
**Modified:** no. Run as a separate process (`engine.py`).
**License text:** `LICENSES/KataGo-MIT.txt`
**Source:** `https://github.com/lightvector/KataGo` tag `v1.18.1`

The KataGo distribution carries its own bundled libraries (OpenSSL
`libcrypto-3`/`libssl-3`, zlib, bzip2, libzip and the externals listed at the
top of its LICENSE). Those come from upstream as-is; their notices live in
KataGo's own `cpp/external` subdirectories.

**On the networks:** `kata1-b28c512nbt` is released under the same MIT terms.
`b18c384nbt-humanv0` is MIT licensed too, but **the provenance of its training
data is not documented upstream.** GoMentor's second product thesis ("the move
one rank above you") depends on that network, so this is stated openly rather
than buried.

## Stone sounds — MIT

From [KaTrain](https://github.com/sanderland/katrain), recorded by Jordan
Seaward. Text: `sounds/LICENSE.txt`, copied to `LICENSES/KaTrain-sounds-MIT.txt`.

## Python runtime and standard library — PSF License

CPython 3.12. Text: `LICENSES/Python-PSF.txt`

## Python libraries

All shipped unmodified, all permissive:

| component | version | license | text |
|---|---|---|---|
| Pillow | 12.3.0 | MIT-CMU (HPND) | `LICENSES/Pillow.txt` |
| aiohttp | 3.14.3 | Apache-2.0 (and MIT) | `LICENSES/aiohttp.txt` |
| aiosignal | 1.4.0 | Apache-2.0 | `LICENSES/aiosignal.txt` |
| aiohappyeyeballs | 2.7.1 | PSF-2.0 | `LICENSES/aiohappyeyeballs.txt` |
| attrs | 26.1.0 | MIT | `LICENSES/attrs.txt` |
| certifi | 2026.7.22 | MPL-2.0 | `LICENSES/certifi-MPL-2.0.txt` |
| frozenlist | 1.8.0 | Apache-2.0 | `LICENSES/frozenlist.txt` |
| idna | 3.19 | BSD-3-Clause | `LICENSES/idna.txt` |
| multidict | 6.7.1 | Apache-2.0 | `LICENSES/multidict.txt` |
| propcache | 0.5.2 | Apache-2.0 | `LICENSES/propcache.txt` |
| yarl | 1.24.5 | Apache-2.0 | `LICENSES/yarl.txt` |
| typing_extensions | 4.16.0 | PSF-2.0 | `LICENSES/typing_extensions.txt` |
| tabulate | 0.10.0 | MIT | `LICENSES/tabulate.txt` |

`certifi` is MPL-2.0: it ships unmodified as its own file (`cacert.pem`), which
is all the MPL asks for — if you patch it, that file's source must be published.

## Microsoft Visual C++ runtime

`MSVCP140*.dll`, `VCRUNTIME140*.dll` and the `api-ms-win-*` stubs are
redistributed under Microsoft's redistributable terms for the Visual C++
runtime. Not modified.

## Build-time only (not shipped)

PyInstaller 6.22.2 is GPLv2-or-later **with the bootloader exception**, which
explicitly permits distributing the packaged application under any license of
your choosing. It is a build tool and no part of it is redistributed in a way
that affects GoMentor's license.

---

## How this was checked

The list is not from memory. It was produced from what the built package
actually contains: `dist/GoMentor/_internal/`, the PyInstaller archive TOC
(`build/GoMentor/PYZ-00.toc`), distribution metadata in the build venv, and
`ffmpeg.exe -version` for the FFmpeg configure flags. The GPLv3 and LGPLv3
texts are the canonical FSF files, verified by MD5
(`d32239bcb673463ab874e80d47fae504` and `e6a600fd5e1d9cbde2d983680233ad02`).
`python build.py --license-audit` re-runs the check and fails if a bundled
component has no notice here.

This file records licences and obligations; it is not legal advice.
