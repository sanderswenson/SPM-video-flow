"""End-to-end: raw clip -> plan -> postable video.

Stages report progress through a `report(stage, fraction)` callback (fraction is None when
unknown) so the CLI can print and the app can drive a progress bar.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable

from . import captions, framing, llm, media, render, transcript
from .config import Settings
from .plan import Plan, Shot, drop_fillers, drop_repeats, remap, shots, summary, total_duration, word_times

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
        decision = llm.review(plan, s, images)
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


def broll_cues(plan: Plan, shot_list: list[Shot], s: Settings, total: float) -> list[render.BrollCue]:
    images = broll_images(s)
    if not images:
        return []
    by_name = {p.name: p for p in images}
    times = word_times(plan, shot_list)
    earliest, latest = 3.0, total - (s.cta_seconds if s.cta else 0.0) - 1.0
    wanted: list[tuple[float, Path]] = []
    for cue in plan.overlays.get("broll", []):
        if cue["image"] in by_name and cue["word"] in times:
            wanted.append((times[cue["word"]][0], by_name[cue["image"]]))
    if not wanted:  # no editor decisions: spread the images out evenly
        n = min(len(images), max(1, int((latest - earliest) // 15)))
        wanted = [(earliest + (latest - earliest) * (k + 1) / (n + 1), images[k]) for k in range(n)]
    cues: list[render.BrollCue] = []
    last_end = -10.0
    for start, image in sorted(wanted):
        start = max(start, earliest)
        dur = min(s.broll_seconds, latest - start)
        if dur < 1.0 or start < last_end + 4.0:
            continue
        cues.append((round(start, 3), round(dur, 3), image))
        last_end = start + dur
    return cues


def render_plan(plan: Plan, wd: Path, out: Path, s: Settings, report: Report = print_report) -> str:
    """Render and return a one-line summary of the edit."""
    src = Path(plan.source)
    info = media.probe(src)
    shot_list = shots(plan, s)
    if not shot_list:
        raise ValueError("The edit keeps nothing. Mark some words as kept and try again.")
    total = total_duration(shot_list)
    size = s.output_size(*info.display_size)

    samples: list[framing.Sample] = []
    reframes = framing.base_crop(*info.display_size, *size) != info.display_size or s.punch_in > 1.0
    if s.framing == "auto" and reframes:
        if framing.available():
            report("Framing", 0.0)
            samples = framing.detect(src, info, wd / "subjects.json", lambda p: report("Framing", p))
        else:
            report("Framing: OpenCV is not installed, using the manual crop position", None)
    framings = framing.frame_shots(shot_list, samples, info, size, s)

    caps = None
    if s.captions:
        caps = wd / "captions.ass"
        captions.write(remap(plan, shot_list), size, s, caps, overlay_events(plan, shot_list, s, total))

    report("Cutting", 0.0)
    listing = render.cut_segments(src, shot_list, framings, size, info, s, wd, lambda p: report("Cutting", p))
    report("Rendering", 0.0)
    out.parent.mkdir(parents=True, exist_ok=True)
    render.final(listing, info, size, s, caps, out, total, lambda p: report("Rendering", p),
                 broll=broll_cues(plan, shot_list, s, total), music=pick_music(s, src))

    if plan.post:
        p = plan.post
        out.with_suffix(".txt").write_text(
            f"{p.get('title', '')}\n\n{p.get('caption', '')}\n\n{' '.join(p.get('hashtags', []))}\n", encoding="utf-8"
        )
    return summary(plan, shot_list)


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
