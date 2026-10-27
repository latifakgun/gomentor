## GoMentor

Rank-conditioned Go coach & analyst. Inference runs on **your** GPU; nothing
is uploaded. / Dereceye göre konuşan Go hocası. Çıkarım **senin** GPU'nda,
hiçbir şey sunucuya gitmiyor.

### Downloads / İndirme
- **Windows** — `GoMentor-setup-<version>.exe`. KataGo included. For the analysis
  video install FFmpeg separately: `winget install Gyan.FFmpeg` (playing, analysis
  and problems work without it). / KataGo dahil. Analiz videosu için FFmpeg'i
  ayrıca kur.
- **macOS (Apple Silicon)** — `GoMentor-<version>.dmg`. First `brew install katago ffmpeg`
  (KataGo has no official macOS binary; Homebrew's is used). / Önce
  `brew install katago ffmpeg`.

### New in 0.4.0 / Yenilikler
- **go-hoca is now GoMentor.** On first start your games, ranks and review cards
  are imported from the old `maclar/` folder automatically; the old folder is
  left untouched. Installing over an existing go-hoca install upgrades it. /
  **go-hoca artık GoMentor.** İlk açılışta maçların, rankların ve tekrar kartların
  eski `maclar/` klasöründen kendiliğinden aktarılır; eski klasöre dokunulmaz.
- The source code is in English (file names, identifiers, comments); the app
  itself is still fully Turkish and English. / Kaynak kod İngilizce; uygulama
  yine tamamen Türkçe ve İngilizce.
- Bundled tsumego descriptions are in English. / Paketle gelen tsumego
  açıklamaları İngilizce.

### Known limits / Bilinen sınırlar
- Unsigned: on macOS **right click → Open** the first time, SmartScreen warning on
  Windows. / İmzalanmadı: macOS'ta ilk açılışta **sağ tık → Aç**, Windows'ta
  SmartScreen uyarısı.
- The app path must be ASCII. / Uygulama yolu ASCII olmalı.
- Level estimate is off on 13x13 and 19x19 (calibration D=730 / D=460). / Seviye
  tahmini 13x13 ve 19x19'da kapalı.
- The analysis video uses Microsoft Edge's online voices (edge-tts) and needs an
  internet connection. / Analiz videosu Microsoft Edge'in çevrimiçi seslerini
  kullanır, internet gerekir.

### License / Lisans
GoMentor's own code is **MIT** (`LICENSE`). The package carries third-party
components; see `THIRD-PARTY-NOTICES.md` and `LICENSES/`.

**FFmpeg:** `ffmpeg.exe` is not bundled; install it yourself for the analysis
video. The FFmpeg libraries inside PySide6 (7.1.1, LGPL v2.1+) are listed in
`THIRD-PARTY-NOTICES.md` with their source. / `ffmpeg.exe` pakette değil;
PySide6'nın içindeki FFmpeg kütüphaneleri (LGPL) bildirimde.
