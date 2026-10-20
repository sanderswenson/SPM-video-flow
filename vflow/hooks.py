"""Hook production: a catalog of hook patterns matched against what a clip actually says, turned into a
hook sheet (lines to film, each with an on-screen title), and rendered as one video per hook.

Two tiers, like the rest of vflow: the catalog match and a first draft of every line work locally from the
transcript (the "signals" below); with an API key the Claude pass writes the lines properly from the shortlist.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections import Counter
from dataclasses import asdict, dataclass, field
from importlib import resources
from pathlib import Path

from pydantic import BaseModel, Field

from . import llm
from .config import Settings
from .plan import Plan
from .transcript import phrases

SIGNALS = frozenset({
    "money", "percent", "number", "count", "timeframe", "before_after", "result", "cost", "mistake", "contrast",
    "list", "process", "question", "audience", "place", "proof", "secret", "comparison", "story", "how_to",
    "emotion", "topic",
})
KNOWN_SLOTS = frozenset({
    "money", "money_before", "money_after", "timeframe", "number", "percent", "count", "audience", "Audience",
    "audience_singular", "place", "topic", "Topic", "subject", "result",
})
DELIVERIES = ("spoken", "text", "either")
# How much a signal is worth as the basis of a hook: a before/after pair carries a video on its own,
# an audience word or the topic only shapes the wording.
SIGNAL_WEIGHT = {
    "before_after": 2.5, "money": 1.2, "percent": 1.2, "cost": 1.0, "result": 1.0, "timeframe": 1.0, "count": 0.8,
    "mistake": 0.8, "secret": 0.6, "comparison": 0.6, "audience": 0.6, "place": 0.6, "contrast": 0.5, "story": 0.5,
    "process": 0.5, "list": 0.5, "how_to": 0.5, "proof": 0.5, "number": 0.4, "emotion": 0.4, "question": 0.3,
    "topic": 0.2,
}


# --- catalog -------------------------------------------------------------------------------------

@dataclass
class Pattern:
    id: str
    family: str
    name: str
    template: str
    example: str = ""
    needs: list[str] = field(default_factory=list)
    wants: list[str] = field(default_factory=list)
    avoid: list[str] = field(default_factory=list)
    delivery: str = "spoken"
    footage_ok: bool = False
    film: str = ""

    @property
    def slots(self) -> list[str]:
        return re.findall(r"\{(\w+)\}", self.template)

    @property
    def free_slots(self) -> list[str]:
        """Slots vflow can't fill from the clip; the creator completes them on the sheet."""
        return [x for x in self.slots if x not in KNOWN_SLOTS]


def parse_catalog(text: str, origin: str = "catalog") -> list[Pattern]:
    data = tomllib.loads(text)
    out: list[Pattern] = []
    seen: set[str] = set()
    for k, raw in enumerate(data.get("hook", []), 1):
        try:
            p = Pattern(**raw)
        except TypeError as e:
            raise ValueError(f"{origin}: hook #{k}: {e}") from None
        where = f"{origin}: hook {p.id!r}"
        if not (p.id and p.family and p.name and p.template):
            raise ValueError(f"{where}: id, family, name and template are required")
        if p.id in seen:
            raise ValueError(f"{where}: duplicate id")
        for key in ("needs", "wants", "avoid"):
            bad = set(getattr(p, key)) - SIGNALS
            if bad:
                raise ValueError(f"{where}: unknown signal(s) in {key}: {', '.join(sorted(bad))}")
        if p.delivery not in DELIVERIES:
            raise ValueError(f"{where}: delivery must be one of {', '.join(DELIVERIES)}")
        seen.add(p.id)
        out.append(p)
    return out


def load_catalog(extra: str | Path | None = None) -> list[Pattern]:
    """The bundled catalog, plus the creator's own file if configured (same ids replace bundled ones)."""
    text = resources.files("vflow").joinpath("data/hooks.toml").read_text(encoding="utf-8")
    patterns = {p.id: p for p in parse_catalog(text, "hooks.toml")}
    if extra and Path(extra).is_file():
        for p in parse_catalog(Path(extra).read_text(encoding="utf-8"), Path(extra).name):
            patterns[p.id] = p
    return list(patterns.values())


# --- spoken numbers ------------------------------------------------------------------------------

_UNITS = {w: n for n, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen".split())}
_TENS = {w: 10 * (n + 2) for n, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split())}
_SCALES = {"hundred": 100, "thousand": 1_000, "million": 1_000_000, "billion": 1_000_000_000}
_NUMBER_WORDS = set(_UNITS) | set(_TENS) | set(_SCALES) | {"and", "a"}
_MONEY_WORDS = {"dollars", "dollar", "bucks"}


def _letters(tok: str) -> str:
    return re.sub(r"[^a-z]", "", tok.lower())


