"""Decide what to keep: turn a word transcript plus drop decisions into cut segments.

A plan is plain JSON on disk (work/<name>/plan.json) so it can be reviewed or hand-edited
before rendering: flip a word's "keep" and re-run `vflow render`.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import Settings
from .transcript import Word


@dataclass
class Plan:
    source: str
    duration: float
    fps: float
    words: list[Word]
    keep: list[bool]
    reasons: dict[int, str] = field(default_factory=dict)  # word index -> why it was dropped
    post: dict = field(default_factory=dict)  # title / caption / hashtags from the LLM pass

    def save(self, path: Path) -> None:
        data = {
            "source": self.source,
            "duration": self.duration,
            "fps": self.fps,
            "post": self.post,
            "words": [
                {**asdict(w), "keep": k, **({"reason": self.reasons[i]} if i in self.reasons else {})}
                for i, (w, k) in enumerate(zip(self.words, self.keep))
            ],
        }
        path.write_text(json.dumps(data, indent=1))

    @classmethod
    def load(cls, path: Path) -> "Plan":
        data = json.loads(path.read_text())
        words, keep, reasons = [], [], {}
        for i, w in enumerate(data["words"]):
            words.append(Word(start=w["start"], end=w["end"], text=w["text"], prob=w.get("prob", 1.0)))
            keep.append(bool(w.get("keep", True)))
            if "reason" in w:
                reasons[i] = w["reason"]
        return cls(data["source"], data["duration"], data["fps"], words, keep, reasons, data.get("post", {}))

    def drop(self, indices, reason: str) -> None:
        for i in indices:
            if self.keep[i]:
                self.keep[i] = False
                self.reasons[i] = reason


def drop_fillers(plan: Plan, fillers: list[str]) -> None:
    filler_set = {f.lower() for f in fillers}
    plan.drop([i for i, w in enumerate(plan.words) if w.norm in filler_set], "filler")


def segments(plan: Plan, s: Settings) -> list[tuple[float, float]]:
    """Source time ranges to keep, in order, snapped to frame boundaries."""
    words, keep = plan.words, plan.keep
    frame = 1.0 / plan.fps

    runs: list[list[int]] = []
    for i, w in enumerate(words):
        if not keep[i]:
            continue
        if runs:
            last = runs[-1][-1]
            # Continue the run only if nothing was dropped in between and the pause is short.
            if last == i - 1 and w.start - words[last].end <= s.max_pause:
                runs[-1].append(i)
                continue
        runs.append([i])

    out: list[tuple[float, float]] = []
    for run in runs:
        first, last = run[0], run[-1]
        start = words[first].start - s.pad_before
        end = words[last].end + s.pad_after
        # Padding must never reach into a neighbouring (possibly dropped) word.
        if first > 0:
            start = max(start, words[first - 1].end)
        if last + 1 < len(words):
            end = min(end, words[last + 1].start)
        start = max(0.0, math.floor(start / frame + 1e-6) * frame)
        end = min(plan.duration, math.ceil(end / frame - 1e-6) * frame)
        if end - start < s.min_segment:
            continue
        if out and start <= out[-1][1] + frame / 2:
            out[-1] = (out[-1][0], end)
        else:
            out.append((start, end))
    return [(round(a, 6), round(b, 6)) for a, b in out]


def remap(plan: Plan, segs: list[tuple[float, float]]) -> list[Word]:
    """Kept words with timestamps moved onto the edited timeline (for captions)."""
    out: list[Word] = []
    offsets, t = [], 0.0
    for a, b in segs:
        offsets.append(t)
        t += b - a
    j = 0
    for w, k in zip(plan.words, plan.keep):
        if not k:
            continue
        mid = (w.start + w.end) / 2
        while j < len(segs) and segs[j][1] < mid:
            j += 1
        if j == len(segs):
            break
        a, b = segs[j]
        if mid < a:
            continue  # word fell in a gap (e.g. an island shorter than min_segment)
        base = offsets[j] - a
        out.append(Word(round(max(w.start, a) + base, 3), round(min(w.end, b) + base, 3), w.text, w.prob))
    return out


def summary(plan: Plan, segs: list[tuple[float, float]]) -> str:
    kept = sum(b - a for a, b in segs)
    by_reason: dict[str, int] = {}
    for r in plan.reasons.values():
        by_reason[r] = by_reason.get(r, 0) + 1
    reasons = ", ".join(f"{n} {r}" for r, n in sorted(by_reason.items())) or "none"
    return (
        f"{plan.duration:.1f}s raw -> {kept:.1f}s edited ({len(segs)} cuts kept, "
        f"{100 * (1 - kept / plan.duration):.0f}% removed; dropped words: {reasons})"
    )
