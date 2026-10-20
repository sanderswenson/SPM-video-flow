import pytest

from vflow import hooks, pipeline
from vflow.config import Settings
from vflow.hooks import Hook, HookSheet, Pattern, Signals
from vflow.plan import Plan, Shot, segments, total_duration, word_times
from vflow.transcript import Word


def speech(text, start=1.0, gap=0.35):
    """Evenly timed words from a string; a sentence end adds a short pause like real speech."""
    words, t = [], start
    for tok in text.split():
        words.append(Word(round(t, 3), round(t + 0.25, 3), tok))
        t += 0.3 + (gap if tok[-1] in ".?!" else 0.0)
    return words


def make_plan(text):
    words = speech(text)
    return Plan("talk.mp4", words[-1].end + 1, 30.0, words, [True] * len(words))


TALK = ("As any landlord knows, a long-term tenant usually means deferred maintenance and dated cosmetics. "
        "Mission Place in Danville had a tenant for nine years. We did a detailed walkthrough and sent the owner a "
        "list of recommendations. All in all we spent about fifty five hundred dollars on the property. "
        "It had been renting for $3,250 a month. We rent it out at $3,600 a month within 14 days. "
        "That's a $4,200 a year improvement which pays for the repairs in 18 months. "
        "So this walkthrough wasn't just about repairs.")


# --- catalog -------------------------------------------------------------------------------------

def test_bundled_catalog_is_valid_and_varied():
    cat = hooks.load_catalog()
    assert len(cat) >= 60
    assert len({p.family for p in cat}) >= 15
    assert len({p.id for p in cat}) == len(cat)
    for p in cat:
        assert p.needs, p.id  # every pattern asks for at least one signal
        assert set(p.needs) | set(p.wants) | set(p.avoid) <= hooks.SIGNALS
        assert p.delivery in hooks.DELIVERIES
        assert p.example and p.film
    assert any(p.footage_ok for p in cat) and any(not p.footage_ok for p in cat)


def test_catalog_validation_and_user_overrides(tmp_path):
    with pytest.raises(ValueError, match="unknown signal"):
        hooks.parse_catalog('[[hook]]\nid="x"\nfamily="f"\nname="n"\ntemplate="t"\nneeds=["moneys"]\n')
    with pytest.raises(ValueError, match="duplicate"):
        hooks.parse_catalog('[[hook]]\nid="x"\nfamily="f"\nname="n"\ntemplate="t"\n'
                            '[[hook]]\nid="x"\nfamily="f"\nname="n"\ntemplate="t"\n')
    extra = tmp_path / "mine.toml"
    extra.write_text('[[hook]]\nid="number-result"\nfamily="number"\nname="Mine"\ntemplate="{money}!"\nneeds=["money"]\n'
                     '[[hook]]\nid="my-own"\nfamily="me"\nname="Own"\ntemplate="Hi {audience}"\nneeds=["audience"]\n')
    cat = {p.id: p for p in hooks.load_catalog(extra)}
    assert cat["number-result"].name == "Mine" and "my-own" in cat
    assert Pattern("a", "f", "n", "{money} and {thing} for {Audience}").free_slots == ["thing"]


# --- spoken numbers and signals ------------------------------------------------------------------

def test_spoken_numbers():
    assert hooks.spoken_numbers("we spent fifty five hundred dollars on it") == "we spent $5,500 on it"
    assert hooks.spoken_numbers("eighteen months, ten percent and two thousand bucks.") == "18 months, 10% and $2,000."
    assert hooks.spoken_numbers("one step at a time and a hundred ideas") == "one step at a time and 100 ideas"
    assert hooks.spoken_numbers("three things. Twenty five, then.") == "3 things. 25, then."


def test_signals_from_a_talk():
    sig = hooks.analyze(make_plan(TALK))
    assert sig.found["money"][:3] == ["$5,500", "$3,250", "$3,600"]
    assert sig.found["cost"] == ["$5,500"]
    assert sig.found["before_after"] == ["$3,250 -> $3,600"]
    assert sig.found["timeframe"] == ["9 years", "14 days", "18 months"]
    assert sig.found["audience"][0] == "landlords"  # addressed ("as any landlord knows") beats mentioned (tenant)
    assert sig.found["place"] == ["Danville"]
    assert sig.slots["subject"] == "Mission Place"
    assert sig.slots["money"] == "$5,500" and sig.slots["money_after"] == "$3,600 a month"
    assert sig.slots["audience_singular"] == "landlord" and sig.slots["Audience"] == "Landlords"
    assert sig.has("result") and sig.has("process") and sig.has("contrast")
    assert sig.payoff is not None
    assert "$" in sig.payoff_text and sig.payoff_text.split()[-1].lower().strip(".") not in hooks._DANGLING
    assert sig.slots["result"] and sig.slots["result"].split()[-1] not in hooks._DANGLING
    assert "$3,250 -> $3,600" in sig.summary()


