"""Auto-update from GitHub Releases.

The release repo is baked into installed builds by CI (vflow/_build.py). Each release carries a
`vflow-<version>-setup.exe`; updating downloads it and runs it silently. The installer closes the
running app, replaces it, and relaunches it.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Callable

from .. import __version__

try:
    from .._build import UPDATE_REPO  # type: ignore[import-not-found]
except ImportError:
    UPDATE_REPO = ""


def _ver(v: str) -> tuple[int, ...]:
    parts = []
    for p in v.lstrip("vV").split("."):
        digits = "".join(c for c in p if c.isdigit())
        parts.append(int(digits or 0))
    return tuple(parts)


def enabled() -> bool:
    return bool(UPDATE_REPO) and getattr(sys, "frozen", False) and sys.platform == "win32"


def check() -> dict | None:
    """Return info about a newer release, or None if up to date / updates are off."""
    if not enabled():
        return None
    req = urllib.request.Request(
        f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": f"vflow/{__version__}"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.load(r)
    latest = data.get("tag_name", "")
    if _ver(latest) <= _ver(__version__):
        return None
    asset = next((a for a in data.get("assets", []) if a["name"].lower().endswith("-setup.exe")), None)
    if asset is None:
        return None
    return {
        "version": latest.lstrip("vV"),
        "notes": data.get("body") or "",
        "url": asset["browser_download_url"],
        "size": asset.get("size", 0),
        "sha256": (asset.get("digest") or "").removeprefix("sha256:"),
    }


def download(info: dict, on_progress: Callable[[float], None]) -> Path:
    dest = Path(tempfile.gettempdir()) / f"vflow-{info['version']}-setup.exe"
    req = urllib.request.Request(info["url"], headers={"User-Agent": f"vflow/{__version__}"})
    h = hashlib.sha256()
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or info.get("size") or 0)
        done = 0
        while chunk := r.read(1 << 16):
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if total:
                on_progress(done / total)
    if info.get("sha256") and h.hexdigest() != info["sha256"]:
        dest.unlink(missing_ok=True)
        raise RuntimeError("Downloaded update failed its checksum; try again.")
    return dest


def launch_installer(path: Path) -> None:
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    subprocess.Popen(
        [str(path), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"],
        creationflags=flags, close_fds=True,
    )
