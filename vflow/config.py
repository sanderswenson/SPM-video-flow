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
    cut_repeats: bool = True  # drop earlier takes of repeated lines (runs with or without the editor pass)

    # LLM pass (retakes, flubs, caption cleanup, hook, callouts, post copy). Skipped when no API key is set.
    use_llm: bool = True
    llm_model: str = "claude-opus-5-5"
    llm_effort: str = "high"
    style_notes: str = ""  # free-form guidance, e.g. "keep it under 60s, punchy"
    clean_transcript: bool = True  # let the editor pass fix numbers, spelling and grammar in captions

    # Framing
    aspect: str = "9:16"  # 9:16 | 4:5 | 1:1 | 16:9 | source
    framing: str = "auto"  # auto = find the speaker and keep them in frame; manual = crop_x / crop_y
    crop_x: float = 0.5  # manual horizontal crop position, 0 = left edge, 1 = right edge
    crop_y: float = 0.5
    punch_in: float = 1.08  # zoom alternated at hard cuts so jump cuts read as intentional; 1.0 = off

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

    # Extras
    hook: bool = True  # cold-open with the strongest line and show a hook title over the first seconds
    callouts: bool = True  # big on-screen number/keyword cards where the editor pass suggests them
    broll_dir: str = ""  # folder of images to cut away to; the editor pass decides where they go
    broll_seconds: float = 2.5
    stock_provider: str = "pexels"  # pexels | unsplash: stock photos for B-roll when the folder is empty
    stock_api_key: str = ""  # or set VFLOW_STOCK_API_KEY / PEXELS_API_KEY / UNSPLASH_ACCESS_KEY; the app uses the keyring
    music: str = ""  # audio file (or a folder of them) for a background bed, ducked under speech
    music_db: float = -20.0  # music level relative to the speech
    cta: str = "Follow for more"  # end card text; empty = no end card (the editor pass may tailor it)
    cta_seconds: float = 3.0

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
