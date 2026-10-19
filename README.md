# vflow: raw footage in, postable video out

Drop in an uncut talking-head clip. vflow:

1. **Transcribes** it with word-level timestamps ([faster-whisper](https://github.com/SYSTRAN/faster-whisper), runs locally).
2. **Cuts dead air and filler words** (um, uh, ...) while leaving natural short pauses alone.
3. **Removes retakes, false starts, stutters and off-camera chatter** with a Claude editor pass, keeping the last clean take. It also writes a title, caption and hashtags.
4. **Reframes** to 9:16 (or 4:5, 1:1, 16:9).
5. **Burns in word-by-word highlighted captions.**
6. **Normalizes loudness** to -14 LUFS with a two-pass measurement, so it sounds right on IG, TikTok and Shorts.

Every cut is frame-aligned, with a 12 ms audio fade at each cut so there are no clicks.

## Setup

You need **Python 3.10+** (3.12 is what's been tested on Windows), **ffmpeg**, and git.

**1. Install ffmpeg** (must be on your PATH):

```bash
winget install Gyan.FFmpeg     # Windows (then open a new terminal)
brew install ffmpeg            # macOS
sudo apt install ffmpeg        # Debian/Ubuntu
```

Check with `ffmpeg -version`.

**2. Get the code and install it** in a virtual environment:

```bash
git clone https://github.com/sanderswenson/SPM-video-flow.git
cd SPM-video-flow
python -m venv .venv
.venv\Scripts\activate          # Windows (PowerShell / cmd)
source .venv/bin/activate        # macOS / Linux
pip install -e .
```

Activate the venv again (`.venv\Scripts\activate`) in each new terminal before running vflow.

**3. Optional: a Claude API key** turns on retake detection and post copy. Without one, vflow still cuts
silences and filler words. In the desktop app, paste it in **Settings**. For the command line, set it in your shell:

```bash
$env:ANTHROPIC_API_KEY = "sk-ant-..."    # Windows PowerShell (this session only)
export ANTHROPIC_API_KEY=sk-ant-...      # macOS / Linux
```

The first video you process downloads the Whisper speech model (Balanced / `small.en` is about 500 MB).

## Desktop app

```bash
vflow-app            # opens a window
vflow-app --browser  # or use your default browser instead
```

If no native window is available (some Linux setups), it opens in the browser automatically.

- **Drop raw videos** on the window (or click **Add videos**). They queue and process one at a time.
- Finished videos go to **`Videos/vflow`** in your home folder (change it in Settings, or click **Output folder** to open it).
- **Settings**: format, framing, pacing, caption style, editor notes, transcription speed, and your Claude API key
  (stored in the system credential store, e.g. Windows Credential Manager).
- **Review cuts** (optional): turn on "Pause before rendering" to see the transcript with every cut struck
  through and color-coded by reason. Click a word to cut or restore it, shift-click to do a range, then **Render**.
- **Done**: play the video, show it in the file manager, and copy the generated caption + hashtags.

## Command line

### Use

```bash
vflow edit raw.mov                      # -> out/raw_edit.mp4 (+ out/raw_edit.txt post copy)
vflow edit inbox/                       # batch: every video in the folder
vflow edit raw.mov --notes "tight, under 45s, cut the intro small talk"
vflow edit raw.mov --aspect 4:5 --crop-x 0.4
vflow edit raw.mov --no-llm             # silence + filler cuts only, no API calls
```

#### Review before rendering

```bash
vflow edit raw.mov --plan-only          # writes work/raw/plan.json and stops
# open plan.json: every word has "keep": true/false and a "reason" when it was dropped
vflow render work/raw                   # render from the (edited) plan
```

Outputs go to `out/` and scratch files to `work/` in the folder you run from. Re-running `edit` on the same file reuses the cached transcript in `work/<name>/transcript.json`.

### Configure

Copy `vflow.example.toml` to `vflow.toml`. It covers pause length, caption font, colors and position, aspect, loudness target and Whisper model size. CLI flags override the file.

## How it works

```
raw.mov
  └─ ffmpeg → 16 kHz wav → faster-whisper (word timestamps)        transcript.json
       └─ drop fillers → Claude: drop retakes/false starts         plan.json  (reviewable)
            └─ kept words → frame-snapped segments (+pad, max pause)
                 ├─ per-segment encode (parallel, PCM audio, fades)
                 ├─ remap word times onto the edited timeline → captions.ass
                 └─ concat → crop/scale → burn captions → loudnorm (2-pass) → out/*.mp4
```

| Module | Role |
|---|---|
| `vflow/transcript.py` | Whisper transcription, word model, phrase grouping |
| `vflow/plan.py` | Keep/drop decisions → cut segments; timeline remap |
| `vflow/llm.py` | Claude editor pass (structured output) |
| `vflow/captions.py` | Word-highlight ASS captions |
| `vflow/render.py` | ffmpeg cutting, framing, captions, loudness |
| `vflow/pipeline.py`, `vflow/cli.py` | Orchestration and CLI |
| `vflow/app/` | Desktop app: local server + job queue (`server.py`, `jobs.py`), UI (`static/index.html`), window (`main.py`) |

## Tests

```bash
pip install -e '.[dev]' && pytest
```
