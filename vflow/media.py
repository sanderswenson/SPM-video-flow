"""Thin wrappers around ffprobe/ffmpeg."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path


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
        check=True, capture_output=True, text=True,
    ).stdout
    data = json.loads(out)
    video = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    if video is None:
        raise ValueError(f"{path} has no video stream")
    if not any(s["codec_type"] == "audio" for s in data["streams"]):
        raise ValueError(f"{path} has no audio stream")

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


def run(cmd: list[str], quiet: bool = True) -> None:
    base = ["ffmpeg", "-hide_banner", "-y", "-loglevel", "error"]
    if not quiet:
        base += ["-stats"]
    subprocess.run(base + cmd, check=True)


def extract_audio(src: Path, dst: Path) -> None:
    run(["-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dst)])
