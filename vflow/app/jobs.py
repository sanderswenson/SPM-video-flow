"""Job queue: one worker thread processes clips in order, the UI polls their state.

A job is a raw clip (transcribe, plan, render) or a *variant* of one: the same plan rendered with a
different hook from its hook sheet, either a filmed hook clip prepended or a title over a cold open.
"""

from __future__ import annotations

import copy
import shutil
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..hooks import HookSheet, assign_clips, produce
from ..pipeline import VIDEO_EXTS, make_plan, output_for, render_plan, render_variant, workdir_for
from ..plan import Plan, segments, summary
from .store import AppSettings, work_root

# Rough share of total time each stage takes, so one bar can span the whole job.
STAGES = {
    "Extracting audio": (0.00, 0.03),
    "Transcribing": (0.03, 0.40),
    "Editor pass": (0.40, 0.50),
    "Transcribing hook clip": (0.00, 0.12),
    "Framing": (0.50, 0.58),
    "Cutting": (0.58, 0.76),
    "Rendering": (0.76, 1.00),
}


@dataclass
class Job:
    src: Path
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    # queued -> working -> review -> render_queued -> working -> done | error
    status: str = "queued"
    stage: str = "Waiting"
    progress: float | None = 0.0
    message: str = ""
    out: str = ""
    post: dict = field(default_factory=dict)
    # For a variant: {"n": hook number on the sheet, "title": hook title, "clip": filmed clip path or ""}
    variant: dict | None = None
    added: float = field(default_factory=time.time)

    @property
    def workdir(self) -> Path:
        return workdir_for(self.src, work_root())

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.src.name, "src": str(self.src), "status": self.status,
            "stage": self.stage, "progress": self.progress, "message": self.message,
            "out": self.out, "post": self.post, "variant": self.variant,
        }