def test_signals_ignore_dropped_words_and_use_caption_fixes():
    plan = make_plan("we spent fifty five hundred dollars. Nobody tells landlords this. Scrap that line entirely.")
    plan.words[2].display = "$5,500"
    for i in (3, 4, 5):
        plan.words[i].display = ""
    plan.drop(range(10, 14), "off_script")
    sig = hooks.analyze(plan)
    assert sig.found["money"] == ["$5,500"] and sig.has("secret")
    assert "scrap" not in " ".join(sum(sig.found.values(), [])).lower()


# --- matching and drafting -----------------------------------------------------------------------

def pattern(id, family, needs, template="{money} x", wants=(), footage_ok=True, avoid=()):
    return Pattern(id, family, id, template, example="e", needs=list(needs), wants=list(wants), avoid=list(avoid),
                   footage_ok=footage_ok, film="f")


def test_shortlist_requires_needs_and_spreads_families():
    sig = Signals({"money": ["$5", "$6", "$7"], "result": ["sold"], "timeframe": ["2 weeks"]}, {}, (0, 3))
    cat = [
        pattern("n1", "number", ["money"]), pattern("n2", "number", ["money"], wants=["timeframe"]),
        pattern("n3", "number", ["money", "result"]), pattern("t1", "time", ["timeframe"]),
        pattern("m1", "mistake", ["mistake"]), pattern("b1", "before", ["before_after"]),
        pattern("p1", "payoff", ["result"], avoid=["money"]),
    ]
    picked = hooks.shortlist(cat, sig, n=4)
    ids = [m.pattern.id for m in picked]
    assert "m1" not in ids and "b1" not in ids  # unmet needs
    assert ids[0] == "n3"  # two met needs beat one
    assert sum(i.startswith("n") for i in ids) == 2  # at most two per family while others qualify
    assert "t1" in ids and "p1" in ids
    assert picked[ids.index("p1")].score < picked[ids.index("t1")].score  # avoid penalty


def test_local_draft_fills_slots_and_marks_free_ones():
    plan = make_plan(TALK)
    sheet = hooks.produce(plan, Settings(hooks_count=6), use_llm=False)
    assert sheet.writer == "catalog" and len(sheet.hooks) == 6
    assert [h.n for h in sheet.hooks] == [1, 2, 3, 4, 5, 6]
    assert len({h.family for h in sheet.hooks}) >= 3
    texts = " ".join(h.spoken for h in sheet.hooks)
    assert "$5,500" in texts or "$3,600" in texts
    for h in sheet.hooks:
        assert h.title and 1 <= len(h.title.split()) <= 8 and not h.title.endswith(".")
        assert h.cold_open is not None and h.cold_open_at and h.cold_open_text
        if "[" in h.spoken:
            assert not h.from_footage or h.spoken.count("[") >= 1  # free slots are shown as [slot]
    assert hooks.fill("Before: {money_before}. After: {money_after}.", {"money_before": "$1", "money_after": "$2"}) \
        == "Before: $1. After: $2."
    assert hooks.fill("{money} in. {result}.", {"money": "$5", "result": "a win"}) == "$5 in. A win."
    assert hooks.fill("stop {action}.", {}) == "Stop [action]."


def test_title_rule():
    assert hooks._title_from("This cost $5,500.") == "This cost $5,500"
    assert hooks._title_from("Before: $3,250 a month. After: $3,600 a month.") == "Before: $3,250 a month. After: $3,600 a month"
    assert hooks._title_from("You own a rental. Here's what it's secretly costing you.") == "You own a rental"
    assert hooks._title_from("This is costing you $350 a month and you don't know it.") == "This is costing you $350 a month"
    assert hooks._title_from("If your rental looks like this, you're losing money.") == "If your rental looks like this,".rstrip(",")


