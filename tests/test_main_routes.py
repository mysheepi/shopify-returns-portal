"""
Integration tests for the FastAPI routes in main.py.

Strategy
--------
- Use FastAPI's TestClient against the real app.
- Override the database lifespan init (init_db touches Postgres) by
  patching it during app startup.
- Mock the database.* helpers (query, fetchone, execute, get_db) so we
  exercise route logic without a real DB.
- Verify auth, status codes, SQL parameter construction, and that
  /api/sync/trigger correctly delegates to sync.trigger_sync.
"""

import os
from contextlib import contextmanager
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient


# --- Test fixture: a TestClient with init_db patched out ------------------- #

@pytest.fixture
def client():
    """A TestClient over the real FastAPI app.

    Patches `database.init_db` for the lifespan startup so we don't try to
    connect to Postgres on app boot.
    """
    with patch("main.init_db"):
        import main
        with TestClient(main.app) as c:
            yield c


@pytest.fixture
def auth_headers():
    return {"X-Portal-Password": os.environ["PORTAL_PASSWORD"]}


@pytest.fixture
def fake_db_ctx():
    """A context manager that yields a sentinel 'connection' for get_db()."""
    @contextmanager
    def _ctx():
        yield "fake-conn"
    return _ctx


# ── Health & frontend ────────────────────────────────────────────────────────