class JobManager:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self.jobs: list[Job] = []
        self.lock = threading.Lock()
        self.wake = threading.Event()
        threading.Thread(target=self._loop, daemon=True, name="vflow-worker").start()

    # --- queries -----------------------------------------------------------

    def snapshot(self) -> list[dict]:
        with self.lock:
            return [j.to_dict() for j in self.jobs]

    def get(self, job_id: str) -> Job:
        with self.lock:
            for j in self.jobs:
                if j.id == job_id:
                    return j
        raise KeyError(job_id)

    def busy(self) -> bool:
        with self.lock:
            return any(j.status == "working" for j in self.jobs)

    # --- commands ----------------------------------------------------------

    def add(self, paths: list[str]) -> list[str]:
        """Queue videos; returns names that were skipped."""
        skipped = []
        with self.lock:
            existing = {(j.src, j.status) for j in self.jobs if not j.variant}
            for p in paths:
                src = Path(p)
                if not src.is_file() or src.suffix.lower() not in VIDEO_EXTS:
                    skipped.append(src.name)
                    continue
                if any(s == src and st not in ("done", "error") for s, st in existing):
                    continue
                self.jobs.append(Job(src))
        self.wake.set()
        return skipped

    def remove(self, job_id: str) -> None:
        with self.lock:
            self.jobs = [j for j in self.jobs if not (j.id == job_id and j.status != "working")]

    def clear_finished(self) -> None:
        with self.lock:
            self.jobs = [j for j in self.jobs if j.status not in ("done", "error")]

    def retry(self, job_id: str) -> None:
        job = self.get(job_id)
        if job.status in ("error", "done"):
            job.status, job.stage, job.progress, job.message = "queued", "Waiting", 0.0, ""
            self.wake.set()

    def load_plan(self, job_id: str) -> Plan:
        return Plan.load(self.get(job_id).workdir / "plan.json")

    def save_keep(self, job_id: str, keep: list[bool]) -> str:
        job = self.get(job_id)
        plan = self.load_plan(job_id)
        if len(keep) != len(plan.keep):
            raise ValueError("word count mismatch")
        for i, (old, new) in enumerate(zip(plan.keep, keep)):
            if old != new:
                plan.keep[i] = new
                if new:
                    plan.reasons.pop(i, None)
                else:
                    plan.reasons[i] = "manual"
        plan.save(job.workdir / "plan.json")
        return summary(plan, segments(plan, self.settings.pipeline))

    def render(self, job_id: str) -> None:
        job = self.get(job_id)
        if job.status in ("review", "done", "error"):
            job.status, job.stage, job.progress, job.message = "render_queued", "Waiting to render", 0.0, ""
            self.wake.set()

    # --- hooks -------------------------------------------------------------

    def sheet_path(self, job: Job) -> Path:
        return Path(self.settings.output_dir) / f"{job.src.stem}_hooks.md"

    def hooks(self, job_id: str, regenerate: bool = False) -> HookSheet:
        """The job's hook sheet, written on first request (needs the plan, so the clip must have been processed)."""
        job = self.get(job_id)
        wd = job.workdir
        if not (wd / "plan.json").exists():
            raise ValueError("Process the video first; hooks are matched against its transcript.")
        sheet_file = wd / "hooks.json"
        if sheet_file.exists() and not regenerate:
            return HookSheet.load(sheet_file)
        sheet = produce(Plan.load(wd / "plan.json"), copy.deepcopy(self.settings.pipeline))
        sheet.save(sheet_file)
        md = self.sheet_path(job)
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text(sheet.markdown(job.src.name), encoding="utf-8")
        return sheet

    def assign(self, job_id: str, paths: list[str], picks: list[int] | None = None) -> dict[int, str]:
        """Match filmed hook clips (files or folders) to the sheet's hook numbers."""
        sheet = self.hooks(job_id)
        files: list[Path] = []
        for p in paths:
            path = Path(p)
            if path.is_dir():
                files += sorted(f for f in path.iterdir() if f.suffix.lower() in VIDEO_EXTS)
            elif path.is_file() and path.suffix.lower() in VIDEO_EXTS:
                files.append(path)
        return {n: str(f) for n, f in assign_clips(files, len(sheet.hooks), picks).items()}

    def add_variants(self, job_id: str, picks: list[int], clips: dict[str, str]) -> list[str]:
        """Queue one render per picked hook; `clips` maps hook numbers (as strings) to filmed clip paths."""
        job = self.get(job_id)
        sheet = self.hooks(job_id)
        ids = []
        with self.lock:
            for n in picks:
                if not 1 <= n <= len(sheet.hooks):
                    raise ValueError(f"no hook {n} on the sheet")
                clip = (clips.get(str(n)) or "").strip()
                if clip and not Path(clip).is_file():
                    raise ValueError(f"clip not found: {clip}")
                v = Job(job.src, status="render_queued", stage="Waiting to render",
                        variant={"n": n, "title": sheet.hooks[n - 1].title, "clip": clip})
                self.jobs.append(v)
                ids.append(v.id)
        self.wake.set()
        return ids

    # --- worker ------------------------------------------------------------

    def _next(self) -> Job | None:
        with self.lock:
            for j in self.jobs:
                if j.status in ("queued", "render_queued"):
                    return j
        return None

    def _loop(self) -> None:
        while True:
            job = self._next()
            if job is None:
                self.wake.wait(1.0)
                self.wake.clear()
                continue
            self._run(job)

    def _report(self, job: Job):
        def report(stage: str, fraction: float | None) -> None:
            job.stage = stage
            lo, hi = STAGES.get(stage, (None, None))
            if fraction is None or lo is None:
                job.progress = None if lo is None else lo
            else:
                job.progress = lo + (hi - lo) * fraction
        return report

    def _run(self, job: Job) -> None:
        rendering_only = job.status == "render_queued"
        job.status = "working"
        s = copy.deepcopy(self.settings.pipeline)
        out_dir = Path(self.settings.output_dir)
        report = self._report(job)
        try:
            wd = job.workdir
            if job.variant:
                plan = Plan.load(wd / "plan.json")
                sheet = HookSheet.load(wd / "hooks.json")
                hook = sheet.hooks[job.variant["n"] - 1]
                clip = Path(job.variant["clip"]) if job.variant.get("clip") else None
                out, job.message = render_variant(plan, wd, out_dir, s, hook, clip, report=report)
                job.post = {**plan.post, "title": hook.post_title or hook.title} if plan.post else {}
            else:
                if rendering_only:
                    plan = Plan.load(wd / "plan.json")
                else:
                    plan = make_plan(job.src, wd, s, report=report)
                    plan.save(wd / "plan.json")
                    job.post = plan.post
                    if self.settings.review:
                        job.status, job.stage, job.progress = "review", "Ready for review", None
                        job.message = summary(plan, segments(plan, s))
                        return
                out = output_for(job.src, out_dir)
                job.message = render_plan(plan, wd, out, s, report=report)
                job.post = plan.post
            job.out = str(out)
            job.status, job.stage, job.progress = "done", "Done", 1.0
            shutil.rmtree(wd / "segments", ignore_errors=True)  # big; plan + transcript stay for re-renders
        except Exception as e:  # surface every failure in the UI rather than killing the worker
            traceback.print_exc()
            job.status, job.stage, job.progress = "error", "Failed", None
            job.message = str(e) or e.__class__.__name__
