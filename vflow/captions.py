"""Burned-in, word-by-word highlighted captions as an ASS subtitle file."""

from __future__ import annotations

from pathlib import Path

from .config import Settings
from .transcript import Word


def _ass_color(rgb: str) -> str:
    rgb = rgb.lstrip("#")
    r, g, b = rgb[0:2], rgb[2:4], rgb[4:6]
    return f"&H00{b}{g}{r}".upper()


def _ts(t: float) -> str:
    cs = max(0, round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    sec, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{sec:02d}.{cs:02d}"


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")")


def group_words(words: list[Word], max_words: int, max_chars: int, gap: float = 0.4) -> list[list[Word]]:
    groups: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        if cur:
            chars = sum(len(x.text) + 1 for x in cur) + len(w.text)
            if (
                len(cur) >= max_words
                or chars > max_chars
                or w.start - cur[-1].end > gap
                or cur[-1].text[-1:] in ".?!,"
            ):
                groups.append(cur)
                cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return groups


def build(words: list[Word], size: tuple[int, int], s: Settings) -> str:
    w, h = size
    font_px = round(h * s.caption_size) if w < h else round(h * s.caption_size * 1.3)
    outline = max(2, round(font_px * 0.09))
    margin_v = round(h * (1 - s.caption_y))
    base, hi = _ass_color(s.caption_color), _ass_color(s.caption_highlight)

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Word,{s.caption_font},{font_px},{base},{base},&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,{outline},0,2,{round(w * 0.08)},{round(w * 0.08)},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    groups = group_words(words, s.caption_words, s.caption_max_chars)
    for gi, group in enumerate(groups):
        next_start = groups[gi + 1][0].start if gi + 1 < len(groups) else group[-1].end + 0.5
        # Hold the last caption on screen through short pauses instead of flashing off.
        group_end = min(next_start, group[-1].end + 0.6)
        texts = [_escape(x.text.upper() if s.caption_uppercase else x.text) for x in group]
        for wi, word in enumerate(group):
            start = word.start
            end = group[wi + 1].start if wi + 1 < len(group) else group_end
            if end <= start:
                continue
            parts = [
                f"{{\\c{hi}&\\fscx108\\fscy108}}{t}{{\\c{base}&\\fscx100\\fscy100}}" if j == wi else t
                for j, t in enumerate(texts)
            ]
            events.append(f"Dialogue: 0,{_ts(start)},{_ts(end)},Word,,0,0,0,,{' '.join(parts)}")
    return header + "\n".join(events) + "\n"


def write(words: list[Word], size: tuple[int, int], s: Settings, path: Path) -> None:
    path.write_text(build(words, size, s), encoding="utf-8")
