"""Keep the speaker in frame when reframing to another aspect ratio.

The source is sampled a few times a second and the largest face in each sample is recorded
(OpenCV's bundled Haar cascades: no models to download). Each shot of the edit then gets its own
crop: static and centred on the subject when they stay put, a slow speed-limited pan when they
wander. Cuts between shots are where re-framing (and a small alternating punch-in) is free.
"""

from __future__ import annotations

import json
import statistics
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import media
from .config import Settings
from .media import MediaInfo
from .plan import Shot

SAMPLE_FPS = 2.5
ANALYSIS_WIDTH = 480

Box = tuple[float, float, float, float]  # head box, normalised 0..1 of the displayed frame


@dataclass
class Sample:
    t: float
    box: Box | None


# --- detection -----------------------------------------------------------------------------

def available() -> bool:
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
    except ImportError:
        return False
    return True


def _find_face(gray, cascades, min_size: int) -> Box | None:
    import cv2

    h, w = gray.shape
    boxes = []
    for kind, c in cascades:
        views = [(gray, False)]
        if kind == "profile":
            views.append((cv2.flip(gray, 1), True))
        for img, flipped in views:
            for (x, y, bw, bh) in c.detectMultiScale(img, 1.1, 5, minSize=(min_size, min_size)):
                if flipped:
                    x = w - x - bw
                boxes.append((x, y, bw, bh))
        if boxes and kind == "frontal":
            break  # a frontal hit is more reliable than profile; stop early
    if not boxes:
        return None
    x, y, bw, bh = max(boxes, key=lambda b: b[2] * b[3])
    return (x / w, y / h, (x + bw) / w, (y + bh) / h)


