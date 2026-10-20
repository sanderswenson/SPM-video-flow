from vflow import captions
from vflow.config import Settings
from vflow.plan import Plan, Shot, drop_fillers, drop_repeats, remap, segments, shots, word_times
from vflow.transcript import Word, normalize, phrases


def make_plan(spec, fps=30.0, duration=None):
    words = [Word(a, b, t) for a, b, t in spec]
    return Plan("in.mp4", duration or spec[-1][1] + 1, fps, words, [True] * len(words))


def speech(text, start=1.0, pause_after=(".", "?", "!"), gap=0.35):
    """Evenly timed words from a string; a sentence end adds a short pause like real speech."""
    spec, t = [], start
    for tok in text.split():
        spec.append((round(t, 3), round(t + 0.25, 3), tok))
        t += 0.3 + (gap if tok[-1] in pause_after else 0.0)
    return spec


def test_short_pauses_stay_long_pauses_cut():
    p = make_plan([(1.0, 1.4, "hello"), (1.5, 1.9, "there"), (4.0, 4.5, "friend")])
    segs = segments(p, Settings())
    assert len(segs) == 2
    assert segs[0][0] < 1.0 and segs[0][1] > 1.9
    assert segs[1][0] < 4.0 and segs[1][1] > 4.5


def test_filler_is_cut_even_without_pause():
    p = make_plan([(1.0, 1.4, "so"), (1.45, 1.7, "um,"), (1.75, 2.2, "yeah")])
    drop_fillers(p, Settings().fillers)
    assert p.keep == [True, False, True]
    segs = segments(p, Settings())
    assert len(segs) == 2
    # padding never reaches into the dropped word
    assert segs[0][1] <= 1.45 + 1 / 30 and segs[1][0] >= 1.7 - 1 / 30


def test_segments_snap_to_frames():
    p = make_plan([(1.013, 1.4, "a"), (3.0, 3.31, "b")])
    for a, b in segments(p, Settings()):
        assert abs(a * 30 - round(a * 30)) < 1e-3
        assert abs(b * 30 - round(b * 30)) < 1e-3


def test_remap_moves_words_onto_edited_timeline():
    p = make_plan([(1.0, 1.4, "one"), (5.0, 5.4, "two")])
    segs = segments(p, Settings())
    out = remap(p, segs)
    assert [w.text for w in out] == ["one", "two"]
    first_len = segs[0][1] - segs[0][0]
    assert out[0].start < first_len
    assert abs(out[1].start - (first_len + (5.0 - segs[1][0]))) < 1e-3


def test_plan_roundtrip(tmp_path):
    p = make_plan([(1.0, 1.4, "um"), (1.5, 1.9, "hi")])
    drop_fillers(p, ["um"])
    p.post = {"title": "x"}
    p.overlays = {"hook_text": "Hi", "hook_range": [1, 1], "callouts": [], "broll": [], "cta": "Follow"}
    p.words[1].display = "Hi!"
    p.save(tmp_path / "plan.json")
    q = Plan.load(tmp_path / "plan.json")
    assert q.keep == p.keep and q.reasons == p.reasons and q.post == p.post
    assert q.overlays == p.overlays and q.words[1].display == "Hi!"


def test_phrases_break_on_pause_and_punctuation():
    words = [Word(0, 0.3, "Hi."), Word(0.35, 0.6, "So"), Word(0.65, 0.9, "yes"), Word(2.0, 2.3, "next")]
    assert phrases(words) == [[0], [1, 2], [3]]


def test_normalize_merges_split_tokens():
    words = normalize([Word(0, 0.2, "$5"), Word(0.2, 0.5, ",500"), Word(0.6, 0.9, "long"), Word(0.9, 1.2, "-time"),
                       Word(1.3, 1.5, "ok"), Word(1.5, 1.6, "...")])
    assert [w.text for w in words] == ["$5,500", "long-time", "ok..."]
    assert words[0].start == 0 and words[0].end == 0.5


# --- repeated lines --------------------------------------------------------------------------

def kept_text(p):
    return " ".join(w.text for w, k in zip(p.words, p.keep) if k)


def test_sentence_restart_keeps_last_take():
    p = make_plan(speech("As any landlord knows, a long, as any landlord knows, a long-time tenant means deferred "
                         "maintenance, and as any landlord knows, a long-time tenant means deferred maintenance and "
                         "dated cosmetics. As any landlord knows, a long-term tenant usually means dated cosmetics."))
    drop_repeats(p)
    assert kept_text(p) == "As any landlord knows, a long-term tenant usually means dated cosmetics."
    assert set(p.reasons.values()) == {"retake"}


def test_chain_of_false_starts():
    p = make_plan(speech("The property is under market. So our order, so our work turns to updating, so our work "
                         "turns to, so our work becomes making, so our work. So, our work is repairing what needs repaired."))
    drop_repeats(p)
    assert kept_text(p) == "The property is under market. So, our work is repairing what needs repaired."


