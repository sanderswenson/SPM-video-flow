"""Render a plan: cut segments, join them, reframe, burn captions, normalize loudness."""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import media
from .config import Settings
from .media import MediaInfo

FADE = 0.012  # seconds of audio fade at every cut, kills clicks without being audible


def cut_segments(src: Path, segs: list[tuple[float, float]], info: MediaInfo, s: Settings, workdir: Path) -> Path:
    """Encode each kept range to an intermediate file and write a concat list.

    Intermediates use near-lossless H.264 + PCM audio so the join is sample-exact
    (AAC priming would add tiny gaps at every cut).
    """
    segdir = workdir / "segments"
    segdir.mkdir(exist_ok=True)
    for old in segdir.glob("*.mov"):
        old.unlink()
    fps = s.fps or info.fps

    def one(i: int) -> Path:
        a, b = segs[i]
        dur = b - a
        # Floor the seek so float error can never push it past the first frame's timestamp.
        seek = math.floor(a * 1e6) / 1e6
        out = segdir / f"{i:04d}.mov"
        fade = min(FADE, dur / 4)
        media.run([
            "-ss", f"{seek:.6f}", "-i", str(src), "-t", f"{dur:.6f}",
            "-map", "0:v:0", "-map", "0:a:0",
            "-vf", f"fps={fps}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "12", "-pix_fmt", "yuv420p",
            "-af", f"afade=t=in:d={fade:.4f},afade=t=out:st={dur - fade:.4f}:d={fade:.4f}",
            "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", "-video_track_timescale", "90000",
            str(out),
        ], quiet=True)
        return out

    workers = max(1, (os.cpu_count() or 2) // 2)
    with ThreadPoolExecutor(workers) as pool:
        files = list(pool.map(one, range(len(segs))))

    listing = workdir / "concat.txt"
    listing.write_text("".join(f"file '{f.resolve()}'\n" for f in files))
    return listing


def measure_loudness(listing: Path, target: float) -> dict:
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-f", "concat", "-safe", "0", "-i", str(listing),
         "-vn", "-af", f"loudnorm=I={target}:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"],
        capture_output=True, text=True, check=True,
    )
    match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", proc.stderr, re.S)
    if not match:
        raise RuntimeError("could not read loudnorm measurement:\n" + proc.stderr[-2000:])
    return json.loads(match.group(0))


def crop_filter(info: MediaInfo, size: tuple[int, int], s: Settings) -> str:
    src_w, src_h = info.display_size
    out_w, out_h = size
    if s.aspect == "source":
        return f"scale={out_w}:{out_h}:flags=lanczos"
    target = out_w / out_h
    if src_w / src_h > target:
        cw, ch = round(src_h * target / 2) * 2, src_h
    else:
        cw, ch = src_w, round(src_w / target / 2) * 2
    x = round((src_w - cw) * min(max(s.crop_x, 0), 1))
    y = round((src_h - ch) * min(max(s.crop_y, 0), 1))
    return f"crop={cw}:{ch}:{x}:{y},scale={out_w}:{out_h}:flags=lanczos"


def final(listing: Path, info: MediaInfo, size: tuple[int, int], s: Settings, captions: Path | None, out: Path) -> None:
    m = measure_loudness(listing, s.loudness)
    loud = (
        f"loudnorm=I={s.loudness}:TP=-1.5:LRA=11:measured_I={m['input_i']}:measured_TP={m['input_tp']}"
        f":measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true"
    )

    vf = [crop_filter(info, size, s), "setsar=1"]
    if captions is not None:
        ass = f"ass={captions.name}"
        fonts = Path(__file__).resolve().parent.parent / "fonts"
        if fonts.is_dir():
            ass += f":fontsdir={fonts}"
        vf.append(ass)
    vf.append("format=yuv420p")

    # Run from the work dir so the subtitle path needs no filter-graph escaping.
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-y", "-loglevel", "error", "-stats",
         "-f", "concat", "-safe", "0", "-i", str(listing.resolve()),
         "-vf", ",".join(vf), "-af", f"{loud},aresample=48000",
         "-c:v", "libx264", "-preset", s.preset, "-crf", str(s.crf), "-profile:v", "high",
         "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
         str(out.resolve())],
        check=True, cwd=listing.parent,
    )