def test_sheet_roundtrip_and_markdown(tmp_path):
    h = Hook(1, "number-result", "number", "Number → result", "We spent $5,500 and got $4,200 a year back.",
             "$5,500 in, $4,200 a year out", why="cost and return", film="hold up the figure", cold_open=[10, 18],
             cold_open_at=[61.2, 66.0], cold_open_text="We rent it out at $3,600 a month.", from_footage=True,
             post_title="A $5,500 walkthrough that pays for itself")
    h2 = Hook(2, "story-walked-in", "story", "We walked in", "We walked in and the first thing we saw was [thing].",
              "We walked in and")
    sheet = HookSheet("talk.mp4", [h, h2], {"money": ["$5,500"]}, {"money": "$5,500"}, "We rent it out at $3,600.", "claude")
    sheet.save(tmp_path / "hooks.json")
    back = HookSheet.load(tmp_path / "hooks.json")
    assert back.to_dict() == sheet.to_dict()
    assert back.hooks[0].slug == "5500-in-4200-a" and back.hooks[1].slug == "we-walked-in-and"
    md = sheet.markdown("talk.mp4", folder="hooks")
    assert md.startswith("# Hooks for talk.mp4") and "## 1. $5,500 in, $4,200 a year out" in md
    assert '--hooks hooks' in md and "--pick 1" in md
    assert "1:01-1:06" in md and "From footage: yes" in md
    assert "## 2." in md and "From footage" not in md.split("## 2.")[1]


def test_claude_writer_is_validated(monkeypatch):
    plan = make_plan(TALK)
    sig = hooks.analyze(plan)
    cat = hooks.load_catalog()
    matches = hooks.shortlist(cat, sig, n=8)
    good = matches[0].pattern.id
    drafts = [
        hooks.HookDraft(pattern_id=good, spoken="We spent $5,500 and got $350 a month back.", title="The $5,500 bet.",
                        cold_open_first_word=sig.payoff[0], cold_open_last_word=sig.payoff[1], why="cost vs rent",
                        film="", post_title="The $5,500 bet"),
        hooks.HookDraft(pattern_id="not-a-pattern", spoken="x y z", title="t", cold_open_first_word=-1,
                        cold_open_last_word=-1, why="", film="", post_title=""),
        hooks.HookDraft(pattern_id=good, spoken="We spent $5,500 and got $350 a month back.", title="dupe",
                        cold_open_first_word=-1, cold_open_last_word=-1, why="", film="", post_title=""),
        hooks.HookDraft(pattern_id=matches[1].pattern.id, spoken="Landlords, this one's for you.", title="For landlords",
                        cold_open_first_word=0, cold_open_last_word=400, why="", film="", post_title=""),
    ]

    class FakeResponse:
        stop_reason = "end_turn"
        parsed_output = hooks.HookSet(hooks=drafts)

    class FakeMessages:
        def parse(self, **kwargs):
            assert kwargs["output_format"] is hooks.HookSet
            assert "Shortlisted patterns" in kwargs["messages"][0]["content"]
            assert good in kwargs["messages"][0]["content"]
            return FakeResponse()

    class FakeClient:
        def __init__(self, *a, **k):
            self.beta = type("B", (), {"messages": FakeMessages()})()

    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    sheet = hooks.produce(plan, Settings(hooks_count=5), catalog=cat)
    assert sheet.writer == Settings().llm_model
    assert [h.n for h in sheet.hooks] == [1, 2]
    assert sheet.hooks[0].title == "The $5,500 bet" and sheet.hooks[0].from_footage and sheet.hooks[0].cold_open == list(sig.payoff)
    assert sheet.hooks[0].film == matches[0].pattern.film  # the catalog's note fills an empty one
    assert not sheet.hooks[1].from_footage and sheet.hooks[1].cold_open is None  # out-of-range cold open dropped


def test_claude_refusal_falls_back_to_templates(monkeypatch):
    class FakeClient:
        def __init__(self, *a, **k):
            self.beta = type("B", (), {"messages": type("M", (), {"parse": lambda self, **kw: type("R", (), {
                "stop_reason": "refusal", "parsed_output": None})()})()})()

    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    sheet = hooks.produce(make_plan(TALK), Settings(hooks_count=3))
    assert sheet.writer == "catalog" and len(sheet.hooks) == 3


# --- filmed clips ----------------------------------------------------------------------------------

def test_assign_clips_by_number_then_order(tmp_path):
    files = [tmp_path / n for n in ("IMG_4821.MOV", "03 mistake.mov", "hook-1.mp4", "2024-10-19 take.mp4", "IMG_02.MOV")]
    got = hooks.assign_clips(files, count=10)
    assert {k: v.name for k, v in got.items()} == {
        1: "hook-1.mp4", 2: "IMG_02.MOV", 3: "03 mistake.mov", 4: "2024-10-19 take.mp4", 5: "IMG_4821.MOV"}
    got = hooks.assign_clips([tmp_path / "a.mov", tmp_path / "b.mov"], count=10, picks=[4, 7])
    assert {k: v.name for k, v in got.items()} == {4: "a.mov", 7: "b.mov"}
    assert hooks.assign_clips([tmp_path / "42.mov"], count=10) == {1: tmp_path / "42.mov"}  # no hook 42: by order