def test_restart_mid_sentence_and_lookback():
    p = make_plan(speech("Make sure we had a really good feeling for what was going on after the tenant moved out. "
                         "Make sure that we had a really good feeling for the property after the tenant left."))
    drop_repeats(p)
    assert kept_text(p) == "Make sure we had a really good feeling for the property after the tenant left."
    p = make_plan(speech("Long-term tenants with deferred maintenance also usually means that the a long-term tenant "
                         "with deferred maintenance also usually means that the property is under market."))
    drop_repeats(p)
    assert kept_text(p) == "long-term tenant with deferred maintenance also usually means that the property is under market."


def test_rhetorical_repetition_and_short_echoes_survive():
    text = ("I could bore you with the list. Day in, day out, we do this. In the kitchen and in the yard. "
            "This walkthrough wasn't just about repairs. This walkthrough was one step in a long process.")
    p = make_plan(speech(text))
    assert drop_repeats(p) == 0
    assert kept_text(p) == text


def test_long_block_needs_evidence_it_was_resaid():
    # The same figure comes back 40 words later in a new sentence: not a retake.
    text = ("We spent $5,500 worth of work on the property. I could bore you with the laundry list of things we "
            "needed to do for this property, down to the touch up paint and screw hole patching, up to re-barking "
            "the entire backyard. What you probably want is the punchline: $5,500 worth of work into a property "
            "renting for $3,250. We rent it out at $3,600 a month within 14 days.")
    p = make_plan(speech(text))
    assert drop_repeats(p) == 0
    # The same block re-recorded nearly word for word: the first delivery goes.
    block = ("So we put $5,500 worth of work into this property that had been renting for $3,250 a month. After these "
             "improvements we signed a lease at $3,600 a month. That pays for the repairs in 18 months. ")
    redo = ("What you want is the punchline. So we put $5,500 worth of work into a property renting for $3,250 a month. "
            "We rent it out at $3,600 a month within 14 days. That pays for the repairs in 18 months without a rent raise.")
    p = make_plan(speech("Here is the result. " + block + redo))
    drop_repeats(p)
    kept = kept_text(p)
    assert kept.startswith("Here is the result. ") and kept.endswith(redo.split(". ", 1)[1])
    assert kept.count("$5,500") == 1 and "After these improvements" not in kept


def test_stutter_and_debris_island():
    p = make_plan(speech("I I think this this works."))
    drop_repeats(p)
    assert kept_text(p) == "I think this works."
    assert set(p.reasons.values()) == {"stutter"}


# --- shots, hook, captions -------------------------------------------------------------------

def test_hook_shot_is_prepended_and_words_remap_twice():
    p = make_plan(speech("First line here. The payoff is forty two percent more. Closing thought here."))
    s = Settings()
    p.overlays = {"hook_range": [3, 9]}
    sh = shots(p, s)
    assert sh[0].kind == "hook" and sh[1].kind == "main"
    assert sh[0].start < p.words[3].start and sh[0].end > p.words[9].end
    words = remap(p, sh)
    assert [w.text for w in words][:3] == ["The", "payoff", "is"]
    assert words.count(next(w for w in words if w.text == "payoff")) == 1
    assert sum(w.text == "payoff" for w in words) == 2
    # word_times ignores the hook repeat and reports main-body times, after the hook's duration
    wt = word_times(p, sh)
    assert wt[3][0] >= sh[0].duration
    # a hook that points at dropped words is ignored
    p.keep[5] = False
    assert shots(p, s)[0].kind == "main"


def test_captions_highlight_each_word():
    s = Settings()
    words = [Word(0.0, 0.3, "make"), Word(0.3, 0.6, "it"), Word(0.6, 1.0, "postable")]
    ass = captions.build(words, (1080, 1920), s)
    events = [line for line in ass.splitlines() if line.startswith("Dialogue")]
    assert len(events) == 3
    assert "PlayResY: 1920" in ass
    assert all("MAKE" in e and "POSTABLE" in e for e in events)


def test_captions_use_display_text_fold_hidden_words_and_overlays():
    s = Settings(caption_uppercase=False)
    words = [Word(0.0, 0.3, "fifty", display="$5,500"), Word(0.3, 0.6, "five", display=""),
             Word(0.6, 0.9, "hundred", display=""), Word(1.5, 1.9, "later."), Word(2.0, 2.4, "we", display=None)]
    overlays = {"hook": (2.5, "The $5,500 bet"), "callouts": [(0.0, 2.0, "$5,500 in repairs")], "cta": (8.0, 10.0, "Follow")}
    ass = captions.build(words, (1080, 1920), s, overlays)
    events = [line for line in ass.splitlines() if line.startswith("Dialogue")]
    word_events = [e for e in events if ",Word," in e]
    assert len(word_events) == 3 and "$5,500" in word_events[0] and "fifty" not in ass
    assert "We" in word_events[2]  # capitalized after the sentence end
    assert any(",Hook," in e and "The $5,500 bet" in e for e in events)
    assert any(",Callout," in e for e in events) and any(",CTA," in e and "Follow" in e for e in events)
