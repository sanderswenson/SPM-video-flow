"""Where the app keeps things: per-user folders, saved settings, and the API key."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, fields
from pathlib import Path

from ..config import Settings

APP = "vflow"
KEYRING_SERVICE = "vflow"
KEYRING_USER = "anthropic_api_key"


def data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    d = base / APP
    d.mkdir(parents=True, exist_ok=True)
    return d


def work_root() -> Path:
    d = data_dir() / "work"
    d.mkdir(exist_ok=True)
    return d


def log_path() -> Path:
    return data_dir() / "vflow.log"


def default_output_dir() -> Path:
    return Path.home() / "Videos" / "vflow"


class AppSettings:
    """Pipeline Settings plus app-only preferences, persisted as JSON."""

    def __init__(self) -> None:
        self.path = data_dir() / "settings.json"
        self.pipeline = Settings()
        self.output_dir = str(default_output_dir())
        self.review = False  # pause for review before rendering
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        self.output_dir = data.pop("output_dir", self.output_dir)
        self.review = bool(data.pop("review", self.review))
        known = {f.name for f in fields(Settings)}
        self.pipeline.update({k: v for k, v in data.items() if k in known})

    def save(self) -> None:
        self.path.write_text(json.dumps(self.to_dict(), indent=1))

    def to_dict(self) -> dict:
        return {**asdict(self.pipeline), "output_dir": self.output_dir, "review": self.review}

    def update(self, values: dict) -> None:
        values = dict(values)
        if "output_dir" in values:
            self.output_dir = str(values.pop("output_dir"))
        if "review" in values:
            self.review = bool(values.pop("review"))
        known = {f.name for f in fields(Settings)}
        current = asdict(self.pipeline)
        clean = {}
        for k, v in values.items():
            if k not in known:
                continue
            # Coerce to the type of the default so form strings don't sneak in.
            if isinstance(current[k], bool):
                v = bool(v)
            elif isinstance(current[k], float):
                v = float(v)
            elif isinstance(current[k], int):
                v = int(v)
            clean[k] = v
        self.pipeline.update(clean)
        self.save()


# --- API key: OS credential store (Windows Credential Manager), falling back to a file. ---

def _key_file() -> Path:
    return data_dir() / "api_key"


def get_api_key() -> str | None:
    try:
        import keyring

        key = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
        if key:
            return key
    except Exception:
        pass
    f = _key_file()
    return f.read_text().strip() if f.exists() else None


def set_api_key(key: str | None) -> None:
    try:
        import keyring

        if key:
            keyring.set_password(KEYRING_SERVICE, KEYRING_USER, key)
        else:
            try:
                keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
            except Exception:
                pass
        _key_file().unlink(missing_ok=True)
    except Exception:
        if key:
            _key_file().write_text(key)
        else:
            _key_file().unlink(missing_ok=True)
    if key:
        os.environ["ANTHROPIC_API_KEY"] = key
    else:
        os.environ.pop("ANTHROPIC_API_KEY", None)


def load_api_key_into_env() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        key = get_api_key()
        if key:
            os.environ["ANTHROPIC_API_KEY"] = key
