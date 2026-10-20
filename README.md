# vflow: raw footage in, postable video out

Drop in an uncut talking-head clip. vflow:

1. **Transcribes** it with word-level timestamps ([faster-whisper](https://github.com/SYSTRAN/faster-whisper), runs locally).
2. **Cuts dead air and filler words** (um, uh, ...) while leaving natural short pauses alone.
3. **Cuts repeated lines**: restarts, chains of false starts and whole re-recorded stretches, keeping the last take. This runs
   locally and needs no API key. A **Claude editor pass** then refines it (subtle retakes, off-camera chatter, tangents),
   **fixes the captions** (spoken numbers become "$5,500", misheard words, casing), picks a **hook** (a cold-open of the
   strongest line plus a title over the first seconds), places **callout cards** for the key numbers and **B-roll**
   photos (yours, or stock photos it searches for), writes the **call to action**, and the post title, caption and hashtags.
4. **Reframes** to 9:16 (or 4:5, 1:1, 16:9) and **keeps the speaker in frame**: a landscape clip is sampled a few times
   a second, the face is located, and every shot gets its own crop: static when the speaker stays put, a slow pan when
   they move. A subtle punch-in alternates at hard cuts so jump cuts read as intentional.
5. **Burns in word-by-word highlighted captions**, the hook title, callouts and the end card.
6. **Mixes a music bed** under the speech (ducked while they talk) if you give it a track, and **normalizes loudness**
   to -14 LUFS so it sounds right on IG, TikTok and Shorts.
7. **Writes a hook sheet**: ten openings to film as separate takes, matched to what the clip actually says (its
   numbers, before/after, timeframe, who it's for) from a catalog of hook patterns. Film them, drop the takes in, and
   vflow renders **one video per hook** so you can post or test several openings of the same talk. See
   [Hooks](#hooks-film-ten-openings-get-ten-videos).

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

Activate the venv again (`.venv\Scripts\activate`) in each new terminal before running vflow. If you installed an earlier
version, run `pip install -e .` again: speaker framing needs OpenCV, which is now a dependency.

**3. Optional: a Claude API key** turns on the editor pass (retake refinement, caption fixes, hook, callouts, B-roll
placement, post copy). Without one, vflow still cuts silences, filler words and repeated lines, frames the speaker, adds
captions and the end card. In the desktop app, paste it in **Settings**. For the command line, set it in your shell:

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
- **Settings**: format and framing (follow the speaker or a fixed position, punch-in strength), pacing, what the editor
  pass may do, caption style, the extras (hook, callouts, a B-roll image folder, a music file or folder, the call to action),
  transcription speed, and your Claude API key (stored in the system credential store, e.g. Windows Credential Manager).
- **Review cuts** (optional): turn on "Pause before rendering" to see the transcript with every cut struck
  through and color-coded by reason. Words whose caption text was corrected have a dotted underline (hover to see what will
  be shown). Click a word to cut or restore it, shift-click to do a range, then **Render**.
- **Done**: play the video, show it in the file manager, and copy the generated caption + hashtags.
- **Hooks** (on a finished or paused video): the hook sheet for that clip. Tick the hooks marked *from footage* to render
  them straight away, or **Add filmed hooks…** to match your takes to the hooks (a number in the file name picks the hook;
  otherwise they go in order) and **Render**. Each variant queues as its own video, named `<clip>_hook03-<title>.mp4`.

## Command line

### Use

```bash
vflow edit raw.mov                      # -> out/raw_edit.mp4 (+ out/raw_edit.txt post copy)
vflow edit inbox/                       # batch: every video in the folder
vflow edit raw.mov --notes "tight, under 45s, cut the intro small talk"
vflow edit raw.mov --aspect 4:5 --framing manual --crop-x 0.4
vflow edit raw.mov --broll photos/ --music bed.mp3 --cta "Link in bio"
vflow edit raw.mov --no-llm             # local cuts only (silences, fillers, repeated lines), no API calls
vflow edit raw.mov --no-hook --no-callouts --punch-in 1 --cta ""   # just the clean cut and captions
vflow hooks raw.mov                     # -> out/raw_hooks.md: ten hooks to film for this clip
vflow variants raw.mov --hooks takes/   # one video per filmed hook clip in takes/
vflow variants raw.mov --pick 1,4       # hooks 1 and 4 from the footage alone, no filming
```

#### Review before rendering

```bash
vflow edit raw.mov --plan-only          # writes work/raw/plan.json and stops
# open plan.json: every word has "keep": true/false, a "reason" when it was dropped, and an optional
# "display" (what the caption shows); "overlays" holds the hook, callouts, B-roll cues and CTA
vflow render work/raw                   # render from the (edited) plan
```

Outputs go to `out/` and scratch files to `work/` in the folder you run from. Re-running `edit` on the same file reuses
the cached transcript in `work/<name>/transcript.json` and the speaker positions in `work/<name>/subjects.json`.

### Configure

Copy `vflow.example.toml` to `vflow.toml`. It covers pause length, framing, caption font, colors and position, the
extras, aspect, loudness target and Whisper model size. CLI flags override the file.

### Hooks: film ten openings, get ten videos

The first two seconds decide whether anyone watches the rest, and the right opening depends on what the talk
contains: a figure, a before/after, a mistake, a timeframe. So instead of guessing one hook, vflow proposes several
and lets you film them as quick extra takes.

```bash
vflow hooks raw.mov
```

reads the clip's transcript (reusing the cached one) and writes `out/raw_hooks.md`, the **hook sheet**: ten hooks,
each with the line to say, an on-screen title, the pattern it follows, why it fits this clip, a filming note, and
whether a line already in the footage can stand in for it. The hooks come from `vflow/data/hooks.toml`, a catalog of
about 80 patterns in 20 families (number, payoff, mistake, contrarian, question, curiosity, story, before/after, list,
time, cost, comparison, proof, challenge, address, objection, promise, reveal, loss, confession, hot take, how-to).
Each pattern says which **signals** it needs; vflow detects those signals in the transcript (money, percentages,
timeframes, a before/after pair, costs, who the talk addresses, a place, mistakes, contrasts, lists, a process, proof,
...) and offers only patterns the clip can back up, at most two per family. With a Claude key the lines are written
for the clip in the speaker's voice, using only facts from the transcript; without one they come straight from the
templates with the clip's figures filled in and `[brackets]` where you need to complete them.

Film each hook as its own take: 3 to 6 seconds, looking at the lens, talking the moment you hit record. Save them
as `01.mov`, `02.mov`, ... (any name that starts with the hook number works; otherwise files are matched in order) and

```bash
vflow variants raw.mov --hooks takes/
```

transcribes each take, trims its dead air and any restart, puts it in front of the edit (captioned like the rest,
with the hook's title over it) and renders `out/raw_hook01-<title>.mp4`, `out/raw_hook02-...`, each with its own post
copy. Hooks marked *from footage* on the sheet can also be rendered without filming anything:
`vflow variants raw.mov --pick 1,4` opens those with the stand-in line from the footage under the hook's title.

Add your own patterns in a TOML file (same format as the bundled catalog; an entry with the same `id` replaces the
bundled one) and point `hooks_catalog` in `vflow.toml` or the app's Settings at it. `hooks_count` sets how many hooks
a sheet proposes.

### Stock photos for B-roll

If you don't have photos of your own (`--broll` / the B-roll folder is empty), the editor pass proposes a few
stock-photo searches tied to moments in the talk ("suburban house exterior" where the house is mentioned), and vflow
fetches one photo per moment from [Pexels](https://www.pexels.com/api/) or
[Unsplash](https://unsplash.com/developers), both free. Paste the key in the app's Settings under **Stock photos**
(stored in the credential store, like the Claude key), or for the command line set `VFLOW_STOCK_API_KEY`
(or `PEXELS_API_KEY` / `UNSPLASH_ACCESS_KEY`), or put `stock_api_key` in `vflow.toml`; `stock_provider` picks the
service. Photos are cached under `work/<name>/broll/` so re-renders don't refetch, and the photographers' credits are
appended to the post copy `.txt`. Your own images always take precedence, and without a key nothing is fetched.

## How it works

```
raw.mov
  └─ ffmpeg → 16 kHz wav → faster-whisper (word timestamps) → merge split tokens     transcript.json
       └─ drop fillers → drop repeated lines → Claude: refine cuts, fix captions,
          pick hook / callouts / B-roll spots / CTA, write post copy                  plan.json  (reviewable)
            └─ kept words → frame-snapped shots (+pad, max pause), hook clip first
                 ├─ sample source @2.5 fps → face boxes → one crop per shot          subjects.json
                 ├─ per-shot encode (parallel, framed to output size, PCM audio, fades)
                 ├─ remap word times onto the edited timeline → captions.ass (+ hook, callouts, CTA)
                 └─ concat → B-roll overlays → burn captions → loudnorm (2-pass) → music bed → out/*.mp4
```

| Module | Role |
|---|---|
| `vflow/transcript.py` | Whisper transcription, word model, token cleanup, phrase grouping |
| `vflow/plan.py` | Keep/drop decisions, repeated-line detection → shots; timeline remap |
| `vflow/llm.py` | Claude editor pass (structured output) |
| `vflow/hooks.py`, `vflow/data/hooks.toml` | Hook catalog, transcript signals, catalog matching, the hook sheet, the Claude hook writer |
| `vflow/framing.py` | Face sampling and per-shot crop (static or slow pan, punch-in) |
| `vflow/captions.py` | Word-highlight ASS captions, hook title, callouts, end card |
| `vflow/render.py` | ffmpeg cutting + framing, B-roll, captions, music, loudness |
| `vflow/pipeline.py`, `vflow/cli.py` | Orchestration (including hook variants: a filmed take prepended to the edit) and CLI |
| `vflow/app/` | Desktop app: local server + job queue (`server.py`, `jobs.py`), UI (`static/index.html`), window (`main.py`) |

## Tests

```bash
pip install -e '.[dev]' && pytest
```
