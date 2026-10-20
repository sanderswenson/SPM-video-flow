"""Render a plan: cut and frame each shot, join them, add captions, B-roll and music, normalize loudness."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

from . import media
from .config import Settings
from .framing import Framing
from .media import MediaInfo
from .plan import Shot

FADE = 0.012  # seconds of audio fade at every cut, kills clicks without being audible
BROLL_FADE = 0.3

Progress = Callable[[float], None]
BrollCue = tuple[float, float, Path]  # start on the edited timeline, duration, image file


def cut_segments(src: Path, shots: list[Shot], framings: list[Framing], size: tuple[int, int], info: MediaInfo,
                 s: Settings, workdir: Path, on_progress: Progress = lambda p: None) -> Path:
    """Encode each shot, already framed to the output size, to an intermediate file and write a concat list.

    Intermediates use near-lossless H.264 + PCM audio so the join is sample-exact
    (AAC priming would add tiny gaps at every cut).
    """
    segdir = workdir / "segments"
    segdir.mkdir(exist_ok=True)
    for old in segdir.glob("*.mov"):
        old.unlink()
    fps = s.fps or info.fps

    def one(i: int) -> str:
        sh, fr = shots[i], framings[i]
        dur = sh.duration
        # Floor the seek so float error can never push it past the first frame's timestamp.
        seek = math.floor(sh.start * 1e6) / 1e6
        name = f"{i:04d}.mov"
        fade = min(FADE, dur / 4)
        media.run([
            "-ss", f"{seek:.6f}", "-i", str(src), "-t", f"{dur:.6f}",
            "-map", "0:v:0", "-map", "0:a:0",
            "-vf", f"fps={fps},{fr.filter(size)},setsar=1",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "12", "-pix_fmt", "yuv420p",
            "-af", f"afade=t=in:d={fade:.4f},afade=t=out:st={dur - fade:.4f}:d={fade:.4f}",
            "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", "-video_track_timescale", "90000",
            str(segdir / name),
        ])
        return name

    workers = max(1, (os.cpu_count() or 2) // 2)
    with ThreadPoolExecutor(workers) as pool:
        futures = [pool.submit(one, i) for i in range(len(shots))]
        for done, _ in enumerate(as_completed(futures), 1):
            on_progress(done / len(shots))
        names = [f.result() for f in futures]

    # Relative paths: resolved against the list's folder, and no drive letters/backslashes to escape.
    listing = workdir / "concat.txt"
    listing.write_text("".join(f"file 'segments/{n}'\n" for n in names))
    return listing


def measure_loudness(listing: Path, target: float) -> dict:
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-f", "concat", "-safe", "0", "-i", listing.name,
         "-vn", "-af", f"loudnorm=I={target}:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"],
        capture_output=True, text=True, check=True, cwd=listing.parent, **media.NO_WINDOW,
    )
    match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", proc.stderr, re.S)
    if not match:
        raise RuntimeError("could not read loudnorm measurement:\n" + proc.stderr[-2000:])
    return json.loads(match.group(0))


def _f(v: float) -> str:
    return f"{v:.3f}"


def final(listing: Path, info: MediaInfo, size: tuple[int, int], s: Settings, captions: Path | None, out: Path,
          total: float, on_progress: Progress = lambda p: None, broll: list[BrollCue] | None = None,
          music: Path | None = None) -> None:
    m = measure_loudness(listing, s.loudness)
    loud = (
        f"loudnorm=I={s.loudness}:TP=-1.5:LRA=11:measured_I={m['input_i']}:measured_TP={m['input_tp']}"
        f":measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true"
    )
    workdir = listing.parent
    ow, oh = size
    fps = s.fps or info.fps
    inputs = ["-f", "concat", "-safe", "0", "-i", listing.name]
    n_inputs = 1
    graph: list[str] = []

    # --- video: B-roll cutaways (slow push-in, faded) over the talking head, then captions ---
    vlabel = "[0:v]"
    for k, (start, dur, image) in enumerate(broll or []):
        idx, n_inputs = n_inputs, n_inputs + 1
        inputs += ["-loop", "1", "-framerate", str(fps), "-t", _f(dur), "-i", str(image.resolve())]
        frames = max(1, round(dur * fps))
        fade = min(BROLL_FADE, dur / 3)
        graph.append(
            f"[{idx}:v]scale={ow}:{oh}:force_original_aspect_ratio=increase,crop={ow}:{oh},"
            f"zoompan=z='1+0.07*on/{frames}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={ow}x{oh}:fps={fps},"
            f"format=yuva420p,fade=t=in:st=0:d={_f(fade)}:alpha=1,fade=t=out:st={_f(dur - fade)}:d={_f(fade)}:alpha=1,"
            f"setpts=PTS-STARTPTS+{_f(start)}/TB[b{k}]"
        )
        graph.append(f"{vlabel}[b{k}]overlay=eof_action=pass:repeatlast=0:enable='between(t,{_f(start)},{_f(start + dur)})'[v{k}]")
        vlabel = f"[v{k}]"
    vchain = []
    if captions is not None:
        ass = f"ass={captions.name}"
        fonts = Path(__file__).resolve().parent / "fonts"
        if fonts.is_dir():
            # Copied next to the work files so the filter can use a relative path (no "C:" to escape).
            shutil.copytree(fonts, workdir / "fonts", dirs_exist_ok=True)
            ass += ":fontsdir=fonts"
        vchain.append(ass)
    vchain.append("format=yuv420p")
    graph.append(f"{vlabel}{','.join(vchain)}[vout]")

    # --- audio: normalized speech, optionally with a music bed ducked under it ---
    graph.append(f"[0:a]{loud},aresample=48000[voice]")
    alabel = "[voice]"
    if music is not None and total > 2:
        idx, n_inputs = n_inputs, n_inputs + 1
        inputs += ["-stream_loop", "-1", "-i", str(music.resolve())]
        fade_out = min(2.5, total / 3)
        graph.append(
            f"[{idx}:a]atrim=0:{_f(total)},asetpts=PTS-STARTPTS,aformat=sample_rates=48000:channel_layouts=stereo,"
            f"afade=t=in:d=1,afade=t=out:st={_f(total - fade_out)}:d={_f(fade_out)},volume={s.music_db}dB[music]"
        )
        graph.append("[voice]asplit[voice1][voice2]")
        graph.append("[music][voice2]sidechaincompress=threshold=0.02:ratio=8:attack=30:release=500[ducked]")
        graph.append("[voice1][ducked]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.89:level=false[aout]")
        alabel = "[aout]"

    media.run_with_progress(
        [*inputs, "-filter_complex", ";".join(graph), "-map", "[vout]", "-map", alabel,
         "-c:v", "libx264", "-preset", s.preset, "-crf", str(s.crf), "-profile:v", "high",
         "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-t", _f(total),
         str(out.resolve())],
        total, on_progress, cwd=workdir,
    )
