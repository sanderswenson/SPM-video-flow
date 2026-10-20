"""Where the app keeps things: per-user folders, saved settings, and the API keys."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, fields
from pathlib import Path

from ..config import Settings

APP = "vflow"
KEYRING_SERVICE = "vflow"
# kind -> (keyring user name, environment variable the pipeline reads, fallback file name)
SECRETS = {
    "anthropic": ("anthropic_api_key", "ANTHROPIC_API_KEY", "api_key"),
    "stock": ("stock_api_key", "VFLOW_STOCK_API_KEY", "stock_api_key"),
}
# Pipeline settings that must never land in settings.json or the UI state.
SECRET_SETTINGS = {"stock_api_key"}


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
        known = {f.name for f in fields(Settings)} - SECRET_SETTINGS
        self.pipeline.update({k: v for k, v in data.items() if k in known})

    def save(self) -> None:
        self.path.write_text(json.dumps(self.to_dict(), indent=1))

    def to_dict(self) -> dict:
        pipeline = {k: v for k, v in asdict(self.pipeline).items() if k not in SECRET_SETTINGS}
        return {**pipeline, "output_dir": self.output_dir, "review": self.review}

    def update(self, values: dict) -> None:
        values = dict(values)
        if "output_dir" in values:
            self.output_dir = str(values.pop("output_dir"))
        if "review" in values:
            self.review = bool(values.pop("review"))
        known = {f.name for f in fields(Settings)} - SECRET_SETTINGS
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


# --- secrets: OS credential store (Windows Credential Manager), falling back to a file. ---

def get_secret(kind: str) -> str | None:
    user, _, file = SECRETS[kind]
    try:
        import keyring

        value = keyring.get_password(KEYRING_SERVICE, user)
        if value:
            return value
    except Exception:
        pass
    f = data_dir() / file
    return f.read_text().strip() if f.exists() else None


def set_secret(kind: str, value: str | None) -> None:
    user, env, file = SECRETS[kind]
    f = data_dir() / file
    try:
        import keyring

        if value:
            keyring.set_password(KEYRING_SERVICE, user, value)
        else:
            try:
                keyring.delete_password(KEYRING_SERVICE, user)
            except Exception:
                pass
        f.unlink(missing_ok=True)
    except Exception:
        if value:
            f.write_text(value)
        else:
            f.unlink(missing_ok=True)
    if value:
        os.environ[env] = value
    else:
        os.environ.pop(env, None)


def get_api_key() -> str | None:
    return get_secret("anthropic")


def set_api_key(key: str | None) -> None:
    set_secret("anthropic", key)


def set_stock_key(key: str | None) -> None:
    set_secret("stock", key)


def load_api_key_into_env() -> None:
    for kind, (_, env, _) in SECRETS.items():
        if not os.environ.get(env):
            value = get_secret(kind)
            if value:
                os.environ[env] = value
