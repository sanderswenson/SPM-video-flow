# vflow: raw footage in, postable video out

Drop in an uncut talking-head clip. vflow:

1. **Transcribes** it with word-level timestamps ([faster-whisper](https://github.com/SYSTRAN/faster-whisper), runs locally).
2. **Cuts dead air and filler words** (um, uh, ...) while leaving natural short pauses alone.
3. **Removes retakes, false starts, stutters and off-camera chatter** with a Claude editor pass, keeping the last clean take. It also writes a title, caption and hashtags.
4. **Reframes** to 9:16 (or 4:5, 1:1, 16:9).
5. **Burns in word-by-word highlighted captions.**
6. **Normalizes loudness** to -14 LUFS with a two-pass measurement, so it sounds right on IG, TikTok and Shorts.

Every cut is frame-aligned, with a 12 ms audio fade at each cut so there are no clicks.

## Desktop app (Windows)

Download `vflow-<version>-setup.exe` from Releases (or from the latest **Windows app** Actions run's
artifacts) and run it. It installs per-user, so no admin prompt. Then:

- **Drop raw videos** on the window (or click **Add videos**). They queue and process one at a time.
- **Settings**: format, framing, pacing, caption style, editor notes, Whisper speed, and your Claude API key
  (stored in Windows Credential Manager).
- **Review cuts** (optional): turn on "Pause before rendering" to see the transcript with every cut struck
  through and color-coded by reason. Click a word to cut or restore it, shift-click to do a range, then **Render**.
- **Done**: play the video, show it in Explorer, and copy the generated caption + hashtags.
- **Updates**: the app checks for a new release on launch and every 6 hours. One click downloads the new
  installer, which updates in place and relaunches.

The installer isn't code-signed yet, so the first time Windows SmartScreen shows "Windows protected your PC".
Click **More info → Run anyway**. Signing can be added to CI later with no app changes.

First run downloads the speech model (Balanced is about 500 MB) into the user's cache.

### Releasing an update

1. Bump `__version__` in `vflow/__init__.py` and commit.
2. `git tag v0.2.1 && git push origin v0.2.1`
3. The **Windows app** workflow builds the app, smoke-tests it (a real transcription + render on a synthesized
   speech clip), tests a silent install and uninstall, and publishes the installer as a GitHub Release. Installed apps pick it up.

**While this repo is private**, installed apps can't read its releases. Make a small public repo for
releases (e.g. `vflow-releases`, with one commit such as a README), then in this repo's settings add:
- the variable `RELEASE_REPO` = `owner/vflow-releases`
- the secret `RELEASES_TOKEN` = a fine-grained token with *Contents: read and write* on that repo.

Builds bake the release repo in at build time, so set these before tagging the release you hand out.

### Run the app from source

```bash
pip install -e .
vflow-app            # native window
vflow-app --browser  # or in your browser
```

## Command line

### Setup

You need Python 3.10+ and ffmpeg.

```bash
brew install ffmpeg            # or: apt install ffmpeg
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
export ANTHROPIC_API_KEY=sk-ant-...   # optional, turns on retake detection + post copy
```

The first run downloads the Whisper model (`small.en` is about 500 MB).

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

Re-running `edit` on the same file reuses the cached transcript in `work/<name>/transcript.json`.

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
| `vflow/app/` | Desktop app: local server + job queue (`server.py`, `jobs.py`), UI (`static/index.html`), window (`main.py`), updater |
| `packaging/` | PyInstaller spec, Inno Setup installer, icon |
| `.github/workflows/windows.yml` | Build, smoke-test, package and release on Windows |

## Tests

```bash
pip install -e '.[dev]' && pytest
```