def test_variant_prepends_lead_clip_and_shifts_captions(tmp_path, monkeypatch):
    s = Settings(captions=True, callouts=True, cta="")
    plan = make_plan("First line here. The payoff is forty two percent more. Closing thought here.")
    plan.overlays = {"hook_text": "old title", "hook_range": [3, 9], "callouts": [{"first": 4, "last": 5, "text": "42%"}]}
    plan.post = {"title": "old", "caption": "c", "hashtags": ["#x"]}
    hook = Hook(2, "payoff-first", "payoff", "Payoff first", "Forty two percent more. Let me show you how.",
                "42% more", cold_open=[3, 9], from_footage=True, post_title="New title")

    lead_words = speech("Forty two percent more. Let me show you how.", start=0.8)
    lead_plan = Plan(str(tmp_path / "02.mov"), lead_words[-1].end + 0.5, 30.0, lead_words, [True] * len(lead_words))
    lead_shots = [Shot(a, b, "hook", str(tmp_path / "02.mov")) for a, b in segments(lead_plan, s)]
    lead = pipeline.Lead(tmp_path / "02.mov", lead_plan, lead_shots, tmp_path / "lead")
    monkeypatch.setattr(pipeline, "lead_from_clip", lambda clip, wd, s, report: lead)

    captured = {}
    monkeypatch.setattr(pipeline.media, "probe", lambda p: pipeline.media.MediaInfo(60.0, 1920, 1080, 30.0))
    monkeypatch.setattr(pipeline.framing, "available", lambda: False)

    def fake_cut(src, shots, framings, size, info, s, wd, on_progress=lambda p: None):
        captured["shots"] = shots
        captured["framings"] = framings
        return wd / "concat.txt"

    def fake_final(listing, info, size, s, caps, out, total, on_progress=lambda p: None, broll=None, music=None):
        captured["caps"] = caps.read_text(encoding="utf-8")
        captured["total"] = total
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"")

    monkeypatch.setattr(pipeline.render, "cut_segments", fake_cut)
    monkeypatch.setattr(pipeline.render, "final", fake_final)

    out, summary = pipeline.render_variant(plan, tmp_path, tmp_path / "out", s, hook, tmp_path / "02.mov")
    assert out.name == "talk_hook02-42-more.mp4" and out.exists()
    shots = captured["shots"]
    assert shots[: len(lead_shots)] == lead_shots and all(sh.kind == "main" and sh.source is None for sh in shots[len(lead_shots):])
    assert len(captured["framings"]) == len(shots)
    lead_len = total_duration(lead_shots)
    assert abs(captured["total"] - (lead_len + total_duration(shots[len(lead_shots):]))) < 1e-6
    caps = captured["caps"]
    assert "42% MORE" in caps and "OLD TITLE" not in caps  # the hook's title replaces the plan's
    assert caps.count(",Word,") >= len(lead_words) + 10  # lead words captioned, then the body
    # Body words start after the lead, and the callout at word 4 lands in the body, not on the lead.
    body_times = word_times(plan, shots)
    assert min(t for t, _ in body_times.values()) >= lead_len - 1e-6
    post = (tmp_path / "out" / "talk_hook02-42-more.txt").read_text(encoding="utf-8")
    assert post.startswith("New title")

    # Without a clip the variant cold-opens the stand-in line from the footage and keeps the new title.
    out2, _ = pipeline.render_variant(plan, tmp_path, tmp_path / "out", s, hook, None)
    shots2 = captured["shots"]
    assert shots2[0].kind == "hook" and shots2[0].source is None
    assert "42% MORE" in captured["caps"]


def test_cut_segments_uses_each_shots_source(tmp_path, monkeypatch):
    from vflow import render
    from vflow.framing import Framing

    calls = []
    monkeypatch.setattr(render.media, "run", lambda cmd, cwd=None: calls.append(cmd))
    info = render.media.MediaInfo(60.0, 1920, 1080, 30.0)
    shots = [Shot(0.0, 2.0, "hook", str(tmp_path / "hook.mov")), Shot(5.0, 7.0)]
    fr = [Framing(608, 1080, "0", "0")] * 2
    render.cut_segments(tmp_path / "main.mp4", shots, fr, (1080, 1920), info, Settings(), tmp_path)
    srcs = sorted(c[c.index("-i") + 1] for c in calls)
    assert srcs == sorted([str(tmp_path / "hook.mov"), str(tmp_path / "main.mp4")])
    assert (tmp_path / "concat.txt").read_text().count("file ") == 2
