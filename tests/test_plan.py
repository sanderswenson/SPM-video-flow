from vflow import captions
from vflow.config import Settings
from vflow.plan import Plan, drop_fillers, remap, segments
from vflow.transcript import Word, phrases


def make_plan(spec, fps=30.0, duration=None):
    words = [Word(a, b, t) for a, b, t in spec]
    return Plan("in.mp4", duration or spec[-1][1] + 1, fps, words, [True] * len(words))


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
    p.save(tmp_path / "plan.json")
    q = Plan.load(tmp_path / "plan.json")
    assert q.keep == p.keep and q.reasons == p.reasons and q.post == p.post


def test_phrases_break_on_pause_and_punctuation():
    words = [Word(0, 0.3, "Hi."), Word(0.35, 0.6, "So"), Word(0.65, 0.9, "yes"), Word(2.0, 2.3, "next")]
    assert phrases(words) == [[0], [1, 2], [3]]


def test_captions_highlight_each_word():
    s = Settings()
    words = [Word(0.0, 0.3, "make"), Word(0.3, 0.6, "it"), Word(0.6, 1.0, "postable")]
    ass = captions.build(words, (1080, 1920), s)
    events = [line for line in ass.splitlines() if line.startswith("Dialogue")]
    assert len(events) == 3
    assert "PlayResY: 1920" in ass
    assert all("MAKE" in e and "POSTABLE" in e for e in events)
