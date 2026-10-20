"""Burned-in text as one ASS subtitle file: word-by-word highlighted captions, plus the hook
title, callout cards and the end-card call to action when the plan has them."""

from __future__ import annotations

from pathlib import Path

from .config import Settings
from .transcript import Word


def _ass_color(rgb: str, alpha: int = 0) -> str:
    rgb = rgb.lstrip("#")
    r, g, b = rgb[0:2], rgb[2:4], rgb[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def _ts(t: float) -> str:
    cs = max(0, round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    sec, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{sec:02d}.{cs:02d}"


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", "\\N")


def _merge_hidden(words: list[Word]) -> list[Word]:
    """Caption-ready words: fold in words the cleanup pass hid (empty display text) and
    capitalize the first word of each sentence, since a cut can start one mid-sentence."""
    out: list[Word] = []
    for w in words:
        if not w.shown.strip():
            if out:
                p = out[-1]
                out[-1] = Word(p.start, max(p.end, w.end), p.text, p.prob, p.display)
            continue
        shown = w.shown
        if not out or out[-1].shown[-1:] in ".?!":
            shown = shown[0].upper() + shown[1:]
        out.append(Word(w.start, w.end, w.text, w.prob, shown if shown != w.text else None))
    return out


def group_words(words: list[Word], max_words: int, max_chars: int, gap: float = 0.4) -> list[list[Word]]:
    groups: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        if cur:
            chars = sum(len(x.shown) + 1 for x in cur) + len(w.shown)
            if (
                len(cur) >= max_words
                or chars > max_chars
                or w.start - cur[-1].end > gap
                or cur[-1].shown[-1:] in ".?!,"
            ):
                groups.append(cur)
                cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return groups


def build(words: list[Word], size: tuple[int, int], s: Settings, overlays: dict | None = None) -> str:
    """overlays: {"hook": (end, text), "callouts": [(start, end, text)], "cta": (start, end, text)}"""
    overlays = overlays or {}
    w, h = size
    portrait = w < h
    font_px = round(h * s.caption_size) if portrait else round(h * s.caption_size * 1.3)
    outline = max(2, round(font_px * 0.09))
    margin_v = round(h * (1 - s.caption_y))
    base, hi = _ass_color(s.caption_color), _ass_color(s.caption_highlight)
    side = round(w * 0.08)
    hook_px = round(h * (0.058 if portrait else 0.075))
    call_px = round(h * (0.046 if portrait else 0.06))
    cta_px = round(h * (0.042 if portrait else 0.055))

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Word,{s.caption_font},{font_px},{base},{base},&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,{outline},0,2,{side},{side},{margin_v},1
Style: Hook,{s.caption_font},{hook_px},{hi},{hi},&H00000000,&H90000000,-1,0,0,0,100,100,0,0,1,{outline + 2},2,8,{side},{side},{round(h * 0.11)},1
Style: Callout,{s.caption_font},{call_px},{hi},{hi},&H99000000,&H00000000,-1,0,0,0,100,100,0,0,3,{round(call_px * 0.4)},0,8,{side},{side},{round(h * 0.13)},1
Style: CTA,{s.caption_font},{cta_px},{_ass_color("1A1A1A")},{_ass_color("1A1A1A")},{hi},&H00000000,-1,0,0,0,100,100,0,0,3,{round(cta_px * 0.5)},0,8,{side},{side},{round(h * 0.13)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    groups = group_words(_merge_hidden(words), s.caption_words, s.caption_max_chars)
    for gi, group in enumerate(groups):
        next_start = groups[gi + 1][0].start if gi + 1 < len(groups) else group[-1].end + 0.5
        # Hold the last caption on screen through short pauses instead of flashing off.
        group_end = min(next_start, group[-1].end + 0.6)
        texts = [_escape(x.shown.upper() if s.caption_uppercase else x.shown) for x in group]
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

    hook = overlays.get("hook")
    if hook and hook[1].strip():
        text = hook[1].upper() if s.caption_uppercase else hook[1]
        events.append(f"Dialogue: 1,{_ts(0)},{_ts(hook[0])},Hook,,0,0,0,,{{\\fad(120,300)}}{_escape(text)}")
    for start, end, text in overlays.get("callouts", []):
        if end > start and text.strip():
            events.append(f"Dialogue: 1,{_ts(start)},{_ts(end)},Callout,,0,0,0,,{{\\fad(150,200)}}{_escape(text)}")
    cta = overlays.get("cta")
    if cta and cta[2].strip() and cta[1] > cta[0]:
        events.append(f"Dialogue: 1,{_ts(cta[0])},{_ts(cta[1])},CTA,,0,0,0,,{{\\fad(300,0)}}{_escape(cta[2])}")
    return header + "\n".join(events) + "\n"


def write(words: list[Word], size: tuple[int, int], s: Settings, path: Path, overlays: dict | None = None) -> None:
    path.write_text(build(words, size, s, overlays), encoding="utf-8")