def _words_to_number(run: list[str]) -> int | None:
    total = current = 0
    seen = False
    for tok in run:
        if tok in _UNITS:
            current += _UNITS[tok]
        elif tok in _TENS:
            current += _TENS[tok]
        elif tok == "hundred":
            current = max(current, 1) * 100
        elif tok in _SCALES:
            total += max(current, 1) * _SCALES[tok]
            current = 0
        elif tok in ("and", "a"):
            continue
        else:
            return None
        seen = True
    return total + current if seen else None


def merge_numbers(toks: list[str]) -> list[tuple[list[int], str]]:
    """Rewrite spoken figures, keeping track of which input tokens each output token came from:
    'fifty five hundred dollars' -> '$5,500', 'eighteen months' -> '18 months', 'ten percent' -> '10%'."""
    out: list[tuple[list[int], str]] = []
    i = 0
    while i < len(toks):
        # A run may not start with "and", nor with "a" unless it is "a hundred" / "a thousand".
        first = _letters(toks[i])
        if first == "and" or (first == "a" and not (i + 1 < len(toks) and _letters(toks[i + 1]) in _SCALES)):
            out.append(([i], toks[i]))
            i += 1
            continue
        j = i
        while j < len(toks) and _letters(toks[j]) in _NUMBER_WORDS and not (j > i and re.search(r"[.?!,;:]$", toks[j - 1])):
            j += 1
        while j > i and _letters(toks[j - 1]) in ("and", "a"):
            j -= 1
        run = [_letters(t) for t in toks[i:j]]
        value = None
        if run and not (len(run) == 1 and run[0] in ("one", "a", "zero")):
            value = _words_to_number(run)
        if value is None:
            out.append(([i], toks[i]))
            i += 1
            continue
        tail = re.search(r"[^\w]*$", toks[j - 1]).group(0)  # punctuation after the last number word
        text, used = f"{value:,}", list(range(i, j))
        if not tail and j < len(toks):
            nxt = _letters(toks[j])
            if nxt in _MONEY_WORDS or nxt == "percent":
                text = f"${value:,}" if nxt in _MONEY_WORDS else f"{value:,}%"
                tail = re.search(r"[^\w]*$", toks[j]).group(0)
                used.append(j)
                j += 1
        out.append((used, text + tail))
        i = j
    return out


def spoken_numbers(text: str) -> str:
    return " ".join(t for _, t in merge_numbers(text.split()))


# --- signals: what the clip has to work with ------------------------------------------------------

_ROLES = [
    "landlord", "owner", "homeowner", "investor", "tenant", "renter", "agent", "realtor", "broker", "buyer", "seller",
    "flipper", "host", "founder", "entrepreneur", "operator", "client", "customer", "manager", "contractor",
    "developer", "designer", "marketer", "freelancer", "creator", "coach", "trainer", "parent", "student", "teacher",
    "nurse", "doctor", "lawyer", "accountant", "advisor", "beginner", "runner", "athlete",
]
_PLURALS = {"coach": "coaches"}
_ROLE_LOOKUP = {r: r for r in _ROLES} | {_PLURALS.get(r, r + "s"): r for r in _ROLES}
_TIME_UNITS = {"second", "minute", "hour", "day", "week", "month", "year", "night"}
_RATE_UNITS = {"month", "year", "week", "day", "night", "hour"}

_CUES: dict[str, list[str]] = {
    "result": ["result", "results", "ended up", "turned into", "pays for", "paid for", "pay for itself", "payback",
               "pays itself", "signed", "lease", "leased", "rented", "sold", "increase", "increased", "improvement",
               "improved", "gain", "profit", "return", "roi", "worth it", "punchline", "bottom line", "more a month",
               "a year improvement", "back in"],
    "mistake": ["mistake", "mistakes", "wrong", "never", "avoid", "problem", "problems", "fail", "failed", "failing",
                "worst", "regret", "trap", "warning", "stop", "deferred", "neglect", "neglected", "dated", "issue",
                "issues", "don't", "do not", "shouldn't", "too late", "overlook", "overlooked", "ignore", "ignored"],
    "contrast": ["actually", "instead", "turns out", "the truth", "myth", "wrong", "not necessarily", "wasn't just",
                 "not just", "surprisingly", "most people think", "everyone thinks", "people think", "unpopular",
                 "the opposite", "counterintuitive", "contrary"],
    "list": ["first", "second", "third", "next", "number one", "finally", "lastly", "step", "steps", "things",
             "checklist", "laundry list", "list"],
    "process": ["how we", "the process", "process", "step", "steps", "checklist", "walkthrough", "walk-through",
                "walk through", "system", "routine", "every time", "first thing", "order of business", "we do",
                "our work", "recommendations", "inspection", "plan", "playbook"],
    "how_to": ["how to", "here's how", "the way to", "guide", "how you", "how do you", "how i"],
    "question": ["why", "what if", "how do you", "would you", "did you know", "have you ever", "do you"],
    "proof": ["we've been", "we have been", "managing", "managed", "years", "clients", "properties", "hundreds",
              "thousands", "every", "experience", "for a living", "we manage", "we've done", "we have done"],
    "secret": ["nobody", "no one", "secret", "most people", "don't realize", "don't tell", "won't tell", "hidden",
               "quietly", "nobody talks", "the thing about"],
    "comparison": ["vs", "versus", "than", "compared", "instead of", "difference", "rather than", "either", "or you"],
    "emotion": ["honestly", "crazy", "insane", "wild", "shocked", "surprised", "love", "hate", "frustrating",
                "frustrated", "amazing", "awesome", "terrible", "nightmare", "blew", "unbelievable", "painful"],
}
_STORY_PAST = {"walked", "found", "got", "went", "did", "spent", "called", "moved", "came", "signed", "rented", "sold",
               "started", "ended", "happened", "turned", "noticed", "saw", "took", "made", "sent", "told"}
