"""Desktop app entry point: local server + native window (Edge WebView2 on Windows).

    vflow-app                 # open the app window
    vflow-app --browser       # use the default browser instead of a native window
    vflow-app --selftest LOG  # headless end-to-end check used by CI on the packaged build
"""

from __future__ import annotations

import argparse
import sys
import threading
import traceback
import webbrowser
from pathlib import Path

from .. import media
from . import store

VIDEO_PATTERN = "Video files (*.mp4;*.mov;*.m4v;*.mkv;*.webm;*.avi)"


def _redirect_output() -> None:
    # A windowed exe has no console: stdout/stderr are None and any print() would crash.
    if sys.stdout is None or sys.stderr is None:
        log = open(store.log_path(), "a", buffering=1, encoding="utf-8")
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log


def run_window(app, url: str) -> None:
    import webview

    window = webview.create_window(
        "vflow", url, width=1120, height=780, min_size=(820, 600), background_color="#111214"
    )

    open_dialog = getattr(getattr(webview, "FileDialog", None), "OPEN", None) or webview.OPEN_DIALOG
    folder_dialog = getattr(getattr(webview, "FileDialog", None), "FOLDER", None) or webview.FOLDER_DIALOG

    def pick_files() -> list[str]:
        result = window.create_file_dialog(open_dialog, allow_multiple=True, file_types=(VIDEO_PATTERN, "All files (*.*)"))
        return list(result or [])

    def pick_folder() -> str | None:
        result = window.create_file_dialog(folder_dialog)
        return result[0] if result else None

    app.pick_files = pick_files
    app.pick_folder = pick_folder
    app.quit = window.destroy

    def on_loaded() -> None:
        # Native drag-and-drop: pywebview exposes the real file path of dropped files.
        try:
            from webview.dom import DOMEventHandler

            def on_drop(e) -> None:
                files = e.get("dataTransfer", {}).get("files", [])
                paths = [f.get("pywebviewFullPath") for f in files if f.get("pywebviewFullPath")]
                if paths:
                    app.jobs.add(paths)

            window.dom.document.events.dragover += DOMEventHandler(lambda e: None, True, True)
            window.dom.document.events.drop += DOMEventHandler(on_drop, True, True)
        except Exception:
            traceback.print_exc()

    window.events.loaded += on_loaded
    webview.start()


def selftest(log_file: Path, video: Path | None, model: str) -> int:
    """Exercise the packaged app end to end without a window. Returns a process exit code."""
    import json
    import time
    import urllib.request

    from .server import App, make_server

    log = open(log_file, "w", encoding="utf-8")
    sys.stdout = sys.stderr = log
    try:
        import anthropic, ctranslate2, faster_whisper, keyring, webview  # noqa: F401  (bundling check)

        media.require_ffmpeg()
        print("imports + ffmpeg ok")

        app = App()
        app.settings.pipeline.update({"whisper_model": model, "use_llm": False})
        app.settings.output_dir = str(store.data_dir() / "selftest-out")
        server = make_server(app)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]

        def api(method: str, path: str, body: dict | None = None):
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/{path}", method=method,
                data=json.dumps(body).encode() if body is not None else None,
                headers={"X-Token": app.token, "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as r:
            assert app.token.encode() in r.read(), "token not injected into page"
        print("ui page ok, version", api("GET", "state")["version"])

        if video is None:
            video = store.data_dir() / "selftest.mp4"
            media.run(["-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30", "-f", "lavfi",
                       "-i", "sine=f=300:r=48000", "-t", "4", "-c:v", "libx264", "-c:a", "aac", str(video)])
        api("POST", "add", {"paths": [str(video)]})

        deadline = time.time() + 1800
        while time.time() < deadline:
            job = api("GET", "state")["jobs"][0]
            if job["status"] in ("done", "error"):
                break
            time.sleep(1)
        print("job:", json.dumps(job))
        if job["status"] != "done":
            return 1
        info = media.probe(Path(job["out"]))
        print("output:", info)
        assert info.display_size == (1080, 1920)
        plan = api("GET", f"jobs/{job['id']}/plan")
        print("transcript:", " ".join(w["t"] if w["k"] else f"[{w['t']}]" for w in plan["words"]))
        print("SELFTEST PASSED")
        return 0
    except Exception:
        traceback.print_exc()
        return 1
    finally:
        log.flush()


def main() -> None:
    ap = argparse.ArgumentParser(prog="vflow-app")
    ap.add_argument("--browser", action="store_true", help="open in the default browser instead of a window")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--selftest", type=Path, metavar="LOG")
    ap.add_argument("--selftest-video", type=Path)
    ap.add_argument("--selftest-model", default="tiny.en")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(selftest(args.selftest, args.selftest_video, args.selftest_model))

    _redirect_output()
    media.use_bundled_ffmpeg()

    from .server import App, make_server

    app = App()
    server = make_server(app, args.port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"

    if not args.browser:
        try:
            run_window(app, url)
            return
        except Exception:
            traceback.print_exc()  # no WebView available: fall back to the browser
    print(f"vflow running at {url}  (Ctrl+C to quit)")
    webbrowser.open(url)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
