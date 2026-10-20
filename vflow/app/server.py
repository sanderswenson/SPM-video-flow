"""Local HTTP server for the UI: serves the page and a small JSON API on 127.0.0.1.

Every API call must carry the per-launch token (sent only inside the page), and the Host header
must be our own address, so other websites in a browser can't drive the app.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Callable

from .. import __version__, llm, stock
from . import store
from .jobs import JobManager


class App:
    def __init__(self) -> None:
        self.settings = store.AppSettings()
        store.load_api_key_into_env()
        self.jobs = JobManager(self.settings)
        self.token = secrets.token_urlsafe(24)
        # Set by the window layer: native file dialogs.
        self.pick_files: Callable[[], list[str]] | None = None
        self.pick_folder: Callable[[], str | None] | None = None

    # --- API ---------------------------------------------------------------

    def state(self) -> dict:
        return {
            "version": __version__,
            "jobs": self.jobs.snapshot(),
            "settings": self.settings.to_dict(),
            "has_key": llm.available(),
            "has_stock_key": stock.available(self.settings.pipeline),
            "native": self.pick_files is not None,
        }

    def handle(self, method: str, path: str, body: dict) -> object:
        parts = path.strip("/").split("/")[1:]  # drop "api"
        match method, parts:
            case "GET", ["state"]:
                return self.state()
            case "POST", ["add"]:
                return {"skipped": self.jobs.add(body.get("paths", []))}
            case "POST", ["pick"]:
                paths = self.pick_files() if self.pick_files else []
                return {"skipped": self.jobs.add(paths or [])}
            case "POST", ["pick-folder"]:
                folder = self.pick_folder() if self.pick_folder else None
                if folder:
                    self.settings.update({"output_dir": folder})
                return {"folder": folder}
            case "POST", ["settings"]:
                self.settings.update(body)
                return self.settings.to_dict()
            case "POST", ["key"]:
                key = (body.get("key") or "").strip()
                if key:
                    err = llm.check_key(key)
                    if err:
                        return {"ok": False, "error": err}
                store.set_api_key(key or None)
                return {"ok": True}
            case "POST", ["stock-key"]:
                key = (body.get("key") or "").strip()
                if key:
                    err = stock.check_key(self.settings.pipeline.stock_provider, key)
                    if err:
                        return {"ok": False, "error": err}
                store.set_stock_key(key or None)
                return {"ok": True}
            case "POST", ["clear"]:
                self.jobs.clear_finished()
                return {}
            case "GET", ["jobs", job_id, "plan"]:
                plan = self.jobs.load_plan(job_id)
                return {
                    "words": [
                        {"t": w.text, "s": w.start, "e": w.end, "k": k, "r": plan.reasons.get(i, ""),
                         **({"d": w.display} if w.display is not None else {})}
                        for i, (w, k) in enumerate(zip(plan.words, plan.keep))
                    ],
                    "post": plan.post,
                    "overlays": plan.overlays,
                }
            case "POST", ["jobs", job_id, "plan"]:
                return {"summary": self.jobs.save_keep(job_id, body["keep"])}
            case "POST", ["jobs", job_id, "render"]:
                self.jobs.render(job_id)
                return {}
            case "POST", ["jobs", job_id, "retry"]:
                self.jobs.retry(job_id)
                return {}
            case "POST", ["jobs", job_id, "remove"]:
                self.jobs.remove(job_id)
                return {}
            case "POST", ["jobs", job_id, "open"]:
                job = self.jobs.get(job_id)
                target = job.out if body.get("what") == "video" else (job.out or str(job.src))
                reveal(Path(target), select=body.get("what") != "video")
                return {}
            case "POST", ["open-output"]:
                folder = Path(self.settings.output_dir)
                folder.mkdir(parents=True, exist_ok=True)
                reveal(folder, select=False)
                return {}
        raise LookupError(path)


def reveal(path: Path, select: bool) -> None:
    """Open a file with its default app, or show it in the file manager."""
    if sys.platform == "win32":
        if select:
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)] if select else ["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path.parent if select else path)])


def _page(token: str) -> bytes:
    html = resources.files("vflow.app").joinpath("static/index.html").read_text(encoding="utf-8")
    return html.replace("__VFLOW_TOKEN__", token).encode("utf-8")


def make_server(app: App, port: int = 0) -> ThreadingHTTPServer:
    allowed_hosts: set[str] = set()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: object) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _dispatch(self, method: str) -> None:
            if self.headers.get("Host") not in allowed_hosts:
                return self._send(403, b"forbidden", "text/plain")
            path = self.path.split("?", 1)[0]
            if method == "GET" and path in ("/", "/index.html"):
                return self._send(200, _page(app.token), "text/html; charset=utf-8")
            if not path.startswith("/api/"):
                return self._send(404, b"not found", "text/plain")
            if not secrets.compare_digest(self.headers.get("X-Token", ""), app.token):
                return self._send(403, b"forbidden", "text/plain")
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                self._json(200, app.handle(method, path, body))
            except (KeyError, LookupError):
                self._json(404, {"error": "not found"})
            except Exception as e:
                self._json(400, {"error": str(e)})

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    real_port = server.server_address[1]
    allowed_hosts.update({f"127.0.0.1:{real_port}", f"localhost:{real_port}"})
    return server
