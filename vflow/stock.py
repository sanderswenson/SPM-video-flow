"""Stock photos for B-roll when the creator has none of their own.

The editor pass proposes short visual search queries; this module fetches one photo per query
from Pexels or Unsplash (free API keys) and caches them per clip, so re-renders never refetch.
Everything here is best-effort: a network problem skips the photo, it never fails a render.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .config import Settings

PROVIDERS = ("pexels", "unsplash")
ENV_KEYS = {
    "pexels": ("VFLOW_STOCK_API_KEY", "PEXELS_API_KEY"),
    "unsplash": ("VFLOW_STOCK_API_KEY", "UNSPLASH_ACCESS_KEY"),
}
USER_AGENT = "vflow (https://github.com/sanderswenson/SPM-video-flow)"
TIMEOUT = 20


@dataclass
class Photo:
    path: Path
    query: str
    credit: str  # "Photographer / Pexels"
    page: str  # where the photo lives online


def api_key(s: Settings) -> str | None:
    if s.stock_api_key:
        return s.stock_api_key
    for name in ENV_KEYS.get(s.stock_provider, ()):
        if os.environ.get(name):
            return os.environ[name]
    return None


def available(s: Settings) -> bool:
    return s.stock_provider in PROVIDERS and api_key(s) is not None


def orientation_for(size: tuple[int, int]) -> str:
    w, h = size
    return "portrait" if w < h else "landscape" if w > h else "square"


def _get(url: str, headers: dict | None = None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()


def _search(provider: str, key: str, query: str, orientation: str) -> dict | None:
    """Best match for a query: {"url": image file url, "credit": str, "page": str}, or None."""
    q = urllib.parse.quote(query)
    if provider == "pexels":
        data = json.loads(_get(f"https://api.pexels.com/v1/search?query={q}&per_page=5&orientation={orientation}",
                               {"Authorization": key}))
        for p in data.get("photos", []):
            src = p.get("src", {})
            url = src.get("large2x") or src.get("large") or src.get("original")
            if url:
                return {"url": url, "credit": f"{p.get('photographer', 'Unknown')} / Pexels", "page": p.get("url", "")}
    elif provider == "unsplash":
        o = "squarish" if orientation == "square" else orientation
        headers = {"Authorization": f"Client-ID {key}", "Accept-Version": "v1"}
        data = json.loads(_get(f"https://api.unsplash.com/search/photos?query={q}&per_page=5&orientation={o}", headers))
        for p in data.get("results", []):
            url = p.get("urls", {}).get("regular")
            if url:
                # Unsplash's API guidelines ask apps to report each download.
                if dl := p.get("links", {}).get("download_location"):
                    try:
                        _get(dl, headers)
                    except Exception:
                        pass
                name = p.get("user", {}).get("name", "Unknown")
                return {"url": url, "credit": f"{name} / Unsplash", "page": p.get("links", {}).get("html", "")}
    return None


def _slug(query: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")[:40] or "photo"


def fetch(queries: list[str], s: Settings, cache_dir: Path, orientation: str,
          log=print) -> dict[str, Photo]:
    """One photo per distinct query, from the clip's cache when it has one. Queries with no match are left out."""
    key = api_key(s)
    if not key or not queries:
        return {}
    cache_dir.mkdir(parents=True, exist_ok=True)
    index_file = cache_dir / "index.json"
    index: dict = json.loads(index_file.read_text()) if index_file.exists() else {}
    out: dict[str, Photo] = {}
    for query in dict.fromkeys(q.strip() for q in queries if q.strip()):
        entry = index.get(query)
        if entry and (cache_dir / entry["file"]).exists():
            out[query] = Photo(cache_dir / entry["file"], query, entry["credit"], entry["page"])
            continue
        try:
            hit = _search(s.stock_provider, key, query, orientation)
            if hit is None:
                log(f"  (no {s.stock_provider} photo for {query!r})")
                continue
            data = _get(hit["url"])
        except Exception as e:  # network trouble must never fail a render
            log(f"  (stock photo for {query!r} skipped: {e})")
            continue
        file = f"{_slug(query)}.jpg"
        (cache_dir / file).write_bytes(data)
        index[query] = {"file": file, "credit": hit["credit"], "page": hit["page"], "provider": s.stock_provider}
        index_file.write_text(json.dumps(index, indent=1))
        out[query] = Photo(cache_dir / file, query, hit["credit"], hit["page"])
    return out


def check_key(provider: str, key: str) -> str | None:
    """Return an error message if the key doesn't work, else None."""
    if provider not in PROVIDERS:
        return f"unknown provider {provider!r}"
    try:
        hit = _search(provider, key, "city skyline", "portrait")
    except urllib.error.HTTPError as e:
        return "That key was rejected." if e.code in (401, 403) else f"{provider} API error {e.code}"
    except Exception as e:
        return f"Couldn't reach {provider}: {e}"
    return None if hit else f"{provider} returned no photos; check the key."
