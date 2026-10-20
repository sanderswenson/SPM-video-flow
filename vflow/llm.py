"""Claude editor pass: retakes and flubs, caption cleanup, hook, callouts, B-roll placement, post copy."""

from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, Field

from .config import Settings
from .plan import Plan
from .transcript import phrases

SYSTEM = """You are the editor cutting raw, single-take talking-head footage into a short-form social video \
(Reels / TikTok / Shorts). The speaker recorded straight through, so the transcript contains mistakes they \
meant to cut later. An automatic pass has already removed filler words and the most obvious repeated lines; \
those ranges are shown as [cut:reason ...] with their word indices.

You get the transcript as phrases; every word carries its index. Decide:

1. drops: word ranges to remove.
   - retake: a line said more than once. Keep the LAST complete, clean take; drop earlier ones. Retakes often \
start with slightly different words, and a speaker may re-record a whole stretch of several sentences.
   - false_start: an abandoned beginning ("So the - so what I mean is ...").
   - stutter: repeated words ("I I think").
   - off_script: talking to the camera or themselves ("wait, let me redo that", "is it recording?").
   - tangent: only when the style notes ask for a tighter cut, or a stretch clearly goes nowhere.
   Be conservative with content: when unsure whether something is a mistake, keep it. The result must read as \
one fluent piece; check every join.
2. restores: automatic [cut:...] ranges that removed something that is NOT said again later. Leave correct cuts alone.
3. fixes: caption text corrections for word ranges (the audio is untouched). Make spoken numbers read right: \
"fifty five hundred dollars" -> "$5,500"; "thirty two fifty" in a rent context -> "$3,250"; a transcription like \
"$32.50" that clearly means $3,250 -> "$3,250". Fix misheard words and casing. Keep the speaker's words otherwise. \
A fix may cover several words with a shorter text.
4. A hook. hook_text: a 3-8 word on-screen line for the first seconds that makes people stay (a number, a \
result, a tension), in the speaker's voice, no clickbait. hook_first_word/hook_last_word: the single strongest \
line (2-7 seconds of speech, from the kept words, usually the result or payoff) to cold-open with before the \
video starts from the top; -1 for both if nothing fits.
5. callouts: up to 4 short on-screen cards (max 5 words / 28 characters) at the moments that carry the numbers \
or the key claim, e.g. "$5,500 in repairs" at the words where that figure is said. Only where they add punch.
6. broll: if image files are listed, place each relevant one at the word where it should appear (at most once \
each, not in the first 3 seconds, at least 8 seconds apart). Skip images that don't fit. Empty list if none.
7. cta: a call to action for the last seconds, max 6 words, matching the content ("Follow for more rental \
math", "Comment 'walkthrough' for the checklist"), or the default given if it fits.
8. Post copy: a short hook title, an Instagram caption in the speaker's voice (1-3 short paragraphs, no \
hashtags inside), and 3-8 relevant hashtags.

Never reorder (the hook cold-open is the only exception and is handled for you) and never rewrite the audio: \
you can only remove word ranges and change caption text."""


class Range(BaseModel):
    first_word: int = Field(description="index of the first word in the range")
    last_word: int = Field(description="index of the last word in the range (inclusive)")


class DropRange(Range):
    reason: Literal["retake", "false_start", "stutter", "off_script", "tangent"]


class Fix(Range):
    text: str = Field(description="corrected caption text for this word range")


class Callout(Range):
    text: str = Field(description="on-screen card text, max 5 words")


class BrollCue(BaseModel):
    first_word: int = Field(description="word index where the image should appear")
    image: str = Field(description="one of the listed image file names, exactly")


class EditDecision(BaseModel):
    drops: list[DropRange]
    restores: list[Range]
    fixes: list[Fix]
    hook_text: str
    hook_first_word: int
    hook_last_word: int
    callouts: list[Callout]
    broll: list[BrollCue]
    cta: str
    title: str
    caption: str
    hashtags: list[str]


def available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def check_key(key: str) -> str | None:
    """Return an error message if the key doesn't work, else None."""
    import anthropic

    try:
        anthropic.Anthropic(api_key=key).models.list(limit=1)
    except anthropic.AuthenticationError:
        return "That API key was rejected."
    except anthropic.APIConnectionError:
        return "Couldn't reach the Anthropic API. Check your internet connection."
    except anthropic.APIStatusError as e:
        return f"API error {e.status_code}"
    return None


