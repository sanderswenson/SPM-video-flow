"""End-to-end: raw clip -> plan -> postable video."""

from __future__ import annotations

from pathlib import Path

from . import captions, llm, media, render, transcript
from .config import Settings
from .plan import Plan, drop_fillers, remap, segments, summary

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}


def workdir_for(src: Path, root: Path) -> Path:
    d = root / src.stem
    d.mkdir(parents=True, exist_ok=True)
    return d


def make_plan(src: Path, wd: Path, s: Settings, transcript_path: Path | None = None) -> Plan:
    info = media.probe(src)

    words_file = wd / "transcript.json"
    if transcript_path is not None:
        words = transcript.load(transcript_path)
    elif words_file.exists():
        print("  transcript: cached")
        words = transcript.load(words_file)
    else:
        print(f"  transcribing with whisper '{s.whisper_model}' ...")
        audio = wd / "audio.wav"
        media.extract_audio(src, audio)
        words = transcript.transcribe(audio, s.whisper_model, s.language)
    transcript.save(words, words_file)
    if not words:
        raise SystemExit(f"no speech found in {src}")

    plan = Plan(str(src.resolve()), info.duration, s.fps or info.fps, words, [True] * len(words))
    drop_fillers(plan, s.fillers)

    if s.use_llm and llm.available():
        print(f"  asking {s.llm_model} to find retakes and write post copy ...")
        decision = llm.review(plan, s)
        if decision is not None:
            llm.apply(plan, decision)
    elif s.use_llm:
        print("  (ANTHROPIC_API_KEY not set: skipping retake detection and post copy)")
    return plan


def render_plan(plan: Plan, wd: Path, out: Path, s: Settings) -> None:
    src = Path(plan.source)
    info = media.probe(src)
    segs = segments(plan, s)
    if not segs:
        raise SystemExit("plan keeps nothing")
    print("  " + summary(plan, segs))

    size = s.output_size(*info.display_size)
    caps = None
    if s.captions:
        caps = wd / "captions.ass"
        captions.write(remap(plan, segs), size, s, caps)

    print(f"  cutting {len(segs)} segments ...")
    listing = render.cut_segments(src, segs, info, s, wd)
    print(f"  rendering {size[0]}x{size[1]} -> {out}")
    out.parent.mkdir(parents=True, exist_ok=True)
    render.final(listing, info, size, s, caps, out)

    if plan.post:
        post = out.with_suffix(".txt")
        p = plan.post
        post.write_text(f"{p.get('title', '')}\n\n{p.get('caption', '')}\n\n{' '.join(p.get('hashtags', []))}\n")
        print(f"  post copy -> {post}")


def process(src: Path, out_dir: Path, work_root: Path, s: Settings, plan_only: bool = False,
            transcript_path: Path | None = None) -> Path:
    print(f"\n== {src.name}")
    wd = workdir_for(src, work_root)
    plan = make_plan(src, wd, s, transcript_path)
    plan_file = wd / "plan.json"
    plan.save(plan_file)
    print(f"  plan -> {plan_file}")
    out = out_dir / f"{src.stem}_edit.mp4"
    if plan_only:
        print("  " + summary(plan, segments(plan, s)))
        return plan_file
    render_plan(plan, wd, out, s)
    return out
