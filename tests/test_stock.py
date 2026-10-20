import json
import urllib.error

import pytest

from vflow import llm, pipeline, stock
from vflow.config import Settings
from vflow.plan import Plan, shots, total_duration
from vflow.transcript import Word


def make_plan(n=60):
    # Continuous speech, one word every half second: the output timeline is the source minus a little padding.
    words = [Word(i * 0.5, i * 0.5 + 0.45, f"w{i}") for i in range(n)]
    return Plan("in.mp4", n * 0.5 + 1, 30.0, words, [True] * n)


def fake_fetch(queries, s, cache_dir, orientation, log=print):
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = {}
    for q in dict.fromkeys(queries):
        f = cache_dir / f"{q}.jpg"
        f.write_bytes(b"x")
        out[q] = stock.Photo(f, q, f"{q} / Pexels", "")
    return out


def no_fetch(*a, **k):
    raise AssertionError("stock.fetch must not be called")


@pytest.fixture
def no_env(monkeypatch):
    for name in ("VFLOW_STOCK_API_KEY", "PEXELS_API_KEY", "UNSPLASH_ACCESS_KEY"):
        monkeypatch.delenv(name, raising=False)


def test_stock_queries_become_spaced_cues_with_credits(tmp_path, monkeypatch, no_env):
    monkeypatch.setattr(pipeline.stock, "fetch", fake_fetch)
    s = Settings(stock_api_key="k", broll_dir="")
    plan = make_plan()
    plan.overlays["broll_queries"] = [
        {"word": 8, "query": "house exterior"},
        {"word": 10, "query": "paint roller"},  # too close to the first cue
        {"word": 30, "query": "backyard mulch"},
        {"word": 44, "query": "house exterior"},  # same photo again
    ]
    sh = shots(plan, s)
    cues, credits = pipeline.broll_cues(plan, sh, s, total_duration(sh), tmp_path, (1080, 1920))
    assert [c[2].name for c in cues] == ["house exterior.jpg", "backyard mulch.jpg"]
    assert all(start >= 3.0 and dur >= 1.0 for start, dur, _ in cues)
    assert cues[1][0] - cues[0][0] >= 4.0
    assert credits == ["house exterior / Pexels", "backyard mulch / Pexels"]
    assert (tmp_path / "broll" / "house exterior.jpg").exists()  # cached under the clip's work folder


def test_own_images_win_over_stock(tmp_path, monkeypatch, no_env):
    monkeypatch.setattr(pipeline.stock, "fetch", no_fetch)
    folder = tmp_path / "photos"
    folder.mkdir()
    (folder / "mine.jpg").write_bytes(b"x")
    s = Settings(stock_api_key="k", broll_dir=str(folder))
    plan = make_plan()
    plan.overlays["broll"] = [{"word": 20, "image": "mine.jpg"}]
    plan.overlays["broll_queries"] = [{"word": 8, "query": "house exterior"}]
    sh = shots(plan, s)
    cues, credits = pipeline.broll_cues(plan, sh, s, total_duration(sh), tmp_path, (1080, 1920))
    assert [c[2].name for c in cues] == ["mine.jpg"] and credits == []


def test_without_a_key_nothing_is_fetched(tmp_path, monkeypatch, no_env):
    monkeypatch.setattr(pipeline.stock, "fetch", no_fetch)
    s = Settings()
    assert not stock.available(s)
    plan = make_plan()
    plan.overlays["broll_queries"] = [{"word": 8, "query": "house exterior"}]
    sh = shots(plan, s)
    assert pipeline.broll_cues(plan, sh, s, total_duration(sh), tmp_path, (1080, 1920)) == ([], [])


def test_key_can_come_from_the_environment(monkeypatch, no_env):
    monkeypatch.setenv("PEXELS_API_KEY", "abc")
    assert stock.api_key(Settings()) == "abc"
    assert stock.api_key(Settings(stock_provider="unsplash")) is None
    monkeypatch.setenv("VFLOW_STOCK_API_KEY", "shared")
    assert stock.api_key(Settings(stock_provider="unsplash")) == "shared"


