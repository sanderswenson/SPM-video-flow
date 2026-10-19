"""Settings for a pipeline run. Defaults live here; a vflow.toml and CLI flags override them."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

ASPECTS = {
    "9:16": (1080, 1920),
    "4:5": (1080, 1350),
    "1:1": (1080, 1080),
    "16:9": (1920, 1080),
}


@dataclass
class Settings:
    # Transcription
    whisper_model: str = "small.en"
    whisper_device: str = "cpu"  # "cuda" for NVIDIA GPUs with CUDA 12 + cuDNN installed
    language: str | None = "en"

    # Cutting
    max_pause: float = 0.35  # longest silence left inside the edit (seconds)
    pad_before: float = 0.06  # breathing room kept before a word
    pad_after: float = 0.12  # and after it
    min_segment: float = 0.25  # drop kept islands shorter than this
    fillers: list[str] = field(
        default_factory=lambda: ["um", "uh", "uhm", "umm", "erm", "er", "ah", "hmm", "mm"]
    )

    # LLM pass (retakes, flubs, post copy). Skipped when no API key is set.
    use_llm: bool = True
    llm_model: str = "claude-opus-5-5"
    llm_effort: str = "medium"
    style_notes: str = ""  # free-form guidance, e.g. "keep it under 60s, punchy"

    # Framing
    aspect: str = "9:16"  # 9:16 | 4:5 | 1:1 | 16:9 | source
    crop_x: float = 0.5  # horizontal crop center, 0 = left edge, 1 = right edge
    crop_y: float = 0.5

    # Captions
    captions: bool = True
    caption_font: str = "Inter"
    caption_size: float = 0.05  # fraction of output height
    caption_words: int = 3  # max words on screen at once
    caption_max_chars: int = 18
    caption_y: float = 0.70  # vertical position of caption baseline, fraction of height
    caption_color: str = "FFFFFF"  # RGB hex
    caption_highlight: str = "FFE14D"  # active word, RGB hex
    caption_uppercase: bool = True

    # Output
    loudness: float = -14.0  # integrated LUFS target (IG/TikTok/YT normalize around here)
    crf: int = 20
    preset: str = "medium"
    fps: float | None = None  # None = keep source fps

    def update(self, values: dict) -> None:
        known = {f.name for f in fields(self)}
        for key, value in values.items():
            if value is None:
                continue
            if key not in known:
                raise ValueError(f"unknown setting: {key}")
            setattr(self, key, value)

    @classmethod
    def load(cls, path: Path | None) -> "Settings":
        s = cls()
        if path is None and Path("vflow.toml").exists():
            path = Path("vflow.toml")
        if path is not None:
            with open(path, "rb") as f:
                s.update(tomllib.load(f))
        return s

    def output_size(self, src_w: int, src_h: int) -> tuple[int, int]:
        if self.aspect == "source":
            return src_w - src_w % 2, src_h - src_h % 2
        if self.aspect not in ASPECTS:
            raise ValueError(f"aspect must be one of {', '.join(ASPECTS)} or 'source'")
        return ASPECTS[self.aspect]
