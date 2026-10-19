"""Thin wrappers around ffprobe/ffmpeg."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Callable

# Keep ffmpeg from flashing a console window for every call in the windowed Windows app.
NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


@dataclass
class MediaInfo:
    duration: float
    width: int
    height: int
    fps: float
    rotation: int = 0

    @property
    def display_size(self) -> tuple[int, int]:
        if self.rotation % 180:
            return self.height, self.width
        return self.width, self.height


def require_ffmpeg() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise SystemExit(f"{tool} not found on PATH. Install ffmpeg (brew install ffmpeg / apt install ffmpeg).")


def probe(path: Path) -> MediaInfo:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
        check=True, capture_output=True, text=True, **NO_WINDOW,
    ).stdout
    data = json.loads(out)
    video = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    if video is None:
        raise ValueError(f"{path.name} has no video stream")
    if not any(s["codec_type"] == "audio" for s in data["streams"]):
        raise ValueError(f"{path.name} has no audio stream")

    rotation = 0
    for sd in video.get("side_data_list", []):
        if "rotation" in sd:
            rotation = abs(int(sd["rotation"]))
    if "rotate" in video.get("tags", {}):
        rotation = abs(int(video["tags"]["rotate"]))

    fps = float(Fraction(video.get("avg_frame_rate") or video["r_frame_rate"]))
    if fps <= 0:
        fps = float(Fraction(video["r_frame_rate"]))
    return MediaInfo(
        duration=float(data["format"]["duration"]),
        width=int(video["width"]),
        height=int(video["height"]),
        fps=fps,
        rotation=rotation,
    )


def run(cmd: list[str], cwd: Path | None = None) -> None:
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error", *cmd],
        cwd=cwd, capture_output=True, text=True, **NO_WINDOW,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.strip()[-1500:]}")


def run_with_progress(cmd: list[str], total: float, on_progress: Callable[[float], None], cwd: Path | None = None) -> None:
    """Run ffmpeg, calling on_progress(0..1) as it encodes `total` seconds of output."""
    proc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-progress", "pipe:1", "-nostats", *cmd],
        cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **NO_WINDOW,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        key, _, value = line.strip().partition("=")
        if key == "out_time_us" and value.isdigit() and total > 0:
            on_progress(min(1.0, int(value) / 1e6 / total))
    err = proc.stderr.read() if proc.stderr else ""
    if proc.wait() != 0:
        raise RuntimeError(f"ffmpeg failed: {err.strip()[-1500:]}")


def extract_audio(src: Path, dst: Path) -> None:
    run(["-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dst)])
