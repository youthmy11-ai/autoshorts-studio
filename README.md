# AutoShorts Studio

**Batch deduplicated short-form video assembly & beat-milestone audio slicing toolkit for Windows, macOS, and Linux.**

AutoShorts Studio combines a content-addressed SQLite WAL media deduplication engine, a beat-milestone audio slicer, and a multi-aspect FFmpeg rendering pipeline (`9:16` Shorts/Reels/TikTok, `16:9` YouTube, and `1:1` Square) into one standalone CLI and dark-slate Tkinter desktop application.

---

## Key Features

- **SHA-256 Deduplication (`SQLite WAL`)**: Computes streaming SHA-256 digests of every video and image source, recording processed hashes and rendered outputs atomically so the same source clip is never reused across batches.
- **Beat-Milestone Audio & Montage Sync**: Parses `MM:SS`, `HH:MM:SS`, seconds, or cumulative duration lists (e.g. `0:15, 0:30, 1:02`), aligns visual clips to beat windows, and slices audio cleanly with fade-in/fade-out envelopes.
- **Multi-Aspect FFmpeg Pipeline**:
  - `9:16` (`1080x1920`) with blurred ambient pillarbox OR smart center-crop fill
  - `16:9` (`1920x1080`) YouTube widescreen
  - `1:1` (`1080x1080`) Square social format
  - Smooth Ken Burns zoom-pan for static images
  - Optional EBU R128 loudness normalization (`-14 LUFS`)
  - Automatic FFmpeg discovery (`PATH` first, then `imageio-ffmpeg` fallback)
- **Desktop GUI & Scriptable CLI**: Launch the dark-slate Tkinter studio via `launch_studio.bat` or automate pipelines with `python -m autoshorts_studio.cli`.

---

## Community vs Pro Edition

| Feature | Community Edition (Free, MIT) | Pro Edition ($19 Lifetime) |
| --- | :---: | :---: |
| SHA-256 SQLite WAL Deduplication | Unlimited | Unlimited |
| `9:16`, `16:9`, `1:1` Aspect Presets | Single preset per run | Simultaneous multi-preset export |
| Beat-Milestone Audio Slicer | Full support | Full support |
| Ken Burns Image Motion & Loudness Norm | Full support | Full support |
| Clean Export (No Forced Watermark) | Yes | Yes |
| Custom Brand Watermark Overlay Hook | — | **Unlocked** |
| Batch Queue Limit | Up to 5 outputs/run | **Up to 500 outputs/run** |
| Offline Key Activation (`FS-PRO-AS-...` / `FS-PRO-BUNDLE-...`) | — | **Instant offline verification** |

**[Upgrade to AutoShorts Studio Pro ($19 Lifetime) ->](https://youthmy11-ai.github.io/#pricing)**

---

## Quick Start

### 1. Install Requirements

Requires Python 3.10+ and either system `ffmpeg` on `PATH` or `imageio-ffmpeg`:

```bash
pip install -r requirements.txt
```

### 2. Launch Desktop GUI

On Windows, double-click `launch_studio.bat` or run:

```bash
python -m autoshorts_studio.gui
```

### 3. CLI Examples

Scan a media folder and inspect deduplication status:

```bash
python -m autoshorts_studio.cli scan --input ./media_inbox
```

Build vertical `9:16` blurred-pillarbox clips with a soundtrack:

```bash
python -m autoshorts_studio.cli build \
  --input ./media_inbox \
  --audio ./music/theme.mp3 \
  --output ./rendered \
  --preset 9:16 \
  --fit blur_pillarbox \
  --count 3
```

Slice an audio track at beat milestones with smooth fades:

```bash
python -m autoshorts_studio.cli slice-audio \
  --input ./music/theme.mp3 \
  --output ./audio_slices \
  --milestones "0:15, 0:30, 1:02" \
  --fade-in 0.05 \
  --fade-out 0.2
```

Check or activate a Pro license key:

```bash
python -m autoshorts_studio.cli check-license --activate FS-PRO-AS-XXXXXXXX-XXXX
```

---

## Running Automated Tests

```bash
python -m unittest discover -s tests -v
```

---

## Companion Tools in ForgeSuite Studio

- **[TokenCost Lab (Free Web App)](https://youthmy11-ai.github.io/tokencalc.html)** — Estimate LLM token usage & monthly AI API costs across 7 frontier models.
- **[JSON2Sheet & PII Masker (Free Web App)](https://youthmy11-ai.github.io/json2sheet.html)** — Flatten nested JSON API payloads into clean CSV/Markdown tables with PII redaction.
- **[ScopeGuard SOW Builder (Free Web App)](https://youthmy11-ai.github.io/sowbuilder.html)** — Draft milestone contracts & export deposits directly into CryptoBill Forge.
- **[CryptoBill Forge (Free Web App)](https://youthmy11-ai.github.io/cryptobill.html)** — Build A4 PDF & shareable USDT / PayPal freelance invoices with offline QR codes.
- **[All-Access Pro Bundle ($35 · Save $14)](https://youthmy11-ai.github.io/#pricing)** — Lifetime keys for AutoShorts Studio Pro ($19), WebLead Extractor Pro ($15), and CryptoBill Forge Pro ($15) with instant on-chain BSC USDT verification.

---

## License

Released under the [MIT License](LICENSE).