_COST_CUES = {"spent", "spend", "spending", "cost", "costs", "costing", "paid", "pay", "invested", "invest", "put",
              "worth", "budget", "bill", "invoice", "price", "total", "repairs", "work"}
_BEFORE_AFTER_CUES = {"to", "from", "now", "went", "up", "increase", "increased", "raise", "raised", "after", "instead",
                      "became", "turned", "jumped", "improvement", "improved", "boost", "then", "rent", "rented",
                      "renting", "lease", "leased", "signed", "sold", "sells", "price", "priced", "was", "today"}
_RESULT_WORDS = {"result", "results", "payback", "signed", "sign", "lease", "leased", "rented", "sold", "increase",
                 "increased", "improvement", "improvements", "improved", "gain", "profit", "return", "roi", "punchline",
                 "pays", "paid", "pay", "itself", "more", "extra", "saved", "savings", "earned", "earns"}
_DANGLING = {"a", "an", "the", "in", "of", "to", "and", "or", "that", "which", "for", "with", "at", "on", "by", "from",
             "without", "into", "so", "but", "is", "was", "are", "were", "it", "this", "we", "i", "you", "they",
             "pay", "pays", "will", "would", "can", "could", "has", "have", "had", "be", "been", "get", "got", "as"}
_ADDRESSING = {"any", "every", "fellow", "you", "all", "dear", "for", "hey", "attention", "calling"}
_STOP = set("""
a an the and or but so if then than that this these those there their they them its it is are was were be been being
am i me my we our ours us you your yours he she his her him who whom which what when where why how all any both each
few more most other some such no nor not only own same too very can will just don should now here very really thing
things going about with have from would could like know want make sure also into over under again still back down
even kind sort lot lots today those them come came take took give gave need needs needed does doing done did said says
say see look looking think thought right good great whole entire essentially obviously usually necessarily piece order
little every after before first second last next because actually probably basically much many well okay yeah let
get got getting go goes went one ones way ways time times out off up on in at by for of to as yes been being has had
having do did done something anything everything nothing someone anyone everyone able around through between while
long term short new old big small may might must shall ought
""".split())


@dataclass
class Signals:
    found: dict[str, list[str]] = field(default_factory=dict)  # signal -> evidence, in order of appearance
    slots: dict[str, str] = field(default_factory=dict)  # template slot -> value from the clip
    payoff: tuple[int, int] | None = None  # word range of the strongest result line (kept words)
    payoff_text: str = ""

    def has(self, name: str) -> bool:
        return bool(self.found.get(name))

    def strength(self, name: str) -> int:
        return min(len(self.found.get(name, [])), 4)

    def summary(self) -> str:
        parts = []
        for key in ("money", "before_after", "percent", "timeframe", "count", "audience", "place"):
            if self.found.get(key):
                parts.append(", ".join(dict.fromkeys(self.found[key][:3])))
        return " · ".join(parts)


def _money_text(value: float) -> str:
    return f"${int(value):,}" if value == int(value) else f"${value:,.2f}"


def _core(tok: str) -> str:
    return tok.strip(".,;:!?\"'()[]")


def _plural(word: str) -> str:
    return _PLURALS.get(word, word + "s")


