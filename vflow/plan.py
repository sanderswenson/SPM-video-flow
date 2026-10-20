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
    # On-screen extras decided by the editor pass:
    #   hook_text: str, hook_range: [first, last] word indices for a cold open,
    #   callouts: [{"first": i, "last": j, "text": str}], broll: [{"word": i, "image": name}], cta: str
    overlays: dict = field(default_factory=dict)

    def save(self, path: Path) -> None:
        data = {
            "source": self.source,
            "duration": self.duration,
            "fps": self.fps,
            "post": self.post,
            "overlays": self.overlays,
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
            words.append(Word(start=w["start"], end=w["end"], text=w["text"], prob=w.get("prob", 1.0),
                              display=w.get("display")))
            keep.append(bool(w.get("keep", True)))
            if "reason" in w:
                reasons[i] = w["reason"]
        return cls(data["source"], data["duration"], data["fps"], words, keep, reasons,
                   data.get("post", {}), data.get("overlays", {}))

    def drop(self, indices, reason: str) -> None:
        for i in indices:
            if self.keep[i]:
                self.keep[i] = False
                self.reasons[i] = reason

    def restore(self, indices) -> None:
        for i in indices:
            self.keep[i] = True
            self.reasons.pop(i, None)


def drop_fillers(plan: Plan, fillers: list[str]) -> None:
    filler_set = {f.lower() for f in fillers}
    plan.drop([i for i, w in enumerate(plan.words) if w.norm in filler_set], "filler")


# --- repeated lines ----------------------------------------------------------------------

LEAD_INS = {"so", "and", "but", "okay", "ok", "alright", "now", "then", "yeah", "well", "or", "like"}


def _prev_kept(plan: Plan, i: int) -> int | None:
    for p in range(i - 1, -1, -1):
        if plan.keep[p]:
            return p
    return None


def _is_anchor(plan: Plan, i: int) -> bool:
    """Does word i start a phrase: first word, after a pause, after punctuation, or after a cut?"""
    p = _prev_kept(plan, i)
    if p is None or p != i - 1:
        return True
    w, pw = plan.words[i], plan.words[p]
    return w.start - pw.end > 0.3 or pw.text[-1:] in ".?!,;:"


def _block_overlap(a: list[str], b: list[str]) -> float:
    """Share of a's bigrams that also occur in b (how much of a stretch gets re-said in b)."""
    if len(a) < 2:
        return 0.0
    grams_b = {(b[k], b[k + 1]) for k in range(len(b) - 1)}
    hits = sum((a[k], a[k + 1]) in grams_b for k in range(len(a) - 1))
    return hits / (len(a) - 1)


def drop_repeats(plan: Plan, aggressive: bool = False, max_span_words: int = 120,
                 max_span_seconds: float = 75.0) -> int:
    """Cut earlier attempts at a line that the speaker said again, keeping the last take.

    Looks for a run of words that is said again later: a restart of the same sentence, a chain of
    false starts ("so our - so our - so our work is"), or a whole block re-recorded. The earlier
    occurrence is dropped up to where the later one begins. The checks are deliberately strict so
    rhetorical repetition survives; `aggressive` loosens them for runs without an editor pass.
    Returns the number of words dropped.
    """
    words = plan.words
    norm = [w.norm for w in words]
    dropped = 0

    for _ in range(4):  # a drop can expose a new restart boundary; iterate to a fixed point
        progress = False
        kept = [i for i in range(len(words)) if plan.keep[i]]
        pos = 0
        while pos < len(kept):
            i = kept[pos]
            if not norm[i]:
                pos += 1
                continue
            found = None
            for q in range(pos + 1, len(kept)):
                j = kept[q]
                span = q - pos
                if span > max_span_words or words[j].start - words[i].start > max_span_seconds:
                    break
                if norm[j] != norm[i]:
                    continue
                run = 0
                while (run < span and q + run < len(kept) and norm[kept[pos + run]]
                       and norm[kept[pos + run]] == norm[kept[q + run]]):
                    run += 1
                j_anchor = _is_anchor(plan, j) or (norm[kept[q - 1]] in LEAD_INS and _is_anchor(plan, kept[q - 1]))
                i_anchor = _is_anchor(plan, i)
                ok = False
                reason = "retake"
                if run >= 5:
                    ok = True  # a long run said twice is a restart wherever it begins
                elif run == 4 and j_anchor and i_anchor:
                    ok = True
                elif run == 3 and j_anchor and i_anchor and span <= 40:
                    ok = True  # short restarts begin and resume at phrase boundaries
                elif run == 2 and span <= 6:
                    # A short false start: accept when the same opening recurs several times in a row
                    # (a chain of restarts), or in aggressive mode when it starts a phrase.
                    pair = (norm[i], norm[kept[pos + 1]])
                    repeats = sum(
                        1 for r in range(pos, min(len(kept) - 1, pos + 30))
                        if (norm[kept[r]], norm[kept[r + 1]]) == pair
                    )
                    ok = repeats >= 3 or (aggressive and _is_anchor(plan, i))
                    reason = "false_start"
                elif run == 1 and span == 1 and not norm[i].isdigit():
                    ok = True
                    reason = "stutter"
                if not ok:
                    continue
                if span > 25:
                    # A long stretch is only a retake if most of it gets said again afterwards.
                    before = [norm[k] for k in kept[pos:q] if norm[k]]
                    after = [norm[k] for k in kept[q:q + span + 10] if norm[k]]
                    if _block_overlap(before, after) < 0.45:
                        continue
                found = (q, reason)
                break
            if found is None:
                pos += 1
                continue
            q, reason = found
            # The restart may begin a little before the matching run ("long-term tenants with ..." vs
            # "the a long-term tenant with ..."): walk both sides back over similar words to the phrase start.
            lo, hi = pos, q
            while lo > 0 and hi - 1 > lo and not _is_anchor(plan, kept[lo]):
                a, b = norm[kept[lo - 1]], norm[kept[hi - 1]]
                if a and b and (a == b or (len(a) >= 4 and (a.startswith(b) or b.startswith(a)))):
                    lo, hi = lo - 1, hi - 1
                else:
                    break
            # Debris between the earlier take and the restart ("the a") goes too.
            plan.drop(range(kept[lo], kept[hi]), reason)
            dropped += hi - lo
            progress = True
            kept = [k for k in range(len(words)) if plan.keep[k]]
            pos = lo  # the restart now sits at this position; re-check it against what follows
        if not progress:
            break

    # Tiny leftovers wedged between two cuts ("So we put") are almost always debris.
    kept = [i for i in range(len(words)) if plan.keep[i]]
    islands: list[list[int]] = []
    for i in kept:
        if islands and islands[-1][-1] == i - 1:
            islands[-1].append(i)
        else:
            islands.append([i])
    for isl in islands:
        first, last = isl[0], isl[-1]
        before = plan.reasons.get(first - 1) if first > 0 else None
        after = plan.reasons.get(last + 1) if last + 1 < len(words) else None
        if (len(isl) <= 3 and before in ("retake", "false_start") and after in ("retake", "false_start")
                and words[last].text[-1:] not in ".?!"):
            plan.drop(isl, "retake")
            dropped += len(isl)
    return dropped


# --- segments and shots ------------------------------------------------------------------

@dataclass
class Shot:
    start: float  # source time range, frame-snapped
    end: float
    kind: str = "main"  # "hook" for a cold-open clip that also appears later in the main body
    source: str | None = None  # another file to cut this shot from (a filmed hook clip); None = the plan's source

    @property
    def duration(self) -> float:
        return self.end - self.start


def _snap_range(plan: Plan, first: int, last: int, s: Settings) -> tuple[float, float]:
    words = plan.words
    frame = 1.0 / plan.fps
    start = words[first].start - s.pad_before
    end = words[last].end + s.pad_after
    # Padding must never reach into a neighbouring (possibly dropped) word.
    if first > 0:
        start = max(start, words[first - 1].end)
    if last + 1 < len(words):
        end = min(end, words[last + 1].start)
    start = max(0.0, math.floor(start / frame + 1e-6) * frame)
    end = min(plan.duration, math.ceil(end / frame - 1e-6) * frame)
    return start, end


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
        start, end = _snap_range(plan, run[0], run[-1], s)
        if end - start < s.min_segment:
            continue
        if out and start <= out[-1][1] + frame / 2:
            out[-1] = (out[-1][0], end)
        else:
            out.append((start, end))
    return [(round(a, 6), round(b, 6)) for a, b in out]


def hook_segment(plan: Plan, s: Settings, max_seconds: float = 8.0) -> tuple[float, float] | None:
    rng = plan.overlays.get("hook_range")
    if not s.hook or not rng:
        return None
    first, last = int(rng[0]), int(rng[1])
    if not (0 <= first <= last < len(plan.words)) or not all(plan.keep[first:last + 1]):
        return None
    start, end = _snap_range(plan, first, last, s)
    if end - start < 0.8 or end - start > max_seconds:
        return None
    return round(start, 6), round(end, 6)


def shots(plan: Plan, s: Settings) -> list[Shot]:
    """The edit in output order: an optional cold-open hook, then the kept segments."""
    out = [Shot(a, b) for a, b in segments(plan, s)]
    hook = hook_segment(plan, s)
    if hook is not None and len(out) > 1:
        out.insert(0, Shot(hook[0], hook[1], "hook"))
    return out


def _as_shots(segs) -> list[Shot]:
    return [x if isinstance(x, Shot) else Shot(x[0], x[1]) for x in segs]


def remap(plan: Plan, segs) -> list[Word]:
    """Kept words with timestamps moved onto the edited timeline, in output order (for captions)."""
    out: list[Word] = []
    t = 0.0
    kept = [w for w, k in zip(plan.words, plan.keep) if k]
    for shot in _as_shots(segs):
        a, b = shot.start, shot.end
        for w in kept:
            mid = (w.start + w.end) / 2
            if a <= mid <= b:
                base = t - a
                out.append(Word(round(max(w.start, a) + base, 3), round(min(w.end, b) + base, 3),
                                w.text, w.prob, w.display))
        t += b - a
    return out


def word_times(plan: Plan, segs) -> dict[int, tuple[float, float]]:
    """Output-timeline interval of each kept word in the main body (hook repeats are ignored)."""
    out: dict[int, tuple[float, float]] = {}
    t = 0.0
    for shot in _as_shots(segs):
        a, b = shot.start, shot.end
        if shot.kind == "main":
            for i, (w, k) in enumerate(zip(plan.words, plan.keep)):
                mid = (w.start + w.end) / 2
                if k and a <= mid <= b:
                    out[i] = (round(max(w.start, a) + t - a, 3), round(min(w.end, b) + t - a, 3))
        t += b - a
    return out


def total_duration(segs) -> float:
    return sum(sh.end - sh.start for sh in _as_shots(segs))


def summary(plan: Plan, segs) -> str:
    kept = total_duration(segs)
    by_reason: dict[str, int] = {}
    for r in plan.reasons.values():
        by_reason[r] = by_reason.get(r, 0) + 1
    reasons = ", ".join(f"{n} {r}" for r, n in sorted(by_reason.items())) or "none"
    return (
        f"{plan.duration:.1f}s raw -> {kept:.1f}s edited ({len(_as_shots(segs))} cuts kept, "
        f"{100 * (1 - kept / plan.duration):.0f}% removed; dropped words: {reasons})"
    )