def detect(src: Path, info: MediaInfo, cache: Path | None = None,
           on_progress: Callable[[float], None] = lambda p: None) -> list[Sample]:
    """Sample the whole source and locate the subject in each sample. Cached per work folder."""
    if cache is not None and cache.exists():
        try:
            data = json.loads(cache.read_text())
            if abs(data.get("duration", -1) - info.duration) < 0.01:
                return [Sample(d["t"], tuple(d["box"]) if d["box"] else None) for d in data["samples"]]
        except (ValueError, KeyError):
            pass

    import cv2
    import numpy as np

    dw, dh = info.display_size
    w = ANALYSIS_WIDTH
    h = round(dh * w / dw / 2) * 2
    data_dir = cv2.data.haarcascades
    cascades = [
        ("frontal", cv2.CascadeClassifier(data_dir + "haarcascade_frontalface_default.xml")),
        ("frontal", cv2.CascadeClassifier(data_dir + "haarcascade_frontalface_alt2.xml")),
        ("profile", cv2.CascadeClassifier(data_dir + "haarcascade_profileface.xml")),
    ]
    proc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(src), "-an",
         "-vf", f"fps={SAMPLE_FPS},scale={w}:{h},format=gray", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **media.NO_WINDOW,
    )
    assert proc.stdout is not None
    samples: list[Sample] = []
    expected = max(1, int(info.duration * SAMPLE_FPS))
    min_size = max(20, w // 16)
    k = 0
    while True:
        buf = proc.stdout.read(w * h)
        if len(buf) < w * h:
            break
        gray = cv2.equalizeHist(np.frombuffer(buf, np.uint8).reshape(h, w))
        samples.append(Sample(round(k / SAMPLE_FPS, 3), _find_face(gray, cascades, min_size)))
        k += 1
        if k % 20 == 0:
            on_progress(min(1.0, k / expected))
    proc.wait()
    samples = _reject_outliers(samples)
    if cache is not None:
        cache.write_text(json.dumps({"duration": info.duration, "fps": SAMPLE_FPS,
                                     "samples": [{"t": s.t, "box": s.box} for s in samples]}))
    return samples


def _reject_outliers(samples: list[Sample]) -> list[Sample]:
    """Drop one-off detections far from their neighbours (a face-like patch in the background)."""
    out = list(samples)
    n = len(out)
    for k, smp in enumerate(samples):
        if smp.box is None:
            continue
        cx = (smp.box[0] + smp.box[2]) / 2
        neigh = [((s.box[0] + s.box[2]) / 2) for s in samples[max(0, k - 4):k + 5] if s.box is not None and s is not smp]
        if len(neigh) >= 3 and abs(cx - statistics.median(neigh)) > 0.2:
            out[k] = Sample(smp.t, None)
    return out


# --- framing ---------------------------------------------------------------------------------

def base_crop(src_w: int, src_h: int, out_w: int, out_h: int) -> tuple[int, int]:
    """Largest crop of the source with the output's aspect ratio."""
    target = out_w / out_h
    if src_w / src_h > target:
        return round(src_h * target / 2) * 2, src_h
    return src_w, round(src_w / target / 2) * 2


def _even(v: float) -> int:
    return int(v) // 2 * 2


def _fmt(v: float) -> str:
    return f"{v:.3f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v)


def _piecewise(keys: list[tuple[float, float]]) -> str:
    """ffmpeg expression that linearly interpolates (t, value) keyframes; holds outside the range."""
    if len(keys) == 1:
        return _fmt(round(keys[0][1]))
    parts = []
    for (t0, v0), (t1, v1) in zip(keys, keys[1:]):
        slope = (v1 - v0) / (t1 - t0) if t1 > t0 else 0.0
        parts.append(f"if(lt(t,{_fmt(t1)}),{_fmt(v0)}+{_fmt(slope)}*(t-{_fmt(t0)}),")
    return "".join(parts) + _fmt(keys[-1][1]) + ")" * len(parts)


@dataclass
class Framing:
    cw: int
    ch: int
    x: str  # ffmpeg expression in shot-relative t (source pixels)
    y: str

    def filter(self, size: tuple[int, int]) -> str:
        return f"crop={self.cw}:{self.ch}:x='{self.x}':y='{self.y}',scale={size[0]}:{size[1]}:flags=lanczos"


def _subject_px(box: Box, W: int, H: int) -> tuple[float, float, float, float, float]:
    """Face in pixels: (x0, x1, cx, top, cy). x0..x1 is the core of the face that must stay in frame;
    an ear or some hair at the edge is fine, which is what gives a static crop room to work."""
    x0, y0, x1, y1 = box[0] * W, box[1] * H, box[2] * W, box[3] * H
    bw, bh = x1 - x0, y1 - y0
    return x0 + 0.12 * bw, x1 - 0.12 * bw, (x0 + x1) / 2, y0 - 0.3 * bh, (y0 + y1) / 2


def _pan_path(times: list[float], targets: list[float], lo_hi: list[tuple[float, float]], cw: float,
              lo_clamp: float, hi_clamp: float) -> list[tuple[float, float]]:
    """Follow a target crop-left position slowly: dead zone, capped speed, hard limit to keep the subject in."""
    dead, vmax = 0.10 * cw, 0.15 * cw
    # Smooth the targets first (centred moving average over about two seconds).
    smooth = []
    for k in range(len(targets)):
        win = [targets[m] for m in range(len(targets)) if abs(times[m] - times[k]) <= 1.0]
        smooth.append(sum(win) / len(win))
    pos = min(max(smooth[0], lo_clamp), hi_clamp)
    path = [(times[0], pos)]
    for k in range(1, len(times)):
        dt = max(1e-3, times[k] - times[k - 1])
        want = smooth[k]
        if abs(want - pos) > dead:
            step = max(-vmax * dt, min(vmax * dt, want - pos))
            pos += step
        lo, hi = lo_hi[k]
        pos = min(max(pos, lo), hi) if lo <= hi else want  # never let the subject leave the frame
        pos = min(max(pos, lo_clamp), hi_clamp)
        path.append((times[k], pos))
    # Thin plateaus and keep at most ~2 keyframes a second so the expression stays short.
    thinned = [path[0]]
    for k in range(1, len(path) - 1):
        if path[k][0] - thinned[-1][0] >= 0.5 and abs(path[k][1] - thinned[-1][1]) >= 0.5:
            thinned.append(path[k])
    if path[-1][0] > thinned[-1][0]:
        thinned.append(path[-1])
    return thinned


def _median_box(samples: list[Sample]) -> Box | None:
    boxes = [s.box for s in samples if s.box is not None]
    if not boxes:
        return None
    return tuple(statistics.median(b[i] for b in boxes) for i in range(4))  # type: ignore[return-value]


def frame_shot(shot: Shot, samples: list[Sample], info: MediaInfo, size: tuple[int, int], s: Settings,
               zoom: float = 1.0) -> Framing:
    W, H = info.display_size
    cw0, ch0 = base_crop(W, H, *size)
    cw, ch = _even(cw0 / zoom), _even(ch0 / zoom)
    x_max, y_max = W - cw, H - ch

    # Manual framing, or nothing known about the subject: use the configured position.
    def manual() -> Framing:
        return Framing(cw, ch, _fmt(round(x_max * min(max(s.crop_x, 0), 1))), _fmt(round(y_max * min(max(s.crop_y, 0), 1))))

    if s.framing != "auto" or not samples:
        return manual()

    inside = [smp for smp in samples if shot.start - 0.2 <= smp.t <= shot.end + 0.2 and smp.box is not None]
    if not inside:
        near = [smp for smp in samples if smp.box is not None and abs(smp.t - (shot.start + shot.end) / 2) < 6]
        box = _median_box(near) or _median_box(samples)
        if box is None:
            return manual()
        inside = [Sample(shot.start, box)]

    subj = [_subject_px(smp.box, W, H) for smp in inside]  # type: ignore[arg-type]
    times = [max(0.0, smp.t - shot.start) for smp in inside]

    # Horizontal: the crop-left positions that keep the face core inside, per sample, and whether
    # one position does it for the whole shot.
    lo_hi = []
    for x0, x1, cx, _, _ in subj:
        margin = min(0.04 * cw, max(0.0, (cw - (x1 - x0)) / 2))
        a, b = x1 + margin - cw, x0 - margin
        lo_hi.append((a, b) if a <= b else (cx - cw / 2, cx - cw / 2))
    lo, hi = max(a for a, _ in lo_hi), min(b for _, b in lo_hi)
    centre = (min(x0 for x0, *_ in subj) + max(x1 for _, x1, *_ in subj)) / 2
    if x_max <= 0:
        x_expr = "0"
    elif lo <= hi:
        x = centre - cw / 2
        x = min(max(x, lo), hi)
        x_expr = _fmt(round(min(max(x, 0), x_max)))
    else:
        targets = [min(max(cx - cw / 2, 0), x_max) for _, _, cx, _, _ in subj]
        x_expr = _piecewise(_pan_path(times, targets, lo_hi, cw, 0, x_max))

    # Vertical: a static position per shot with the face a little above centre.
    if y_max <= 0:
        y_expr = "0"
    else:
        cy = statistics.median(c for *_, c in subj)
        y_expr = _fmt(round(min(max(cy - 0.42 * ch, 0), y_max)))
    return Framing(cw, ch, x_expr, y_expr)


def frame_shots(shots: list[Shot], samples: list[Sample], info: MediaInfo, size: tuple[int, int],
                s: Settings) -> list[Framing]:
    """One framing per shot. A punch-in alternates at hard cuts so jump cuts read as intentional."""
    out: list[Framing] = []
    punched = False
    prev: Shot | None = None
    for sh in shots:
        hard_cut = prev is not None and (sh.kind != prev.kind or sh.start - prev.end > 1.0 or sh.start < prev.end)
        if hard_cut and s.punch_in > 1.0:
            punched = not punched
        zoom = s.punch_in if punched else 1.0
        out.append(frame_shot(sh, samples, info, size, s, zoom))
        prev = sh
    return out
