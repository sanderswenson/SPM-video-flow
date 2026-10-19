"""Claude pass: find retakes, false starts and off-script chatter; write the post copy."""

from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, Field

from .config import Settings
from .plan import Plan
from .transcript import phrases

SYSTEM = """You are an editor cutting raw, single-take talking-head footage into a short-form social video \
(Reels / TikTok / Shorts). The speaker recorded straight through, so the transcript contains mistakes \
they meant to cut later.

You get the transcript as phrases; every word carries its index. Decide which word ranges to remove:
- retake: the speaker said a line, then said it again. Keep the LAST complete, clean take, drop earlier ones.
- false_start: an abandoned beginning ("So the — so what I mean is ...") — drop the abandoned part.
- stutter: repeated words ("I I I think") — drop the extra copies.
- off_script: talking to the camera operator or themselves ("wait, let me redo that", "is it recording?", "okay cut").
- tangent: only when the style notes ask for a tighter cut, or a stretch clearly goes nowhere.

Rules:
- Never reorder, never rewrite. You can only remove word ranges.
- Be conservative with content: when unsure whether something is a mistake, keep it.
- The result must read as one fluent piece. Check that the sentence still makes sense across every cut.
- Filler words (um, uh) were already removed; ignore them.

Also write the post copy for the finished video: a short hook title, an Instagram caption in the \
speaker's voice (1-3 short paragraphs, no hashtags inside), and 3-8 relevant hashtags."""


class DropRange(BaseModel):
    first_word: int = Field(description="index of the first word to remove")
    last_word: int = Field(description="index of the last word to remove (inclusive)")
    reason: Literal["retake", "false_start", "stutter", "off_script", "tangent"]


class EditDecision(BaseModel):
    drops: list[DropRange]
    title: str
    caption: str
    hashtags: list[str]


def available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def render_transcript(plan: Plan) -> str:
    kept = [i for i, k in enumerate(plan.keep) if k]
    sub = [plan.words[i] for i in kept]
    lines = []
    for n, group in enumerate(phrases(sub), 1):
        idx = [kept[g] for g in group]
        w0, w1 = plan.words[idx[0]], plan.words[idx[-1]]
        body = " ".join(f"{i}:{plan.words[i].text}" for i in idx)
        lines.append(f"P{n} [{w0.start:.1f}-{w1.end:.1f}s] {body}")
    return "\n".join(lines)


def review(plan: Plan, s: Settings) -> EditDecision | None:
    import anthropic

    client = anthropic.Anthropic()
    user = "Transcript:\n" + render_transcript(plan)
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
        print(f"  ! LLM pass returned no edit (stop_reason={response.stop_reason}); keeping silence/filler cuts only")
        return None
    return response.parsed_output


def apply(plan: Plan, decision: EditDecision) -> None:
    n = len(plan.words)
    for d in decision.drops:
        lo, hi = max(0, d.first_word), min(n - 1, d.last_word)
        if lo <= hi:
            plan.drop(range(lo, hi + 1), d.reason)
    plan.post = {
        "title": decision.title,
        "caption": decision.caption,
        "hashtags": [h if h.startswith("#") else f"#{h}" for h in decision.hashtags],
    }
