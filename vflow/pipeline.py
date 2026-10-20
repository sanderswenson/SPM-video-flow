"""End-to-end: raw clip -> plan -> postable video.

Stages report progress through a `report(stage, fraction)` callback (fraction is None when
unknown) so the CLI can print and the app can drive a progress bar.
"""

from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import captions, framing, llm, media, render, stock, transcript
from .config import Settings
from .hooks import Hook
from .plan import Plan, Shot, drop_fillers, drop_repeats, remap, segments, shots, summary, total_duration, word_times

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}

Report = Callable[[str, float | None], None]


def print_report(stage: str, fraction: float | None) -> None:
    # The CLI only prints stage changes, not every progress tick.
    if fraction is None or fraction == 0.0:
        print(f"  {stage} ...")


def workdir_for(src: Path, root: Path) -> Path:
    d = root / src.stem
    d.mkdir(parents=True, exist_ok=True)
    return d


def output_for(src: Path, out_dir: Path) -> Path:
    return out_dir / f"{src.stem}_edit.mp4"


def broll_images(s: Settings) -> list[Path]:
    d = Path(s.broll_dir) if s.broll_dir else None
    if d is None or not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if p.suffix.lower() in IMAGE_EXTS)


def pick_music(s: Settings, src: Path) -> Path | None:
    if not s.music:
        return None
    p = Path(s.music)
    if p.is_file():
        return p
    if p.is_dir():
        tracks = sorted(t for t in p.iterdir() if t.suffix.lower() in AUDIO_EXTS)
        if tracks:  # same clip always gets the same track; different clips vary
            return tracks[int(hashlib.md5(src.stem.encode()).hexdigest(), 16) % len(tracks)]
    print(f"  (music not found: {p})")
    return None


def make_plan(src: Path, wd: Path, s: Settings, transcript_path: Path | None = None,
              report: Report = print_report) -> Plan:
    info = media.probe(src)

    words_file = wd / "transcript.json"
    if transcript_path is not None:
        words = transcript.load(transcript_path)
    elif words_file.exists():
        words = transcript.load(words_file)
    else:
        report("Extracting audio", None)
        audio = wd / "audio.wav"
        media.extract_audio(src, audio)
        words = transcript.transcribe(audio, s.whisper_model, s.language, s.whisper_device, report)
        audio.unlink(missing_ok=True)
    transcript.save(words, words_file)
    words = transcript.normalize(words)
    if not words:
        raise ValueError(f"No speech found in {src.name}")

    plan = Plan(str(src.resolve()), info.duration, s.fps or info.fps, words, [True] * len(words))
    drop_fillers(plan, s.fillers)

    llm_on = s.use_llm and llm.available()
    if s.cut_repeats:
        n = drop_repeats(plan, aggressive=not llm_on)
        if n:
            report(f"Cut {n} words of repeated lines", None)
    if llm_on:
        report("Editor pass", None)
        images = [p.name for p in broll_images(s)]
        decision = llm.review(plan, s, images, stock=not images and stock.available(s))
        if decision is None:
            report("Editor pass declined; keeping the automatic cuts only", None)
        else:
            llm.apply(plan, decision, images)
    return plan


def overlay_events(plan: Plan, shot_list: list[Shot], s: Settings, total: float) -> dict:
    """Hook title, callout cards and the end card, placed on the edited timeline."""
    times = word_times(plan, shot_list)
    out: dict = {}
    hook_text = plan.overlays.get("hook_text")
    if s.hook and hook_text:
        hook_len = shot_list[0].duration if shot_list and shot_list[0].kind == "hook" else 0.0
        out["hook"] = (min(max(2.6, hook_len), 4.0, total), hook_text)
    callouts = []
    last_end = 0.0
    if s.callouts:
        for c in sorted(plan.overlays.get("callouts", []), key=lambda c: c["first"]):
            if c["first"] not in times:
                continue
            start = max(times[c["first"]][0], last_end + 0.4)
            end = min(max(times.get(c["last"], times[c["first"]])[1], start + 1.8), start + 4.0, total)
            if end - start >= 1.0:
                callouts.append((start, end, c["text"]))
                last_end = end
    out["callouts"] = callouts
    cta = plan.overlays.get("cta") if "cta" in plan.overlays else s.cta
    if cta and total > 8:
        out["cta"] = (max(0.0, total - s.cta_seconds), total, cta)
    return out


