from vflow import framing
from vflow.config import Settings
from vflow.framing import Sample, base_crop, frame_shot, frame_shots
from vflow.media import MediaInfo
from vflow.plan import Shot

INFO = MediaInfo(duration=60.0, width=1920, height=1080, fps=30.0)
OUT = (1080, 1920)


def face(cx, cy=0.5, w=0.25):
    return (cx - w / 2, cy - w * 0.9, cx + w / 2, cy + w * 0.9)


def test_base_crop_matches_target_aspect():
    assert base_crop(1920, 1080, 1080, 1920) == (608, 1080)
    assert base_crop(1080, 1920, 1920, 1080) == (1080, 608)
    assert base_crop(1920, 1080, 1920, 1080) == (1920, 1080)


def test_static_crop_centres_on_subject_extremes():
    samples = [Sample(t / 2, face(0.40 + 0.02 * (t % 3))) for t in range(20)]
    fr = frame_shot(Shot(0.0, 10.0), samples, INFO, OUT, Settings())
    assert (fr.cw, fr.ch) == (608, 1080)
    x = float(fr.x)  # static: a plain number, no expression
    centre = (0.40 + 0.04) / 2 * 1920 + 0.40 * 1920 / 2  # between the extremes
    assert abs(x + 304 - centre) < 40
    assert fr.y == "0"


def test_wandering_subject_gets_a_pan_expression_that_stays_in_bounds():
    samples = [Sample(t / 2.5, face(0.30 + 0.4 * (t / 50))) for t in range(50)]
    fr = frame_shot(Shot(0.0, 20.0), samples, INFO, OUT, Settings())
    assert fr.x.startswith("if(lt(t,")
    import re
    numbers = [float(n) for n in re.findall(r"(?<![\w.])-?\d+(?:\.\d+)?(?=\+)", fr.x)]
    assert numbers and all(0 <= n <= 1920 - 608 for n in numbers)


def test_manual_framing_and_no_samples_fall_back_to_crop_x():
    s = Settings(framing="manual", crop_x=0.0)
    assert frame_shot(Shot(0, 5), [Sample(1, face(0.8))], INFO, OUT, s).x == "0"
    s = Settings(crop_x=1.0)
    assert frame_shot(Shot(0, 5), [], INFO, OUT, s).x == str(1920 - 608)


def test_shot_without_detections_borrows_nearby_ones():
    samples = [Sample(1.0, face(0.7)), Sample(1.4, face(0.7))] + [Sample(t, None) for t in (5.0, 5.4, 5.8)]
    fr = frame_shot(Shot(5.0, 6.0), samples, INFO, OUT, Settings())
    assert abs(float(fr.x) - (0.7 * 1920 - 304)) < 60


def test_punch_in_alternates_only_at_hard_cuts():
    samples = [Sample(t / 2, face(0.45)) for t in range(60)]
    s = Settings(punch_in=1.1)
    sh = [Shot(0, 5), Shot(5.2, 9), Shot(12, 15), Shot(15.1, 18), Shot(25, 28)]
    frs = frame_shots(sh, samples, INFO, OUT, s)
    widths = [f.cw for f in frs]
    assert widths[0] == widths[1] == 608  # a short gap keeps the framing
    assert widths[2] == widths[3] < 608  # a long gap toggles the punch-in
    assert widths[4] == 608
    assert all(f.filter(OUT).startswith(f"crop={f.cw}:{f.ch}:") and "scale=1080:1920" in f.filter(OUT) for f in frs)


def test_piecewise_expression_interpolates():
    expr = framing._piecewise([(0.0, 100.0), (2.0, 300.0), (4.0, 300.0)])
    assert expr == "if(lt(t,2),100+100*(t-0),if(lt(t,4),300+0*(t-2),300))"