def analyze(plan: Plan) -> Signals:
    """Read the kept words and report what hooks could lean on: figures, a before/after, a timeframe,
    who it's for, where it is, a mistake, a contrast, ... plus the strongest payoff line."""
    words = plan.words
    tokens = [(i, words[i].shown) for i, k in enumerate(plan.keep) if k and words[i].shown.strip()]
    merged = merge_numbers([t for _, t in tokens])
    units: list[tuple[int, str]] = [(tokens[pos[0]][0], text) for pos, text in merged]  # (word index, text)
    cores = [_core(t) for _, t in units]
    norms = [c.lower() for c in cores]
    joined = " " + " ".join(norms) + " "
    sig = Signals()
    found = sig.found

    def add(name: str, *evidence: str) -> None:
        bucket = found.setdefault(name, [])
        for e in evidence:
            if e and e not in bucket:
                bucket.append(e)

    def cue_hits(cues: list[str]) -> list[str]:
        hits = []
        for cue in cues:
            if re.search(r"(?<![\w'])" + re.escape(cue) + r"(?![\w'])", joined):
                hits.append(cue)
        return hits

    # Figures. A merged token is numeric when it is digits with optional $ , . %.
    numeric = [re.fullmatch(r"\$?\d[\d,]*(?:\.\d+)?%?", c) is not None for c in cores]
    money: list[tuple[int, float, str, str]] = []  # unit index, value, text, rate ("a month" when said)
    for k, c in enumerate(cores):
        if not numeric[k]:
            continue
        nxt = norms[k + 1] if k + 1 < len(norms) else ""
        if c.endswith("%") or nxt == "percent":
            add("percent", c if c.endswith("%") else c + "%")
            continue
        if c.startswith("$") or nxt in _MONEY_WORDS:
            value = float(c.strip("$").replace(",", ""))
            rate = ""
            rate_at = k + (2 if nxt in _MONEY_WORDS else 1)
            if rate_at + 1 < len(norms) and norms[rate_at] in ("a", "per") and norms[rate_at + 1].rstrip("s") in _RATE_UNITS:
                rate = f" a {norms[rate_at + 1].rstrip('s')}"
            money.append((k, value, _money_text(value), rate))
            continue
        if nxt.rstrip("s") in _TIME_UNITS and re.fullmatch(r"\d+", c):
            n = int(c)
            unit = nxt.rstrip("s")
            add("timeframe", f"{n} {unit}" if n == 1 else f"{n} {unit}s")
            continue
        if re.fullmatch(r"\d{1,2}", c) and 2 <= int(c) <= 12 and len(nxt) >= 4 and nxt.endswith("s") \
                and nxt.rstrip("s") not in _TIME_UNITS and nxt not in _MONEY_WORDS and _letters(nxt) not in _NUMBER_WORDS:
            add("count", f"{c} {nxt}")
            continue
        add("number", c)
    for _, _, text, _ in money:
        add("money", text)
    if money:
        counts = Counter(round(v) for _, v, _, _ in money)
        cost_mentions = [m for m in money if any(
            norms[j] in _COST_CUES for j in range(max(0, m[0] - 4), min(len(norms), m[0] + 5)))]
        if cost_mentions:
            add("cost", *[m[2] for m in cost_mentions])
        pool = cost_mentions or money
        headline = max(pool, key=lambda m: (counts[round(m[1])], m[1]))
        sig.slots["money"] = headline[2]
        # Before/after: two different amounts of the same kind close together, with a change or price word
        # between them. Costs ("spent $5,500") are never one side of the pair.
        cost_at = {m[0] for m in cost_mentions}
        best_pair, best_pair_score = None, 0.0
        for a_i in range(len(money)):
            for b_i in range(a_i + 1, len(money)):
                (ka, va, ta, ra), (kb, vb, tb, rb) = money[a_i], money[b_i]
                if kb - ka > 25:
                    break
                if va == vb or ka in cost_at or kb in cost_at or (ra and rb and ra != rb):
                    continue
                between = norms[ka + 1:kb]
                hits = set(between) & _BEFORE_AFTER_CUES
                if not hits:
                    continue
                score = 1.0 + 0.5 * len(hits) + (0.5 if ra or rb else 0.0) - 0.03 * (kb - ka)
                if score > best_pair_score:
                    best_pair, best_pair_score = ((ta, ra), (tb, rb), between[-2:]), score
        if best_pair is not None:
            (ta, ra), (tb, rb), tail = best_pair
            if "from" in tail:  # "$3,600 now, up from $3,250": the later mention is the earlier state
                (ta, ra), (tb, rb) = (tb, rb), (ta, ra)
            add("before_after", f"{ta} -> {tb}")
            sig.slots["money_before"] = ta + (ra or rb)
            sig.slots["money_after"] = tb + (rb or ra)
    if found.get("timeframe"):
        sig.slots["timeframe"] = found["timeframe"][0]
    if found.get("percent"):
        sig.slots["percent"] = found["percent"][0]
    if found.get("count"):
        sig.slots["count"] = found["count"][0].split()[0]
    if found.get("number"):
        sig.slots["number"] = found["number"][0]

    # Who it's for.
    roles: Counter[str] = Counter()
    for k, n in enumerate(norms):
        if n not in _ROLE_LOOKUP:
            continue
        # Being addressed ("as any landlord knows", "for landlords", "if you're a landlord", "owners out there")
        # says far more about who the video is for than being mentioned ("the tenant moved out").
        weight = 1
        before = norms[max(0, k - 3):k]
        if before and before[-1] in _ADDRESSING:
            weight += 6
        elif len(before) >= 2 and before[-1] in ("a", "an") and before[-2] in ("as", "you're", "youre", "being"):
            weight += 6
        if norms[k + 1:k + 3] == ["out", "there"]:
            weight += 6
        roles[_ROLE_LOOKUP[n]] += weight
    if roles:
        add("audience", *[_plural(r) for r, _ in roles.most_common()])
        top = roles.most_common(1)[0][0]
        sig.slots["audience"] = _plural(top)
        sig.slots["Audience"] = _plural(top).capitalize()
        sig.slots["audience_singular"] = top

    # Proper nouns: a place ("in Danville") and the subject ("Mission Place").
    runs: list[tuple[str, str]] = []  # (phrase, the word before it)
    k = 0
    while k < len(units):
        c = cores[k]
        sentence_start = k == 0 or cores[k - 1][-1:] in ".?!"
        if re.fullmatch(r"[A-Z][a-z]{2,}", c) and not sentence_start and norms[k] not in _STOP and norms[k] not in _ROLE_LOOKUP:
            j = k
            while j + 1 < len(units) and re.fullmatch(r"[A-Z][a-z]{2,}", cores[j + 1]) and norms[j + 1] not in _STOP:
                j += 1
            runs.append((" ".join(cores[k:j + 1]), norms[k - 1] if k else ""))
            k = j + 1
        else:
            k += 1
    for phrase, before in runs:
        if before in ("in", "at", "near", "around", "outside", "downtown", "to"):
            add("place", phrase)
    if found.get("place"):
        sig.slots["place"] = found["place"][0]
    if runs:
        sig.slots["subject"] = runs[0][0]

    # Topic: the most-used content word.
    content = Counter(n for n in norms if len(n) >= 4 and n.isalpha() and n not in _STOP and n not in _ROLE_LOOKUP
                      and n.rstrip("s") not in _TIME_UNITS and n not in _NUMBER_WORDS)
    if content:
        topic = content.most_common(1)[0][0]
        if topic.endswith("s") and not topic.endswith("ss") and content[topic[:-1]]:
            topic = topic[:-1]
        add("topic", topic)
        sig.slots["topic"] = topic
        sig.slots["Topic"] = topic.capitalize()
        sig.slots.setdefault("subject", f"this {topic}")

    # Cue-based signals.
    for name, cues in _CUES.items():
        hits = cue_hits(cues)
        if hits:
            add(name, *hits[:6])
    if any(c.endswith("?") for c in [t for _, t in units]):
        add("question", "?")
    pronouns = sum(n in ("we", "i", "our", "my", "us") for n in norms)
    past = [n for n in norms if n in _STORY_PAST]
    if pronouns >= 3 and len(past) >= 2:
        add("story", *list(dict.fromkeys(past))[:4])

    # The payoff line: the kept stretch (2-7.5 s) that carries the most figures and result words.
    best, best_score = None, 1.5
    short, short_score = "", 1.5
    for group in phrases(words):
        kept = [i for i in group if plan.keep[i]]
        runs_i: list[list[int]] = []
        for i in kept:
            if runs_i and runs_i[-1][-1] == i - 1:
                runs_i[-1].append(i)
            else:
                runs_i.append([i])
        for run in runs_i:
            for a in range(len(run)):
                for b in range(a + 3, len(run)):
                    dur = words[run[b]].end - words[run[a]].start
                    if dur > 7.5:
                        break
                    if dur < 2.0:
                        continue
                    text = " ".join(words[i].shown for i in run[a:b + 1] if words[i].shown)
                    toks = spoken_numbers(text).split()
                    n_money = sum(t.strip(".,").startswith("$") for t in toks)
                    n_num = sum(bool(re.search(r"\d", t)) for t in toks) - n_money
                    n_res = sum(_letters(t) in _RESULT_WORDS for t in toks)
                    score = 3 * n_money + 1.5 * n_res + 0.5 * n_num - 0.04 * len(toks) \
                        + (0.8 if text[-1:] in ".?!" else 0.0) + (0.4 if a == 0 else 0.0) \
                        - (1.5 if _letters(toks[-1]) in _DANGLING else 0.0)
                    if score > best_score:
                        best, best_score = (run[a], run[b]), score
                    if len(toks) <= 9 and score + 0.04 * len(toks) > short_score and (n_money or n_res):
                        short, short_score = " ".join(toks), score + 0.04 * len(toks)
    if best is not None:
        sig.payoff = best
        sig.payoff_text = spoken_numbers(" ".join(words[i].shown for i in range(best[0], best[1] + 1) if words[i].shown))
        result = (short or sig.payoff_text).strip().rstrip(".!?,")
        result = re.sub(r"^(so|and|but|then|because|which|that's|that is|it's|it is)\s+", "", result, flags=re.I)
        if result[:1].isupper() and result[1:2].islower():
            result = result[0].lower() + result[1:]
        sig.slots["result"] = result
    return sig


