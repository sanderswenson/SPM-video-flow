"""Word-level transcripts: the data model, Whisper transcription, and phrase grouping."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class Word:
    start: float
    end: float
    text: str
    prob: float = 1.0

    @property
    def norm(self) -> str:
        return re.sub(r"[^\w']", "", self.text.lower())


def save(words: list[Word], path: Path) -> None:
    path.write_text(json.dumps([asdict(w) for w in words], indent=1))


def load(path: Path) -> list[Word]:
    return [Word(**w) for w in json.loads(path.read_text())]


def transcribe(audio: Path, model_name: str, language: str | None) -> list[Word]:
    from faster_whisper import WhisperModel

    model = WhisperModel(model_name, device="auto", compute_type="auto")
    segments, _ = model.transcribe(
        str(audio),
        language=language,
        word_timestamps=True,
        vad_filter=False,  # we want to see every word, including flubs, so the cutter can decide
        # Whisper tends to "clean up" disfluencies; this prompt nudges it to keep them so they can be cut.
        initial_prompt="Umm, so, uh, I was like, you know... hmm, okay. Let me, let me start over.",
        condition_on_previous_text=False,
    )
    words: list[Word] = []
    for seg in segments:
        for w in seg.words or []:
            text = w.word.strip()
            if text:
                words.append(Word(start=round(w.start, 3), end=round(w.end, 3), text=text, prob=round(w.probability, 3)))
    return words


def phrases(words: list[Word], gap: float = 0.45, max_words: int = 25) -> list[list[int]]:
    """Group word indices into short phrases, breaking on pauses and sentence ends.

    Phrases are the unit the LLM keeps or drops, so they should be small enough that a
    false start ("So the thing — so the main thing is") lands in its own phrase.
    """
    out: list[list[int]] = []
    cur: list[int] = []
    for i, w in enumerate(words):
        if cur:
            prev = words[cur[-1]]
            if w.start - prev.end > gap or prev.text[-1:] in ".?!" or len(cur) >= max_words:
                out.append(cur)
                cur = []
        cur.append(i)
    if cur:
        out.append(cur)
    return out