def test_fetch_downloads_once_and_then_uses_the_cache(tmp_path, monkeypatch):
    s = Settings(stock_api_key="k", stock_provider="pexels")
    calls = []

    def search(provider, key, query, orientation):
        calls.append(("search", provider, query, orientation))
        return {"url": f"https://img/{query}", "credit": "Ann / Pexels", "page": "https://p"}

    def get(url, headers=None):
        calls.append(("get", url))
        return b"JPEGDATA"

    monkeypatch.setattr(stock, "_search", search)
    monkeypatch.setattr(stock, "_get", get)
    photos = stock.fetch(["kitchen", "kitchen ", "yard"], s, tmp_path / "broll", "portrait", log=lambda m: None)
    assert set(photos) == {"kitchen", "yard"}
    assert (tmp_path / "broll" / "kitchen.jpg").read_bytes() == b"JPEGDATA"
    assert [c for c in calls if c[0] == "search"] == [("search", "pexels", "kitchen", "portrait"),
                                                      ("search", "pexels", "yard", "portrait")]
    index = json.loads((tmp_path / "broll" / "index.json").read_text())
    assert index["yard"]["credit"] == "Ann / Pexels"

    monkeypatch.setattr(stock, "_search", lambda *a: (_ for _ in ()).throw(AssertionError("no network expected")))
    monkeypatch.setattr(stock, "_get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no network expected")))
    again = stock.fetch(["kitchen", "yard"], s, tmp_path / "broll", "portrait")
    assert {q: p.path for q, p in again.items()} == {q: p.path for q, p in photos.items()}
    assert again["yard"].credit == "Ann / Pexels"


def test_network_trouble_skips_the_photo(tmp_path, monkeypatch):
    s = Settings(stock_api_key="k")
    monkeypatch.setattr(stock, "_search", lambda *a: (_ for _ in ()).throw(urllib.error.URLError("offline")))
    assert stock.fetch(["kitchen"], s, tmp_path / "broll", "portrait", log=lambda m: None) == {}
    monkeypatch.setattr(stock, "_search", lambda *a: None)
    assert stock.fetch(["kitchen"], s, tmp_path / "broll", "portrait", log=lambda m: None) == {}


def test_search_parses_both_providers(monkeypatch):
    seen = []

    def get(url, headers=None):
        seen.append((url, headers or {}))
        if "api.pexels.com" in url:
            return json.dumps({"photos": [{"photographer": "Ann", "url": "https://pexels.com/p/1",
                                           "src": {"large2x": "https://img/1-large2x", "large": "https://img/1-large"}}]}).encode()
        if "api.unsplash.com/search" in url:
            return json.dumps({"results": [{"urls": {"regular": "https://img/u1"}, "user": {"name": "Bob"},
                                            "links": {"html": "https://unsplash.com/p/u1",
                                                      "download_location": "https://api.unsplash.com/photos/u1/download"}}]}).encode()
        return b"{}"

    monkeypatch.setattr(stock, "_get", get)
    hit = stock._search("pexels", "PK", "house exterior", "portrait")
    assert hit == {"url": "https://img/1-large2x", "credit": "Ann / Pexels", "page": "https://pexels.com/p/1"}
    assert "query=house%20exterior" in seen[0][0] and "orientation=portrait" in seen[0][0]
    assert seen[0][1]["Authorization"] == "PK"
    hit = stock._search("unsplash", "UK", "back yard", "square")
    assert hit == {"url": "https://img/u1", "credit": "Bob / Unsplash", "page": "https://unsplash.com/p/u1"}
    assert "orientation=squarish" in seen[1][0] and seen[1][1]["Authorization"] == "Client-ID UK"
    assert seen[2][0] == "https://api.unsplash.com/photos/u1/download"  # download registered per API guidelines
    monkeypatch.setattr(stock, "_get", lambda url, headers=None: b'{"photos": [], "results": []}')
    assert stock._search("pexels", "PK", "nothing", "portrait") is None
    assert stock._search("unsplash", "UK", "nothing", "portrait") is None


def test_orientation_follows_output_size():
    assert stock.orientation_for((1080, 1920)) == "portrait"
    assert stock.orientation_for((1920, 1080)) == "landscape"
    assert stock.orientation_for((1080, 1080)) == "square"


def test_editor_pass_queries_are_recorded_in_the_plan():
    plan = make_plan(20)
    decision = llm.EditDecision(
        drops=[], restores=[], fixes=[], hook_text="", hook_first_word=-1, hook_last_word=-1, callouts=[], broll=[],
        broll_queries=[llm.BrollQuery(first_word=5, query=" suburban house exterior "),
                       llm.BrollQuery(first_word=99, query="out of range"),
                       llm.BrollQuery(first_word=9, query="  ")],
        cta="", title="t", caption="c", hashtags=["a"],
    )
    llm.apply(plan, decision)
    assert plan.overlays["broll_queries"] == [{"word": 5, "query": "suburban house exterior"}]