# --- matching ------------------------------------------------------------------------------------

@dataclass
class Match:
    pattern: Pattern
    score: float


def shortlist(catalog: list[Pattern], sig: Signals, n: int = 10, per_family: int = 2) -> list[Match]:
    """Patterns whose needs the clip meets, best first, at most `per_family` from one family so a sheet
    isn't ten number hooks."""
    scored: list[Match] = []
    for p in catalog:
        if any(not sig.has(x) for x in p.needs):
            continue
        score = 1.0 + sum(SIGNAL_WEIGHT[x] * (0.8 + 0.1 * sig.strength(x)) for x in p.needs)
        score += sum(0.4 * SIGNAL_WEIGHT[x] for x in p.wants if sig.has(x))
        score -= sum(1.0 for x in p.avoid if sig.has(x))
        score -= 0.3 * len(p.free_slots)
        if p.footage_ok and sig.payoff is not None:
            score += 0.2
        scored.append(Match(p, round(score, 3)))
    scored.sort(key=lambda m: (-m.score, m.pattern.id))
    picked: list[Match] = []
    per: Counter[str] = Counter()
    for m in scored:
        if per[m.pattern.family] < per_family:
            picked.append(m)
            per[m.pattern.family] += 1
        if len(picked) >= n:
            break
    if len(picked) < n:
        picked += [m for m in scored if m not in picked][: n - len(picked)]
    return picked


