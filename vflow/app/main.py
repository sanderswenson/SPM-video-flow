"""Desktop app entry point: local server + native window (Edge WebView2 on Windows).

    vflow-app                 # open the app window
    vflow-app --browser       # use the default browser instead of a native window
"""

from __future__ import annotations

import argparse
import sys
import threading
import traceback
import webbrowser

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


def main() -> None:
    ap = argparse.ArgumentParser(prog="vflow-app")
    ap.add_argument("--browser", action="store_true", help="open in the default browser instead of a window")
    ap.add_argument("--port", type=int, default=0)
    args = ap.parse_args()

    _redirect_output()
    media.require_ffmpeg()

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