def render_transcript(plan: Plan) -> str:
    """Every word with its index, grouped into phrases; already-cut runs wrapped as [cut:reason ...]."""
    lines = []
    for n, group in enumerate(phrases(plan.words), 1):
        w0, w1 = plan.words[group[0]], plan.words[group[-1]]
        parts: list[str] = []
        open_reason: str | None = None
        for i in group:
            tok = f"{i}:{plan.words[i].text}"
            reason = None if plan.keep[i] else plan.reasons.get(i, "cut")
            if reason != open_reason:
                if open_reason is not None:
                    parts.append("]")
                if reason is not None:
                    parts.append(f"[cut:{reason}")
                open_reason = reason
            parts.append(tok)
        if open_reason is not None:
            parts.append("]")
        lines.append(f"P{n} [{w0.start:.1f}-{w1.end:.1f}s] {' '.join(parts)}")
    return "\n".join(lines)


def review(plan: Plan, s: Settings, broll_images: list[str] | None = None) -> EditDecision | None:
    import anthropic

    client = anthropic.Anthropic()
    kept_seconds = sum(w.end - w.start for w, k in zip(plan.words, plan.keep) if k)
    user = (
        f"Raw footage: {plan.duration:.0f}s; about {kept_seconds:.0f}s of speech currently kept.\n\n"
        "Transcript:\n" + render_transcript(plan)
    )
    user += "\n\nAvailable B-roll images: " + (", ".join(broll_images) if broll_images else "none")
    user += f"\nDefault call to action: {s.cta!r}" if s.cta else "\nNo call to action wanted: return an empty cta."
    if not s.hook:
        user += "\nNo cold open wanted: return -1 for the hook words (still write hook_text)."
    if not s.clean_transcript:
        user += "\nDo not correct captions: return an empty fixes list."
    if s.style_notes:
        user += f"\n\nStyle notes from the creator: {s.style_notes}"

    response = client.beta.messages.parse(
        model=s.llm_model,
        max_tokens=16000,
        system=SYSTEM,
        messages=[{"role": "user", "content": user}],
        output_format=EditDecision,
        output_config={"effort": s.llm_effort},
        # If the request is declined by a safety classifier, let the API retry it on a fallback model.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        return None
    return response.parsed_output


def _valid(plan: Plan, first: int, last: int) -> tuple[int, int] | None:
    n = len(plan.words)
    lo, hi = max(0, first), min(n - 1, last)
    return (lo, hi) if 0 <= lo <= hi < n else None


def apply(plan: Plan, decision: EditDecision, broll_images: list[str] | None = None) -> None:
    for r in decision.restores:
        if (rng := _valid(plan, r.first_word, r.last_word)) is not None:
            plan.restore(range(rng[0], rng[1] + 1))
    for d in decision.drops:
        if (rng := _valid(plan, d.first_word, d.last_word)) is not None:
            plan.drop(range(rng[0], rng[1] + 1), d.reason)

    for f in decision.fixes:
        if (rng := _valid(plan, f.first_word, f.last_word)) is None or not f.text.strip():
            continue
        lo, hi = rng
        tokens = f.text.split()
        if len(tokens) == hi - lo + 1:
            for i, tok in zip(range(lo, hi + 1), tokens):
                plan.words[i].display = tok
        else:
            # Several words become one caption token (or one word becomes several): show it on the
            # first word and fold the rest into it.
            plan.words[lo].display = f.text.strip()
            for i in range(lo + 1, hi + 1):
                plan.words[i].display = ""

    ov = plan.overlays
    if decision.hook_text.strip():
        ov["hook_text"] = decision.hook_text.strip()[:60]
    if decision.hook_first_word >= 0 and (rng := _valid(plan, decision.hook_first_word, decision.hook_last_word)):
        if rng[1] - rng[0] <= 30 and all(plan.keep[rng[0]:rng[1] + 1]):
            ov["hook_range"] = [rng[0], rng[1]]
    callouts = []
    for c in decision.callouts[:4]:
        if (rng := _valid(plan, c.first_word, c.last_word)) and c.text.strip():
            callouts.append({"first": rng[0], "last": rng[1], "text": c.text.strip()[:32]})
    ov["callouts"] = callouts
    allowed = set(broll_images or [])
    ov["broll"] = [{"word": b.first_word, "image": b.image} for b in decision.broll
                   if b.image in allowed and 0 <= b.first_word < len(plan.words)]
    ov["cta"] = decision.cta.strip()[:40]

    plan.post = {
        "title": decision.title,
        "caption": decision.caption,
        "hashtags": [h if h.startswith("#") else f"#{h}" for h in decision.hashtags],
    }