# --- hooks and the sheet -------------------------------------------------------------------------

@dataclass
class Hook:
    n: int
    pattern: str
    family: str
    name: str
    spoken: str  # the line to film
    title: str  # on-screen title over the first seconds
    why: str = ""
    film: str = ""
    cold_open: list[int] | None = None  # [first, last] word indices in the main clip that can stand in for the line
    cold_open_at: list[float] | None = None  # source seconds of that range
    cold_open_text: str = ""
    from_footage: bool = False  # a variant can be rendered without filming: title + cold open
    post_title: str = ""

    @property
    def slug(self) -> str:
        words = re.sub(r"[^a-z0-9 ]", "", self.title.lower()).split()[:4]
        return "-".join(words)[:24].strip("-") or self.pattern


@dataclass
class HookSheet:
    source: str
    hooks: list[Hook]
    signals: dict[str, list[str]] = field(default_factory=dict)
    slots: dict[str, str] = field(default_factory=dict)
    payoff_text: str = ""
    writer: str = "catalog"  # "catalog" (local templates) or the model that wrote the lines

    def to_dict(self) -> dict:
        return {"source": self.source, "writer": self.writer, "signals": self.signals, "slots": self.slots,
                "payoff_text": self.payoff_text, "hooks": [asdict(h) for h in self.hooks]}

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_dict(), indent=1, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "HookSheet":
        d = json.loads(path.read_text(encoding="utf-8"))
        return cls(d["source"], [Hook(**h) for h in d["hooks"]], d.get("signals", {}), d.get("slots", {}),
                   d.get("payoff_text", ""), d.get("writer", "catalog"))

    def markdown(self, clip_name: str = "", folder: str = "hooks") -> str:
        name = clip_name or Path(self.source).name
        sig = Signals(self.signals, self.slots)
        lines = [f"# Hooks for {name}", ""]
        if sig.summary():
            lines.append(f"What the clip gives us: {sig.summary()}")
        if self.payoff_text:
            lines.append(f"Payoff line: \"{self.payoff_text}\"")
        if self.writer == "catalog":
            lines.append("Lines drafted from the catalog templates (no Claude key): tighten them before filming; "
                         "[brackets] are for you to fill in.")
        lines += [
            "",
            "Film each hook as its own take, 3-6 seconds: look at the lens, start talking the moment you hit record, "
            "energy a notch above the body of the video. Save the takes as 01.mov, 02.mov, ... (any name starting "
            "with the hook number works) and run:",
            "",
            f"    vflow variants \"{name}\" --hooks {folder}",
            "",
        ]
        if any(h.from_footage for h in self.hooks):
            nums = ",".join(str(h.n) for h in self.hooks if h.from_footage)
            lines += [f"Hooks marked \"from footage\" also render with nothing filmed: "
                      f"vflow variants \"{name}\" --pick {nums}", ""]
        for h in self.hooks:
            lines += [f"## {h.n}. {h.title}", f"- Say: \"{h.spoken}\"", f"- Pattern: {h.name} ({h.pattern})"]
            if h.why:
                lines.append(f"- Why: {h.why}")
            if h.film:
                lines.append(f"- Film: {h.film}")
            if h.cold_open_at and h.cold_open_text:
                a, b = h.cold_open_at
                tag = "From footage: yes, cold open" if h.from_footage else "Cold open from the footage"
                lines.append(f"- {tag} {_mmss(a)}-{_mmss(b)} \"{h.cold_open_text}\"")
            lines.append("")
        return "\n".join(lines)


