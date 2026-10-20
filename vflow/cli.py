"""Command line entry point.

    vflow edit raw.mov                 # one clip -> out/raw_edit.mp4
    vflow edit inbox/                  # every video in a folder
    vflow edit raw.mov --plan-only     # stop after deciding cuts; review work/raw/plan.json
    vflow render work/raw              # render (or re-render) from an edited plan.json
"""

from __future__ import annotations

import argparse
from pathlib import Path

from . import media
from .config import Settings
from .pipeline import VIDEO_EXTS, output_for, process, render_plan
from .plan import Plan


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", type=Path, help="TOML settings file (default: ./vflow.toml if present)")
    p.add_argument("--out", type=Path, default=Path("out"), help="output folder (default: out/)")
    p.add_argument("--aspect", choices=["9:16", "4:5", "1:1", "16:9", "source"])
    p.add_argument("--framing", choices=["auto", "manual"], help="auto = keep the speaker in frame (default)")
    p.add_argument("--crop-x", type=float, help="manual horizontal framing 0..1 when cropping (0.5 = center)")
    p.add_argument("--punch-in", type=float, help="zoom alternated at hard cuts, e.g. 1.08; 1 = off")
    p.add_argument("--no-captions", dest="captions", action="store_const", const=False)
    p.add_argument("--no-hook", dest="hook", action="store_const", const=False, help="no cold open / hook title")
    p.add_argument("--no-callouts", dest="callouts", action="store_const", const=False)
    p.add_argument("--broll", dest="broll_dir", help="folder of images to cut away to")
    p.add_argument("--music", help="background music file (or folder), ducked under speech")
    p.add_argument("--cta", help='end card text, e.g. "Follow for more"; "" for none')
    p.add_argument("--max-pause", type=float, help="longest silence to keep, seconds")
    p.add_argument("--loudness", type=float, help="target LUFS (default -14)")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="vflow", description="Raw talking-head footage in, postable short-form video out.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("edit", help="transcribe, decide cuts, and render")
    e.add_argument("inputs", nargs="+", type=Path, help="video files or folders")
    e.add_argument("--work", type=Path, default=Path("work"), help="scratch folder (default: work/)")
    e.add_argument("--plan-only", action="store_true", help="write plan.json and stop, for manual review")
    e.add_argument("--no-llm", dest="use_llm", action="store_const", const=False, help="skip the Claude editor pass")
    e.add_argument("--notes", dest="style_notes", help='guidance for the editor pass, e.g. "tight, under 45s"')
    e.add_argument("--whisper-model", help="faster-whisper model (tiny.en, base.en, small.en, medium.en, large-v3)")
    e.add_argument("--transcript", type=Path, help="use an existing transcript.json instead of transcribing")
    _common(e)

    r = sub.add_parser("render", help="render from a work folder's plan.json")
    r.add_argument("workdir", type=Path)
    _common(r)

    args = ap.parse_args(argv)
    media.require_ffmpeg()
    s = Settings.load(args.config)
    overrides = {k: v for k, v in vars(args).items() if k in Settings.__dataclass_fields__}
    s.update(overrides)

    if args.cmd == "render":
        plan = Plan.load(args.workdir / "plan.json")
        out = output_for(Path(plan.source), args.out)
        print("  " + render_plan(plan, args.workdir, out, s))
        print(f"  -> {out}")
        return

    files: list[Path] = []
    for p in args.inputs:
        if p.is_dir():
            files += sorted(f for f in p.iterdir() if f.suffix.lower() in VIDEO_EXTS)
        else:
            files.append(p)
    if not files:
        raise SystemExit("no videos found")
    if args.transcript and len(files) > 1:
        raise SystemExit("--transcript only works with a single input")

    for f in files:
        process(f, args.out, args.work, s, plan_only=args.plan_only, transcript_path=args.transcript)


if __name__ == "__main__":
    main()