def test_health_no_auth_required(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_root_serves_frontend(client):
    r = client.get("/")
    # Either serves the file or, if file missing, 404; the relevant point
    # is no auth required and we don't crash.
    assert r.status_code in (200, 404)


# ── Auth ─────────────────────────────────────────────────────────────────────

def test_auth_verify_rejects_missing_password(client):
    r = client.post("/api/auth/verify")
    assert r.status_code == 401


def test_auth_verify_rejects_wrong_password(client):
    r = client.post("/api/auth/verify",
                    headers={"X-Portal-Password": "wrong"})
    assert r.status_code == 401


def test_auth_verify_accepts_correct_password(client, auth_headers):
    r = client.post("/api/auth/verify", headers=auth_headers)
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_auth_verify_uses_constant_time_compare(client):
    """Spec: secrets.compare_digest is used (not ==)."""
    import main
    import inspect
    src = inspect.getsource(main.auth_verify)
    assert "secrets.compare_digest" in src
    src2 = inspect.getsource(main.require_auth)
    assert "secrets.compare_digest" in src2


# ── Auth-gated endpoints reject when unauthenticated ─────────────────────────

@pytest.mark.parametrize("method,path", [
    ("GET",  "/api/skus"),
    ("GET",  "/api/returns"),
    ("GET",  "/api/returns/export"),
    ("GET",  "/api/sync/status"),
    ("POST", "/api/sync/trigger"),
    ("GET",  "/api/settings"),
])
def test_protected_routes_reject_no_auth(client, method, path):
    r = client.request(method, path)
    assert r.status_code == 401, f"{method} {path} should require auth"


def test_settings_post_rejects_no_auth(client):
    r = client.post("/api/settings", json={"return_buffer_days": 5})
    assert r.status_code == 401


def test_protected_routes_reject_wrong_password(client):
    r = client.get("/api/skus", headers={"X-Portal-Password": "nope"})
    assert r.status_code == 401


# ── /api/skus ────────────────────────────────────────────────────────────────

def test_get_skus_returns_list(client, auth_headers, fake_db_ctx):
    fake_rows = [
        {"sku": "MS.HOME.80x40.A1", "return_window_days": 100},
        {"sku": "MS.MASK.GREY",     "return_window_days": 30},
    ]
    with patch("main.get_db", fake_db_ctx), \
         patch("main.query", return_value=fake_rows) as mock_q:
        r = client.get("/api/skus", headers=auth_headers)
    assert r.status_code == 200
    assert r.json() == fake_rows
    # Verify the SQL is the read-only sku_config query
    sql = mock_q.call_args.args[1]
    assert "sku_config" in sql
    assert "ORDER BY sku" in sql


# ── /api/returns: filter SQL construction ────────────────────────────────────

def test_returns_no_filters(client, auth_headers, fake_db_ctx):
    with patch("main.get_db", fake_db_ctx), \
         patch("main.query", return_value=[]) as mock_q:
        r = client.get("/api/returns", headers=auth_headers)
    assert r.status_code == 200
    sql, params = mock_q.call_args.args[1], mock_q.call_args.args[2]
    assert "WHERE" not in sql
    assert params is None


def test_returns_filter_by_sku_list(client, auth_headers, fake_db_ctx):
    with patch("main.get_db", fake_db_ctx), \
         patch("main.query", return_value=[]) as mock_q:
        r = client.get(
            "/api/returns?skus=SKU-A,SKU-B,SKU-C",
            headers=auth_headers,
        )
    assert r.status_code == 200
    sql, params = mock_q.call_args.args[1], mock_q.call_args.args[2]
    assert "sku = ANY(%s)" in sql
    assert params == [["SKU-A", "SKU-B", "SKU-C"]]


def test_returns_filter_strips_whitespace_and_empty(client, auth_headers, fake_db_ctx):
    with patch("main.get_db", fake_db_ctx), \
         patch("main.query", return_value=[]) as mock_q:
        client.get("/api/returns?skus= SKU-A , ,SKU-B ,", headers=auth_headers)
    params = mock_q.call_args.args[2]
    assert params == [["SKU-A", "SKU-B"]]


def test_returns_filter_month_range(client, auth_headers, fake_db_ctx):
    with patch("main.get_db", fake_db_ctx), \
         patch("main.query", return_value=[]) as mock_q:
        r = client.get(
            "/api/returns?from=2024-07&to=2025-04",
            headers=auth_headers,
        )
    assert r.status_code == 200
    sql, params = mock_q.call_args.args[1], mock_q.call_args.args[2]
    assert "order_month >= DATE_TRUNC('month', %s::DATE)" in sql
    assert "order_month <= DATE_TRUNC('month', %s::DATE)" in sql
    assert params == ["2024-07-01", "2025-04-01"]


def test_returns_combined_filters(client, auth_headers, fake_db_ctx):
    with patch("main.get_db", fake_db_ctx), \
         patch("main.query", return_value=[]) as mock_q:
        client.get(
            "/api/returns?skus=SKU-A&from=2024-07&to=2025-04",
            headers=auth_headers,
        )
    params = mock_q.call_args.args[2]
    assert params == [["SKU-A"], "2024-07-01", "2025-04-01"]


def test_returns_select_includes_all_documented_columns(client, auth_headers, fake_db_ctx):
    with patch("main.get_db", fake_db_ctx), \
         patch("main.query", return_value=[]) as mock_q:
        client.get("/api/returns", headers=auth_headers)
    sql = mock_q.call_args.args[1]
    for col in ["sku", "order_month", "return_window_days", "total_ordered",
                "returned_30d", "return_rate_30d", "is_30d_closed",
                "returned_100d", "return_rate_100d", "is_100d_closed"]:
        assert col in sql


def test_returns_returns_list_of_dicts(client, auth_headers, fake_db_ctx):
    fake_rows = [
        {"sku": "S1", "order_month": "2024-07",
         "return_window_days": 30, "total_ordered": 10,
         "returned_30d": 1, "return_rate_30d": 0.1, "is_30d_closed": True,
         "returned_100d": 1, "return_rate_100d": 0.1, "is_100d_closed": False},
    ]
    with patch("main.get_db", fake_db_ctx), \
         patch("main.query", return_value=fake_rows):
        r = client.get("/api/returns", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["sku"] == "S1"


# ── /api/returns/export ──────────────────────────────────────────────────────

def test_export_returns_csv(client, auth_headers, fake_db_ctx):
    fake_rows = [{
        "sku": "S1", "order_month": "2024-07",
        "return_window_days": 30, "total_ordered": 10,
        "returned_30d": 2, "return_rate_30d": 0.2, "is_30d_closed": True,
        "returned_100d": 2, "return_rate_100d": 0.2, "is_100d_closed": False,
    }]
    with patch("main.get_db", fake_db_ctx), \
         patch("main.query", return_value=fake_rows):
        r = client.get("/api/returns/export", headers=auth_headers)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    body = r.text
    # Header row + data row
    assert "sku,order_month,return_window_days" in body
    assert "S1,2024-07" in body


# ── /api/sync/status ─────────────────────────────────────────────────────────

def test_sync_status_combines_live_and_last_completed(client, auth_headers, fake_db_ctx):
    fake_status = {"running": False, "type": None,
                   "orders_synced": 0, "refunds_synced": 0,
                   "started_at": None, "completed_at": None, "error": None}
    fake_last = {"sync_type": "full", "status": "complete",
                 "orders_synced": 1234, "refunds_synced": 56,
                 "started_at": "2025-05-01T10:00:00Z",
                 "completed_at": "2025-05-01T10:30:00Z",
                 "error_message": None}

    with patch("main.get_status", return_value=fake_status), \
         patch("main.get_db", fake_db_ctx), \
         patch("main.fetchone", return_value=fake_last):
        r = client.get("/api/sync/status", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["live"] == fake_status
    assert body["last_completed"]["orders_synced"] == 1234


def test_sync_status_handles_no_prior_completed_sync(client, auth_headers, fake_db_ctx):
    fake_status = {"running": False, "type": None,
                   "orders_synced": 0, "refunds_synced": 0,
                   "started_at": None, "completed_at": None, "error": None}
    with patch("main.get_status", return_value=fake_status), \
         patch("main.get_db", fake_db_ctx), \
         patch("main.fetchone", return_value=None):
        r = client.get("/api/sync/status", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["last_completed"] is None


# ── /api/sync/trigger ────────────────────────────────────────────────────────

def test_sync_trigger_starts_when_idle(client, auth_headers):
    with patch("main.trigger_sync", return_value=True) as mock_t:
        r = client.post("/api/sync/trigger", headers=auth_headers)
    assert r.status_code == 200
    assert r.json() == {"started": True}
    mock_t.assert_called_once()


def test_sync_trigger_returns_409_when_already_running(client, auth_headers):
    with patch("main.trigger_sync", return_value=False):
        r = client.post("/api/sync/trigger", headers=auth_headers)
    assert r.status_code == 409


# ── /api/settings GET ────────────────────────────────────────────────────────

def test_get_settings_returns_default_when_no_row(client, auth_headers, fake_db_ctx):
    with patch("main.get_db", fake_db_ctx), \
         patch("main.fetchone", return_value=None):
        r = client.get("/api/settings", headers=auth_headers)
    assert r.status_code == 200
    assert r.json() == {"return_buffer_days": 10}


def test_get_settings_returns_stored_value(client, auth_headers, fake_db_ctx):
    with patch("main.get_db", fake_db_ctx), \
         patch("main.fetchone", return_value={"return_buffer_days": 25}):
        r = client.get("/api/settings", headers=auth_headers)
    assert r.json() == {"return_buffer_days": 25}


# ── /api/settings POST ───────────────────────────────────────────────────────

def test_post_settings_persists_value_and_reaggregates(client, auth_headers, fake_db_ctx):
    with patch("main.get_db", fake_db_ctx), \
         patch("main.execute") as mock_exec, \
         patch("main.trigger_reaggregate", return_value=True):
        r = client.post("/api/settings", headers=auth_headers,
                        json={"return_buffer_days": 15})
    assert r.status_code == 200
    assert r.json() == {"saved": True, "reaggregating": True}
    sql, params = mock_exec.call_args.args[1], mock_exec.call_args.args[2]
    assert "UPDATE settings" in sql
    assert params == ("15",)


def test_post_settings_rejects_negative(client, auth_headers, fake_db_ctx):
    with patch("main.get_db", fake_db_ctx), patch("main.execute"):
        r = client.post("/api/settings", headers=auth_headers,
                        json={"return_buffer_days": -1})
    assert r.status_code == 400


def test_post_settings_rejects_too_large(client, auth_headers, fake_db_ctx):
    with patch("main.get_db", fake_db_ctx), patch("main.execute"):
        r = client.post("/api/settings", headers=auth_headers,
                        json={"return_buffer_days": 61})
    assert r.status_code == 400


def test_post_settings_accepts_zero_and_sixty(client, auth_headers, fake_db_ctx):
    """Boundaries — 0 and 60 are inclusive per the validator."""
    with patch("main.get_db", fake_db_ctx), \
         patch("main.execute"), \
         patch("main.trigger_reaggregate", return_value=True):
        assert client.post("/api/settings", headers=auth_headers,
                           json={"return_buffer_days": 0}).status_code == 200
        assert client.post("/api/settings", headers=auth_headers,
                           json={"return_buffer_days": 60}).status_code == 200


def test_post_settings_rejects_non_int(client, auth_headers):
    r = client.post("/api/settings", headers=auth_headers,
                    json={"return_buffer_days": "not-a-number"})
    assert r.status_code == 422


def test_post_settings_reaggregating_false_when_already_running(
        client, auth_headers, fake_db_ctx):
    with patch("main.get_db", fake_db_ctx), \
         patch("main.execute"), \
         patch("main.trigger_reaggregate", return_value=False):
        r = client.post("/api/settings", headers=auth_headers,
                        json={"return_buffer_days": 20})
    assert r.json()["reaggregating"] is False