def _mmss(t: float) -> str:
    m, s = divmod(int(t), 60)
    return f"{m}:{s:02d}"


def fill(template: str, slots: dict[str, str]) -> str:
    text = re.sub(r"\{(\w+)\}", lambda m: slots.get(m.group(1), f"[{m.group(1)}]"), template)
    text = re.sub(r"(?<=[.!?] )([a-z])", lambda m: m.group(1).upper(), text)
    return text[0].upper() + text[1:] if text[:1].islower() else text


def _title_from(spoken: str, max_words: int = 8) -> str:
    """A title from a spoken line: its first sentence (two if the first is tiny), cut at a clause break
    when it runs long."""
    sentences = re.split(r"(?<=[.!?])\s+", spoken.strip())
    text = sentences[0]
    for nxt in sentences[1:]:  # "Before: $3,250. After: $3,600." stays whole; a long second sentence doesn't
        if len(f"{text} {nxt}".split()) > max_words:
            break
        text = f"{text} {nxt}"
    toks = text.split()
    if len(toks) > max_words:
        head = toks[:max_words]
        breaks = [k for k, tok in enumerate(head) if tok.endswith((",", ":", ";")) and k >= 2]
        breaks += [k - 1 for k, tok in enumerate(head) if tok.lower() in ("and", "but", "so", "because", "which") and k >= 3]
        toks = head[:max(breaks) + 1] if breaks else head
        text = " ".join(toks)
    return text.rstrip(".,:;")


def _cold_open(plan: Plan, first: int, last: int) -> tuple[list[int], list[float], str] | None:
    n = len(plan.words)
    if not (0 <= first <= last < n) or last - first > 30 or not all(plan.keep[first:last + 1]):
        return None
    a, b = plan.words[first].start, plan.words[last].end
    if not 0.8 <= b - a <= 8.0:
        return None
    text = spoken_numbers(" ".join(plan.words[i].shown for i in range(first, last + 1) if plan.words[i].shown))
    return [first, last], [round(a, 2), round(b, 2)], text


def draft(matches: list[Match], sig: Signals, plan: Plan) -> list[Hook]:
    """Hooks straight from the templates: what you get without an API key."""
    out: list[Hook] = []
    payoff = _cold_open(plan, *sig.payoff) if sig.payoff else None
    for m in matches:
        p = m.pattern
        spoken = fill(p.template, sig.slots)
        why = ", ".join(sig.found[x][0] for x in p.needs if sig.found.get(x))
        h = Hook(len(out) + 1, p.id, p.family, p.name, spoken, _title_from(spoken), why=f"uses {why}" if why else "",
                 film=p.film, post_title=_title_from(spoken, 10))
        if payoff is not None:
            h.cold_open, h.cold_open_at, h.cold_open_text = payoff
            h.from_footage = p.footage_ok
        out.append(h)
    return out


# --- the Claude writer -----------------------------------------------------------------------------

HOOKS_SYSTEM = """You write hooks for short-form talking-head videos (Reels / TikTok / Shorts). A hook is the \
first one to three seconds: a line the creator films as a separate take, plus a short on-screen title. You get \
the transcript of the finished body of the video, the facts an automatic pass found in it, and a shortlist of \
hook patterns that fit its content.

Rules:
- Use only what the transcript says. Every number, place, name and outcome must come from it; never invent \
or round a figure. If the transcript gives before and after numbers, you may state the difference.
- One hook per pattern, strongest first. Skip a pattern that doesn't fit this clip rather than forcing it. \
If the shortlist is shorter than the count asked for, the extra hooks may reuse a family with a different angle.
- spoken: 6-20 words the creator can say in one breath, first person, plain words, in their voice (match \
the transcript's register). No clickbait ("you won't believe"), no emojis, no hashtags.
- title: 3-8 words that work with the sound off; it may repeat the spoken line's key figure.
- cold_open_first_word / cold_open_last_word: if a line already in the transcript makes the same promise \
as this hook, give its word range (kept words only, 2-7 seconds of speech, a complete thought) so a version \
can be rendered without filming; otherwise -1 for both.
- why: under 12 words, which fact in the clip the hook leans on.
- film: a one-line filming note (framing, prop, energy) only when it helps; otherwise empty.
- post_title: a title for the post that uses this hook, under 60 characters."""


class HookDraft(BaseModel):
    pattern_id: str = Field(description="id of the shortlisted pattern this hook follows")
    spoken: str = Field(description="the line the creator films: 6-20 words, one breath")
    title: str = Field(description="on-screen title, 3-8 words")
    cold_open_first_word: int = Field(description="first word index of a transcript line that can stand in for this hook, or -1")
    cold_open_last_word: int = Field(description="last word index (inclusive) of that line, or -1")
    why: str = Field(description="under 12 words: the fact in the clip this hook leans on")
    film: str = Field(description="one-line filming note, or empty")
    post_title: str = Field(description="post title for this variant, under 60 characters")


