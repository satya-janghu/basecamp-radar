"""Route-level tests for the login gate and Basecamp OAuth flow (no database needed)."""

from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.templating import templates


@pytest.fixture
def client(monkeypatch):
    # The base layout queries accounts for the command palette; stub it out.
    monkeypatch.setitem(templates.env.globals, "get_nav_accounts", lambda: [])
    # No lifespan: the scheduler and config seeding need a live database.
    return TestClient(app, base_url="https://testserver")


def login(client):
    resp = client.post("/login", data={"password": "test-password"}, follow_redirects=False)
    assert resp.status_code == 302


@pytest.mark.parametrize("path", ["/", "/briefing", "/manual", "/settings", "/auth/basecamp"])
def test_pages_require_login(client, path):
    resp = client.get(path, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"


def test_disconnect_requires_login(client):
    resp = client.post("/auth/basecamp/disconnect", follow_redirects=False)
    assert resp.headers["location"] == "/login"


def test_wrong_password_is_rejected(client):
    resp = client.post("/login", data={"password": "nope"})
    assert resp.status_code == 200
    assert "Invalid password" in resp.text


def test_manual_renders_after_login(client):
    login(client)
    resp = client.get("/manual")
    assert resp.status_code == 200
    assert "Keyboard Shortcuts" in resp.text


def test_oauth_start_sends_state(client):
    login(client)
    resp = client.get("/auth/basecamp", follow_redirects=False)
    assert resp.status_code == 302
    query = parse_qs(urlparse(resp.headers["location"]).query)
    assert query["type"] == ["web_server"]
    assert query["client_id"] == ["test-client-id"]
    assert query["redirect_uri"] == ["https://testserver/auth/basecamp/callback"]
    assert len(query["state"][0]) >= 32


def test_oauth_callback_rejects_mismatched_state(client, monkeypatch):
    import app.routes.auth as auth_routes

    def fail(*args, **kwargs):
        raise AssertionError("token exchange must not run on a bad state")

    monkeypatch.setattr(auth_routes.httpx, "post", fail)
    login(client)
    client.get("/auth/basecamp", follow_redirects=False)
    resp = client.get("/auth/basecamp/callback?code=abc&state=forged", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/"
