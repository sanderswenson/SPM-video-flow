"""Command line entry point.

    vflow edit raw.mov                 # one clip -> out/raw_edit.mp4
    vflow edit inbox/                  # every video in a folder
    vflow edit raw.mov --plan-only     # stop after deciding cuts; review work/raw/plan.json
    vflow render work/raw              # render (or re-render) from an edited plan.json
    vflow hooks raw.mov                # hook sheet: 10 hooks to film, matched to what the clip says
    vflow variants raw.mov --hooks hooks/   # one video per filmed hook clip
    vflow variants raw.mov --pick 1,4       # one video per hook, from the footage alone
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from . import hooks, llm, media
from .config import Settings
from .hooks import HookSheet
from .pipeline import VIDEO_EXTS, make_plan, output_for, process, render_plan, render_variant, workdir_for
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

    h = sub.add_parser("hooks", help="write a hook sheet: lines to film, matched to what the clip says")
    h.add_argument("input", type=Path, help="video file")
    h.add_argument("--work", type=Path, default=Path("work"), help="scratch folder (default: work/)")
    h.add_argument("--count", dest="hooks_count", type=int, help="how many hooks to propose (default 10)")
    h.add_argument("--no-llm", dest="use_llm", action="store_const", const=False,
                   help="catalog match and template drafts only, no API calls")
    h.add_argument("--notes", dest="style_notes", help='guidance for the writer, e.g. "dry, no questions"')
    h.add_argument("--transcript", type=Path, help="use an existing transcript.json instead of transcribing")
    h.add_argument("--whisper-model", help="faster-whisper model (tiny.en, base.en, small.en, medium.en, large-v3)")
    _common(h)

    v = sub.add_parser("variants", help="render one video per hook: filmed hook clips prepended, or hooks from the footage")
    v.add_argument("input", type=Path, help="the raw video; run `edit` or `hooks` on it first")
    v.add_argument("--hooks", nargs="*", type=Path, default=[], metavar="CLIP",
                   help="filmed hook clips (files or a folder); a number in the name picks the hook, e.g. 03.mov")
    v.add_argument("--pick", help='hook numbers to render, e.g. "1,3,5" (default: every hook with a clip, '
                                  'else every "from footage" hook)')
    v.add_argument("--work", type=Path, default=Path("work"), help="scratch folder (default: work/)")
    _common(v)

    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # hook sheets have arrows and curly quotes; cp1252 consoles don't
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
    if args.cmd == "hooks":
        return cmd_hooks(args, s)
    if args.cmd == "variants":
        return cmd_variants(args, s)

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


def write_sheet(plan: Plan, src: Path, wd: Path, out_dir: Path, s: Settings) -> tuple[HookSheet, Path]:
    """Produce the hook sheet for a plan: hooks.json in the work folder, a readable .md next to the outputs."""
    sheet = hooks.produce(plan, s)
    sheet.save(wd / "hooks.json")
    out_dir.mkdir(parents=True, exist_ok=True)
    md = out_dir / f"{src.stem}_hooks.md"
    md.write_text(sheet.markdown(src.name), encoding="utf-8")
    return sheet, md


def cmd_hooks(args, s: Settings) -> None:
    src: Path = args.input
    if not src.is_file():
        raise SystemExit(f"not found: {src}")
    print(f"\n== {src.name}")
    if s.use_llm and not llm.available():
        print("  (ANTHROPIC_API_KEY not set: hooks come straight from the catalog templates)")
    wd = workdir_for(src, args.work)
    plan_file = wd / "plan.json"
    if plan_file.exists():
        plan = Plan.load(plan_file)
    else:
        plan = make_plan(src, wd, s, args.transcript)
        plan.save(plan_file)
    sheet, md = write_sheet(plan, src, wd, args.out, s)
    print()
    print(sheet.markdown(src.name))
    print(f"  -> {md}")


def cmd_variants(args, s: Settings) -> None:
    src: Path = args.input
    wd = workdir_for(src, args.work)
    if not (wd / "plan.json").exists():
        raise SystemExit(f"no plan for {src.name} in {wd}: run `vflow edit` or `vflow hooks` on it first")
    plan = Plan.load(wd / "plan.json")
    sheet_file = wd / "hooks.json"
    sheet = HookSheet.load(sheet_file) if sheet_file.exists() else write_sheet(plan, src, wd, args.out, s)[0]

    files: list[Path] = []
    for p in args.hooks:
        if p.is_dir():
            files += sorted(f for f in p.iterdir() if f.suffix.lower() in VIDEO_EXTS)
        elif p.is_file():
            files.append(p)
        else:
            raise SystemExit(f"not found: {p}")
    picks = [int(x) for x in re.split(r"[,\s]+", args.pick.strip()) if x] if args.pick else None
    if bad := [n for n in picks or [] if not 1 <= n <= len(sheet.hooks)]:
        raise SystemExit(f"no hook {bad[0]} on the sheet (it has {len(sheet.hooks)})")
    clips = hooks.assign_clips(files, len(sheet.hooks), picks) if files else {}
    if picks is None:
        picks = sorted(clips) if clips else [h.n for h in sheet.hooks if h.from_footage]
    if not picks:
        raise SystemExit("nothing to render: give filmed hook clips with --hooks, or hook numbers with --pick")

    print(f"\n== {src.name}: {len(picks)} variant{'s' if len(picks) != 1 else ''}")
    for n in picks:
        hook = sheet.hooks[n - 1]
        clip = clips.get(n)
        print(f"\n-- hook {n}: {hook.title}  ({'clip: ' + clip.name if clip else 'from footage'})")
        if clip is None and not hook.cold_open:
            print("  (no filmed clip and no stand-in line in the footage: the title alone opens the video)")
        out, summary = render_variant(plan, wd, args.out, s, hook, clip)
        print("  " + summary)
        print(f"  -> {out}")


if __name__ == "__main__":
    main()
