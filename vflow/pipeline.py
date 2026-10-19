"""End-to-end: raw clip -> plan -> postable video.

Stages report progress through a `report(stage, fraction)` callback (fraction is None when
unknown) so the CLI can print and the app can drive a progress bar.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from . import captions, llm, media, render, transcript
from .config import Settings
from .plan import Plan, drop_fillers, remap, segments, summary

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}

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
    if not words:
        raise ValueError(f"No speech found in {src.name}")

    plan = Plan(str(src.resolve()), info.duration, s.fps or info.fps, words, [True] * len(words))
    drop_fillers(plan, s.fillers)

    if s.use_llm and llm.available():
        report("Finding retakes and writing post copy", None)
        decision = llm.review(plan, s)
        if decision is None:
            report("Editor pass declined; keeping silence and filler cuts only", None)
        else:
            llm.apply(plan, decision)
    return plan


def render_plan(plan: Plan, wd: Path, out: Path, s: Settings, report: Report = print_report) -> str:
    """Render and return a one-line summary of the edit."""
    src = Path(plan.source)
    info = media.probe(src)
    segs = segments(plan, s)
    if not segs:
        raise ValueError("The edit keeps nothing. Mark some words as kept and try again.")
    total = sum(b - a for a, b in segs)

    size = s.output_size(*info.display_size)
    caps = None
    if s.captions:
        caps = wd / "captions.ass"
        captions.write(remap(plan, segs), size, s, caps)

    report("Cutting", 0.0)
    listing = render.cut_segments(src, segs, info, s, wd, lambda p: report("Cutting", p))
    report("Rendering", 0.0)
    out.parent.mkdir(parents=True, exist_ok=True)
    render.final(listing, info, size, s, caps, out, total, lambda p: report("Rendering", p))

    if plan.post:
        p = plan.post
        out.with_suffix(".txt").write_text(
            f"{p.get('title', '')}\n\n{p.get('caption', '')}\n\n{' '.join(p.get('hashtags', []))}\n", encoding="utf-8"
        )
    return summary(plan, segs)


def process(src: Path, out_dir: Path, work_root: Path, s: Settings, plan_only: bool = False,
            transcript_path: Path | None = None) -> Path:
    print(f"\n== {src.name}")
    wd = workdir_for(src, work_root)
    if s.use_llm and not llm.available():
        print("  (ANTHROPIC_API_KEY not set: skipping retake detection and post copy)")
    plan = make_plan(src, wd, s, transcript_path)
    plan_file = wd / "plan.json"
    plan.save(plan_file)
    print(f"  plan -> {plan_file}")
    if plan_only:
        print("  " + summary(plan, segments(plan, s)))
        return plan_file
    out = output_for(src, out_dir)
    print("  " + render_plan(plan, wd, out, s))
    print(f"  -> {out}")
    return out