class HookSet(BaseModel):
    hooks: list[HookDraft]


def write_with_claude(plan: Plan, s: Settings, sig: Signals, matches: list[Match], n: int) -> list[Hook] | None:
    import anthropic

    by_id = {m.pattern.id: m.pattern for m in matches}
    shortlist_text = "\n".join(
        f"- {p.id} [{p.family}] {p.name}: {p.template!r}, e.g. {p.example!r}"
        + (f"; filming: {p.film}" if p.film else "") for p in by_id.values())
    facts = "; ".join(f"{k}: {', '.join(v[:4])}" for k, v in sig.found.items() if v and k not in ("topic", "question"))
    user = (
        f"Write {n} hooks for this clip.\n\n"
        f"Facts found automatically (check them against the transcript): {facts or 'none'}\n"
        + (f"Strongest payoff line so far: \"{sig.payoff_text}\" (words {sig.payoff[0]}-{sig.payoff[1]})\n" if sig.payoff else "")
        + f"\nShortlisted patterns:\n{shortlist_text}\n\n"
        "Transcript (every word carries its index; [cut:...] ranges are already removed from the video):\n"
        + llm.render_transcript(plan)
    )
    if s.style_notes:
        user += f"\n\nStyle notes from the creator: {s.style_notes}"
    client = anthropic.Anthropic()
    response = client.beta.messages.parse(
        model=s.llm_model,
        max_tokens=8000,
        system=HOOKS_SYSTEM,
        messages=[{"role": "user", "content": user}],
        output_format=HookSet,
        output_config={"effort": s.llm_effort},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        return None
    out: list[Hook] = []
    seen: set[str] = set()
    for d in response.parsed_output.hooks:
        p = by_id.get(d.pattern_id.strip())
        spoken, title = " ".join(d.spoken.split()), " ".join(d.title.split()).rstrip(".")
        if p is None or not spoken or not title or spoken.lower() in seen:
            continue
        seen.add(spoken.lower())
        h = Hook(len(out) + 1, p.id, p.family, p.name, spoken[:200], title[:60], why=d.why.strip()[:120],
                 film=d.film.strip()[:160] or p.film, post_title=d.post_title.strip()[:80] or title[:60])
        if d.cold_open_first_word >= 0 and (co := _cold_open(plan, d.cold_open_first_word, d.cold_open_last_word)):
            h.cold_open, h.cold_open_at, h.cold_open_text = co
            h.from_footage = True
        out.append(h)
        if len(out) >= n:
            break
    return out or None


def produce(plan: Plan, s: Settings, n: int | None = None, use_llm: bool | None = None,
            catalog: list[Pattern] | None = None) -> HookSheet:
    """The hook sheet for a clip: catalog match, then Claude writes the lines if a key is available."""
    n = n or s.hooks_count
    catalog = catalog if catalog is not None else load_catalog(s.hooks_catalog)
    sig = analyze(plan)
    matches = shortlist(catalog, sig, n=max(n + 4, int(n * 1.5)))
    if not matches:
        raise ValueError("No hook pattern fits this clip (too little speech to read anything from it)")
    hooks: list[Hook] | None = None
    writer = "catalog"
    if (s.use_llm if use_llm is None else use_llm) and llm.available():
        hooks = write_with_claude(plan, s, sig, matches, n)
        if hooks:
            writer = s.llm_model
    if not hooks:
        hooks = draft(matches[:n], sig, plan)
    return HookSheet(plan.source, hooks, sig.found, sig.slots, sig.payoff_text, writer)


# --- filmed hook clips -------------------------------------------------------------------------------

_DATE_LIKE = re.compile(r"\d{4}[-_.]\d{2}[-_.]\d{2}|\d{2}[-_.]\d{2}[-_.]\d{2,4}")
_CLIP_NUMBER = re.compile(r"(?:^|[^0-9])(\d{1,2})(?!\d)")


def assign_clips(files: list[Path], count: int, picks: list[int] | None = None) -> dict[int, Path]:
    """Map filmed hook clips to hook numbers: a number in the file name wins ("03 mistake.mov",
    "hook-3.mp4", "IMG_01.MOV"); the rest go, in name order, to the lowest hook numbers still free."""
    out: dict[int, Path] = {}
    rest: list[Path] = []
    wanted = list(picks) if picks else list(range(1, count + 1))
    for f in sorted(files, key=lambda p: p.name.lower()):
        m = _CLIP_NUMBER.search(_DATE_LIKE.sub(" ", f.stem))
        k = int(m.group(1)) if m else 0
        if k in wanted and k not in out:
            out[k] = f
        else:
            rest.append(f)
    for f, k in zip(rest, [k for k in wanted if k not in out]):
        out[k] = f
    return out