def broll_cues(plan: Plan, shot_list: list[Shot], s: Settings, total: float, wd: Path,
               size: tuple[int, int]) -> tuple[list[render.BrollCue], list[str]]:
    """Where to cut away to an image, and the photo credits to publish.

    The creator's own images (broll_dir) win. Without any, the editor pass's stock-photo queries are
    fetched (and cached under the work folder) when a stock API key is configured.
    """
    images = broll_images(s)
    times = word_times(plan, shot_list)
    earliest, latest = 3.0, total - (s.cta_seconds if s.cta else 0.0) - 1.0
    wanted: list[tuple[float, Path, str]] = []  # start, image, credit
    if images:
        by_name = {p.name: p for p in images}
        for cue in plan.overlays.get("broll", []):
            if cue["image"] in by_name and cue["word"] in times:
                wanted.append((times[cue["word"]][0], by_name[cue["image"]], ""))
        if not wanted:  # no editor decisions: spread the images out evenly
            n = min(len(images), max(1, int((latest - earliest) // 15)))
            wanted = [(earliest + (latest - earliest) * (k + 1) / (n + 1), images[k], "") for k in range(n)]
    else:
        queries = [q for q in plan.overlays.get("broll_queries", []) if q.get("word") in times and q.get("query")]
        if queries and stock.available(s):
            photos = stock.fetch([q["query"] for q in queries], s, wd / "broll", stock.orientation_for(size))
            for q in queries:
                if (photo := photos.get(q["query"].strip())) is not None:
                    wanted.append((times[q["word"]][0], photo.path, photo.credit))
    cues: list[render.BrollCue] = []
    credits: list[str] = []
    used: set[Path] = set()
    last_end = -10.0
    for start, image, credit in sorted(wanted, key=lambda w: w[0]):
        start = max(start, earliest)
        dur = min(s.broll_seconds, latest - start)
        if dur < 1.0 or start < last_end + 4.0 or image in used:
            continue
        used.add(image)
        cues.append((round(start, 3), round(dur, 3), image))
        if credit and credit not in credits:
            credits.append(credit)
        last_end = start + dur
    return cues, credits


@dataclass
class Lead:
    """A separately filmed hook clip that opens a variant: its own plan (for captions) and its shots."""
    source: Path
    plan: Plan
    shots: list[Shot]
    workdir: Path


def lead_from_clip(clip: Path, wd: Path, s: Settings, report: Report = print_report) -> Lead:
    """Transcribe a filmed hook clip, trim its dead air, fillers and restarts, and make it the opening shots."""
    hwd = wd / "hooks" / re.sub(r"[^\w.-]+", "_", clip.stem)
    hwd.mkdir(parents=True, exist_ok=True)
    info = media.probe(clip)
    words_file = hwd / "transcript.json"
    if words_file.exists():
        words = transcript.load(words_file)
    else:
        audio = hwd / "audio.wav"
        media.extract_audio(clip, audio)
        stage = "Transcribing hook clip"
        words = transcript.transcribe(audio, s.whisper_model, s.language, s.whisper_device,
                                      lambda st, p: report(stage if st == "Transcribing" else st, p))
        audio.unlink(missing_ok=True)
        transcript.save(words, words_file)
    words = transcript.normalize(words)
    if not words:
        raise ValueError(f"No speech found in hook clip {clip.name}")
    hp = Plan(str(clip.resolve()), info.duration, s.fps or info.fps, words, [True] * len(words))
    drop_fillers(hp, s.fillers)
    drop_repeats(hp, aggressive=True)  # a flubbed first attempt in the take goes; the last clean one stays
    segs = segments(hp, s)
    if not segs:
        raise ValueError(f"Nothing left of hook clip {clip.name} after trimming silence")
    return Lead(clip, hp, [Shot(a, b, "hook", str(clip.resolve())) for a, b in segs], hwd)


def _framings(src: Path, info: media.MediaInfo, shot_list: list[Shot], wd: Path, size: tuple[int, int],
              s: Settings, report: Report) -> list[framing.Framing]:
    samples: list[framing.Sample] = []
    reframes = framing.base_crop(*info.display_size, *size) != info.display_size or s.punch_in > 1.0
    if s.framing == "auto" and reframes:
        if framing.available():
            report("Framing", 0.0)
            samples = framing.detect(src, info, wd / "subjects.json", lambda p: report("Framing", p))
        else:
            report("Framing: OpenCV is not installed, using the manual crop position", None)
    return framing.frame_shots(shot_list, samples, info, size, s)


def render_plan(plan: Plan, wd: Path, out: Path, s: Settings, report: Report = print_report,
                lead: Lead | None = None) -> str:
    """Render and return a one-line summary of the edit.

    With a `lead`, a separately filmed hook clip opens the video in place of the plan's own cold open;
    its words are captioned too, and everything placed by word (callouts, B-roll) shifts after it.
    """
    src = Path(plan.source)
    info = media.probe(src)
    if lead is None:
        shot_list = main = shots(plan, s)
    else:
        main = [Shot(a, b) for a, b in segments(plan, s)]
        shot_list = lead.shots + main
    if not main:
        raise ValueError("The edit keeps nothing. Mark some words as kept and try again.")
    total = total_duration(shot_list)
    size = s.output_size(*info.display_size)

    framings: list[framing.Framing] = []
    if lead is not None:
        framings += _framings(lead.source, media.probe(lead.source), lead.shots, lead.workdir, size, s, report)
    framings += _framings(src, info, main, wd, size, s, report)

    caps = None
    if s.captions:
        caps = wd / "captions.ass"
        words = remap(plan, main)
        if lead is not None:
            offset = total_duration(lead.shots)
            words = remap(lead.plan, lead.shots) + [
                transcript.Word(round(w.start + offset, 3), round(w.end + offset, 3), w.text, w.prob, w.display)
                for w in words]
        captions.write(words, size, s, caps, overlay_events(plan, shot_list, s, total))
    cues, credits = broll_cues(plan, shot_list, s, total, wd, size)

    report("Cutting", 0.0)
    listing = render.cut_segments(src, shot_list, framings, size, info, s, wd, lambda p: report("Cutting", p))
    report("Rendering", 0.0)
    out.parent.mkdir(parents=True, exist_ok=True)
    render.final(listing, info, size, s, caps, out, total, lambda p: report("Rendering", p),
                 broll=cues, music=pick_music(s, src))

    if plan.post or credits:
        p = plan.post
        text = f"{p.get('title', '')}\n\n{p.get('caption', '')}\n\n{' '.join(p.get('hashtags', []))}\n"
        if credits:
            text += "\nPhotos: " + ", ".join(credits) + "\n"
        out.with_suffix(".txt").write_text(text, encoding="utf-8")
    return summary(plan, shot_list)


def variant_output(src: Path, out_dir: Path, hook: Hook) -> Path:
    return out_dir / f"{src.stem}_hook{hook.n:02d}-{hook.slug}.mp4"


def render_variant(plan: Plan, wd: Path, out_dir: Path, s: Settings, hook: Hook, clip: Path | None = None,
                   report: Report = print_report) -> tuple[Path, str]:
    """One video for one hook from the hook sheet: a filmed hook clip prepended to the edit, or, without a
    clip, the hook's title over a cold open of the footage line that stands in for it. Returns the output
    path and the edit summary."""
    s = copy.copy(s)
    s.hook = True
    v = copy.deepcopy(plan)
    v.overlays = {k: val for k, val in plan.overlays.items() if k != "hook_range"}
    v.overlays["hook_text"] = hook.title
    if clip is None and hook.cold_open:
        v.overlays["hook_range"] = list(hook.cold_open)
    if v.post:
        v.post = {**v.post, "title": hook.post_title or hook.title}
    lead = lead_from_clip(clip, wd, s, report) if clip is not None else None
    out = variant_output(Path(plan.source), out_dir, hook)
    return out, render_plan(v, wd, out, s, report, lead=lead)


def process(src: Path, out_dir: Path, work_root: Path, s: Settings, plan_only: bool = False,
            transcript_path: Path | None = None) -> Path:
    print(f"\n== {src.name}")
    wd = workdir_for(src, work_root)
    if s.use_llm and not llm.available():
        print("  (ANTHROPIC_API_KEY not set: skipping the Claude editor pass; repeated lines are still cut automatically)")
    plan = make_plan(src, wd, s, transcript_path)
    plan_file = wd / "plan.json"
    plan.save(plan_file)
    print(f"  plan -> {plan_file}")
    if plan_only:
        print("  " + summary(plan, shots(plan, s)))
        return plan_file
    out = output_for(src, out_dir)
    print("  " + render_plan(plan, wd, out, s))
    print(f"  -> {out}")
    return out
