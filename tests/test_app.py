import http.client
import json
import threading

import pytest


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    from vflow.app.server import App, make_server

    app = App()
    srv = make_server(app)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield app, srv.server_address[1]
    srv.shutdown()


def request(port, method, path, body=None, token=None, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Token"] = token
    if host:
        headers["Host"] = host
    conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
    r = conn.getresponse()
    return r.status, r.read()


def test_page_carries_token(server):
    app, port = server
    status, body = request(port, "GET", "/")
    assert status == 200 and app.token.encode() in body


def test_api_requires_token(server):
    app, port = server
    assert request(port, "GET", "/api/state")[0] == 403
    assert request(port, "GET", "/api/state", token="wrong")[0] == 403
    status, body = request(port, "GET", "/api/state", token=app.token)
    assert status == 200 and "version" in json.loads(body)


def test_foreign_host_rejected(server):
    # DNS rebinding: a page on evil.example resolving to 127.0.0.1 sends its own Host header.
    app, port = server
    assert request(port, "GET", "/", host=f"evil.example:{port}")[0] == 403


def test_settings_are_coerced_and_persisted(server):
    app, port = server
    status, body = request(port, "POST", "/api/settings", {"max_pause": "0.5", "captions": 0, "review": 1,
                                                          "bogus": 1}, token=app.token)
    assert status == 200
    saved = json.loads(app.settings.path.read_text())
    assert saved["max_pause"] == 0.5 and saved["captions"] is False and saved["review"] is True
    assert "bogus" not in saved


def test_stock_key_is_saved_outside_settings(server, monkeypatch):
    app, port = server
    monkeypatch.delenv("VFLOW_STOCK_API_KEY", raising=False)
    monkeypatch.setattr("vflow.stock.check_key", lambda provider, key: None)
    # No OS credential store in tests: force the file fallback inside the temp data dir.
    monkeypatch.setattr("keyring.set_password", lambda *a: (_ for _ in ()).throw(RuntimeError("no keyring")))
    monkeypatch.setattr("keyring.get_password", lambda *a: (_ for _ in ()).throw(RuntimeError("no keyring")))
    status, body = request(port, "POST", "/api/stock-key", {"key": "pexels-key"}, token=app.token)
    assert status == 200 and json.loads(body)["ok"]
    state = json.loads(request(port, "GET", "/api/state", token=app.token)[1])
    assert state["has_stock_key"] is True
    assert "stock_api_key" not in state["settings"]
    request(port, "POST", "/api/settings", {"stock_api_key": "leak", "stock_provider": "unsplash"}, token=app.token)
    saved = json.loads(app.settings.path.read_text())
    assert "stock_api_key" not in saved and saved["stock_provider"] == "unsplash"
    # The key itself lives in the data dir's fallback file, not in settings.json.
    assert (app.settings.path.parent / "stock_api_key").read_text() == "pexels-key"
    status, body = request(port, "POST", "/api/stock-key", {"key": ""}, token=app.token)
    assert json.loads(body)["ok"] and json.loads(request(port, "GET", "/api/state", token=app.token)[1])["has_stock_key"] is False


def test_add_skips_non_videos(server, tmp_path):
    app, port = server
    (tmp_path / "notes.txt").write_text("x")
    status, body = request(port, "POST", "/api/add", {"paths": [str(tmp_path / "notes.txt")]}, token=app.token)
    assert json.loads(body)["skipped"] == ["notes.txt"]
    assert app.jobs.snapshot() == []
