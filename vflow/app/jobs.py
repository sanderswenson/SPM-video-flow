"""Job queue: one worker thread processes clips in order, the UI polls their state."""

from __future__ import annotations

import copy
import shutil
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..pipeline import make_plan, output_for, render_plan, workdir_for
from ..plan import Plan, segments, summary
from .store import AppSettings, work_root

# Rough share of total time each stage takes, so one bar can span the whole job.
STAGES = {
    "Extracting audio": (0.00, 0.03),
    "Transcribing": (0.03, 0.45),
    "Finding retakes and writing post copy": (0.45, 0.55),
    "Cutting": (0.55, 0.75),
    "Rendering": (0.75, 1.00),
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
    added: float = field(default_factory=time.time)

    @property
    def workdir(self) -> Path:
        return workdir_for(self.src, work_root())

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.src.name, "src": str(self.src), "status": self.status,
            "stage": self.stage, "progress": self.progress, "message": self.message,
            "out": self.out, "post": self.post,
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
        from ..pipeline import VIDEO_EXTS

        skipped = []
        with self.lock:
            existing = {(j.src, j.status) for j in self.jobs}
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
