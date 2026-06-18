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
    ("DELETE", "/api/skus/SKU-A"),
    ("GET",  "/api/returns"),
    ("GET",  "/api/returns/export"),
    ("POST", "/api/returns/reaggregate"),
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


def test_sku_writes_reject_no_auth_with_valid_body(client):
    payload = {"sku": "SKU-X", "product_name": "Product X",
               "return_window_days": 30, "is_active": True}
    assert client.post("/api/skus", json=payload).status_code == 401
    assert client.patch("/api/skus/SKU-X", json={
        "product_name": "Product X",
        "return_window_days": 30,
        "is_active": True,
    }).status_code == 401


def test_protected_routes_reject_wrong_password(client):
    r = client.get("/api/skus", headers={"X-Portal-Password": "nope"})
    assert r.status_code == 401


# ── /api/skus ────────────────────────────────────────────────────────────────

def test_get_skus_returns_list(client, auth_headers, fake_db_ctx):
    fake_rows = [
        {"sku": "MS.HOME.80x40.A1", "product_name": "Home Pillow",
         "return_window_days": 100, "is_active": True},
        {"sku": "MS.MASK.GREY", "product_name": "Sleep Mask",
         "return_window_days": 30, "is_active": True},
    ]
    with patch("main.get_db", fake_db_ctx), \
         patch("main.query", return_value=fake_rows) as mock_q:
        r = client.get("/api/skus", headers=auth_headers)
    assert r.status_code == 200
    assert r.json() == fake_rows
    # Verify the SQL is the read-only sku_config query
    sql = mock_q.call_args.args[1]
    assert "sku_config" in sql
    assert "product_name" in sql
    assert "is_active = TRUE" in sql
    assert "ORDER BY product_name, sku" in sql


def test_get_skus_can_include_inactive(client, auth_headers, fake_db_ctx):
    with patch("main.get_db", fake_db_ctx), \
         patch("main.query", return_value=[]) as mock_q:
        r = client.get("/api/skus?include_inactive=true", headers=auth_headers)
    assert r.status_code == 200
    sql = mock_q.call_args.args[1]
    assert "WHERE is_active = TRUE" not in sql


def test_create_sku_upserts_catalog_row(client, auth_headers, fake_db_ctx):
    fake_row = {"sku": "SKU-X", "product_name": "Product X",
                "return_window_days": 30, "is_active": True}
    with patch("main.get_db", fake_db_ctx), \
         patch("main.fetchone", return_value=fake_row) as mock_fetch, \
         patch("main.trigger_reaggregate", return_value=True):
        r = client.post("/api/skus", headers=auth_headers,
                        json=fake_row)
    assert r.status_code == 200
    body = r.json()
    assert body["sku"] == fake_row
    sql, params = mock_fetch.call_args.args[1], mock_fetch.call_args.args[2]
    assert "INSERT INTO sku_config" in sql
    assert "product_name" in sql
    assert params == ("SKU-X", "Product X", 30, True)


def test_update_sku_edits_catalog_row(client, auth_headers, fake_db_ctx):
    fake_row = {"sku": "SKU-X", "product_name": "Product Y",
                "return_window_days": 100, "is_active": False}
    with patch("main.get_db", fake_db_ctx), \
         patch("main.fetchone", return_value=fake_row) as mock_fetch, \
         patch("main.trigger_reaggregate", return_value=False):
        r = client.patch("/api/skus/SKU-X", headers=auth_headers,
                         json={"product_name": "Product Y",
                               "return_window_days": 100,
                               "is_active": False})
    assert r.status_code == 200
    sql, params = mock_fetch.call_args.args[1], mock_fetch.call_args.args[2]
    assert "UPDATE sku_config" in sql
    assert params == ("Product Y", 100, False, "SKU-X")


def test_delete_sku_soft_deactivates(client, auth_headers, fake_db_ctx):
    fake_row = {"sku": "SKU-X", "product_name": "Product X",
                "return_window_days": 30, "is_active": False}
    with patch("main.get_db", fake_db_ctx), \
         patch("main.fetchone", return_value=fake_row) as mock_fetch, \
         patch("main.trigger_reaggregate", return_value=True):
        r = client.delete("/api/skus/SKU-X", headers=auth_headers)
    assert r.status_code == 200
    sql, params = mock_fetch.call_args.args[1], mock_fetch.call_args.args[2]
    assert "SET is_active = FALSE" in sql
    assert params == ("SKU-X",)


def test_sku_save_can_defer_reaggregate(client, auth_headers, fake_db_ctx):
    fake_row = {"sku": "SKU-X", "product_name": "Product X",
                "return_window_days": 30, "is_active": True}
    with patch("main.get_db", fake_db_ctx), \
         patch("main.fetchone", return_value=fake_row), \
         patch("main.trigger_reaggregate") as mock_reagg:
        r = client.post("/api/skus?reaggregate=false", headers=auth_headers,
                        json=fake_row)
    assert r.status_code == 200
    assert r.json()["reaggregating"] is False
    mock_reagg.assert_not_called()


def test_create_sku_rejects_invalid_window(client, auth_headers, fake_db_ctx):
    with patch("main.get_db", fake_db_ctx):
        r = client.post("/api/skus", headers=auth_headers,
                        json={"sku": "SKU-X", "product_name": "Product X",
                              "return_window_days": 45})
    assert r.status_code == 400


# ── /api/returns: filter SQL construction ────────────────────────────────────

def test_returns_no_filters(client, auth_headers, fake_db_ctx):
    with patch("main.get_db", fake_db_ctx), \
         patch("main.query", return_value=[]) as mock_q:
        r = client.get("/api/returns", headers=auth_headers)
    assert r.status_code == 200
    sql, params = mock_q.call_args.args[1], mock_q.call_args.args[2]
    assert "WHERE sc.is_active = TRUE" in sql
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
    assert "sms.sku = ANY(%s)" in sql
    assert params == [["SKU-A", "SKU-B", "SKU-C"]]


def test_returns_filter_strips_whitespace_and_empty(client, auth_headers, fake_db_ctx):
    with patch("main.get_db", fake_db_ctx), \
         patch("main.query", return_value=[]) as mock_q:
        client.get("/api/returns?skus= SKU-A , ,SKU-B ,", headers=auth_headers)
    params = mock_q.call_args.args[2]
    assert params == [["SKU-A", "SKU-B"]]


def test_returns_ignores_blank_sku_filter(client, auth_headers, fake_db_ctx):
    with patch("main.get_db", fake_db_ctx), \
         patch("main.query", return_value=[]) as mock_q:
        r = client.get("/api/returns?skus=,+,+", headers=auth_headers)
    assert r.status_code == 200
    sql, params = mock_q.call_args.args[1], mock_q.call_args.args[2]
    assert "sms.sku = ANY(%s)" not in sql
    assert params is None


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
                "returned_100d", "return_rate_100d", "is_100d_closed",
                "returned_30d_physical", "return_rate_30d_physical",
                "returned_100d_physical", "return_rate_100d_physical",
                "total_revenue", "total_refunded_amount", "refund_rate_monetary"]:
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


def test_reaggregate_endpoint_starts_when_idle(client, auth_headers):
    with patch("main.trigger_reaggregate", return_value=True) as mock_t:
        r = client.post("/api/returns/reaggregate", headers=auth_headers)
    assert r.status_code == 200
    assert r.json() == {"started": True}
    mock_t.assert_called_once()


def test_reaggregate_endpoint_returns_409_when_running(client, auth_headers):
    with patch("main.trigger_reaggregate", return_value=False):
        r = client.post("/api/returns/reaggregate", headers=auth_headers)
    assert r.status_code == 409


# ── /api/settings GET ────────────────────────────────────────────────────────

def _auto_threshold_rows():
    return [
        {"sku": "SKU-A", "return_window_days": 30,
         "threshold_pct": 12.5, "months_count": 12},
        {"sku": "SKU-B", "return_window_days": 100,
         "threshold_pct": 9.25, "months_count": 8},
        {"sku": "SKU-C", "return_window_days": 30,
         "threshold_pct": None, "months_count": 0},
    ]


def test_get_settings_returns_default_when_no_row(client, auth_headers, fake_db_ctx):
    with patch("main.get_db", fake_db_ctx), \
         patch("main.fetchone", return_value=None), \
         patch("main.query", return_value=[]):
        r = client.get("/api/settings", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["return_buffer_days"] == 10
    assert body["return_rate_thresholds"] == {"_default": 15.0}
    assert body["return_rate_threshold_overrides"] == {"_default": 15.0}
    assert "return_rate_threshold_sources" in body


def test_get_settings_returns_stored_value(client, auth_headers, fake_db_ctx):
    # fetchone is called twice: first for buffer, then for thresholds.
    buf_row = {"return_buffer_days": 25}
    thr_row = {"value": '{"_default": 15, "SKU-B": 22}'}
    with patch("main.get_db", fake_db_ctx), \
         patch("main.fetchone", side_effect=[buf_row, thr_row]), \
         patch("main.query", return_value=_auto_threshold_rows()):
        r = client.get("/api/settings", headers=auth_headers)
    body = r.json()
    assert body["return_buffer_days"] == 25
    assert body["return_rate_thresholds"]["SKU-A"] == 12.5
    assert body["return_rate_thresholds"]["SKU-B"] == 22.0
    assert body["return_rate_thresholds"]["SKU-C"] == 15.0
    assert body["return_rate_threshold_sources"]["SKU-A"]["source"] == "auto"
    assert body["return_rate_threshold_sources"]["SKU-B"]["source"] == "override"
    assert body["return_rate_threshold_sources"]["SKU-C"]["source"] == "default"


def test_get_settings_falls_back_when_threshold_json_is_invalid(
        client, auth_headers, fake_db_ctx):
    buf_row = {"return_buffer_days": 25}
    thr_row = {"value": "{not-json"}
    with patch("main.get_db", fake_db_ctx), \
         patch("main.fetchone", side_effect=[buf_row, thr_row]), \
         patch("main.query", return_value=[]):
        r = client.get("/api/settings", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["return_rate_thresholds"] == {"_default": 15.0}


def test_get_settings_auto_threshold_sql_uses_last_12_closed_months(
        client, auth_headers, fake_db_ctx):
    with patch("main.get_db", fake_db_ctx), \
         patch("main.fetchone", side_effect=[{"return_buffer_days": 10}, None]), \
         patch("main.query", return_value=[]) as mock_query:
        r = client.get("/api/settings", headers=auth_headers)
    assert r.status_code == 200
    sql, params = mock_query.call_args.args[1], mock_query.call_args.args[2]
    assert "ROW_NUMBER() OVER" in sql
    assert "rn <= %(months)s" in sql
    assert params == {"months": 12}
    assert "sms.return_rate_30d" in sql
    assert "sms.return_rate_100d" in sql
    assert "return_rate_30d_physical" not in sql
    assert "return_rate_100d_physical" not in sql
    assert "sms.is_30d_closed" in sql
    assert "sms.is_100d_closed" in sql


# ── /api/settings POST ───────────────────────────────────────────────────────

def test_post_settings_persists_value_and_reaggregates(client, auth_headers, fake_db_ctx):
    with patch("main.get_db", fake_db_ctx), \
         patch("main.execute") as mock_exec, \
         patch("main.trigger_reaggregate", return_value=True):
        r = client.post("/api/settings", headers=auth_headers,
                        json={"return_buffer_days": 15})
    assert r.status_code == 200
    assert r.json() == {"saved": True, "reaggregating": True}
    # save_settings does two execute calls: first the buffer upsert, then the thresholds upsert.
    first_call = mock_exec.call_args_list[0]
    sql, params = first_call.args[1], first_call.args[2]
    assert "INSERT INTO settings" in sql
    assert "return_buffer_days" in sql
    assert "ON CONFLICT" in sql
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


def test_post_settings_rejects_invalid_threshold(client, auth_headers, fake_db_ctx):
    with patch("main.get_db", fake_db_ctx), patch("main.execute"):
        r = client.post("/api/settings", headers=auth_headers,
                        json={"return_buffer_days": 10,
                              "return_rate_thresholds": {"SKU-A": 101}})
    assert r.status_code == 400


def test_post_settings_reaggregating_false_when_already_running(
        client, auth_headers, fake_db_ctx):
    with patch("main.get_db", fake_db_ctx), \
         patch("main.execute"), \
         patch("main.trigger_reaggregate", return_value=False):
        r = client.post("/api/settings", headers=auth_headers,
                        json={"return_buffer_days": 20})
    assert r.json()["reaggregating"] is False


# ── Settings thresholds JSON round-trip ──────────────────────────────────────

def test_settings_thresholds_roundtrip(client, auth_headers, fake_db_ctx):
    """POST custom threshold overrides → GET returns the same override dict."""
    custom_thresholds = {"_default": 20, "SKU-A": 12}
    stored_json = {}

    def _capture_execute(conn, sql, params=None):
        if params and "return_rate_thresholds" in sql:
            stored_json["value"] = params[0]

    with patch("main.get_db", fake_db_ctx), \
         patch("main.execute", side_effect=_capture_execute), \
         patch("main.trigger_reaggregate", return_value=True):
        r = client.post("/api/settings", headers=auth_headers,
                        json={"return_buffer_days": 15,
                              "return_rate_thresholds": custom_thresholds})
    assert r.status_code == 200
    assert "value" in stored_json, "execute was never called with thresholds SQL"

    buf_row = {"return_buffer_days": 15}
    thr_row = {"value": stored_json["value"]}
    with patch("main.get_db", fake_db_ctx), \
         patch("main.fetchone", side_effect=[buf_row, thr_row]), \
         patch("main.query", return_value=_auto_threshold_rows()):
        r2 = client.get("/api/settings", headers=auth_headers)

    assert r2.status_code == 200
    body = r2.json()
    assert body["return_rate_threshold_overrides"] == {
        "_default": 20.0,
        "SKU-A": 12.0,
    }
    assert body["return_rate_thresholds"]["SKU-A"] == 12.0
    assert body["return_rate_thresholds"]["SKU-B"] == 9.25


# ── B2: /api/returns response includes physical and monetary columns ─────────

def test_returns_route_includes_physical_and_monetary_columns(
        client, auth_headers, fake_db_ctx):
    fake_row = {
        "sku": "SKU-X",
        "order_month": "2024-07",
        "return_window_days": 30,
        "total_ordered": 10,
        "returned_30d": 1,
        "return_rate_30d": 0.1,
        "is_30d_closed": True,
        "returned_100d": 1,
        "return_rate_100d": 0.1,
        "is_100d_closed": False,
        "returned_30d_physical": 1,
        "return_rate_30d_physical": 0.1,
        "returned_100d_physical": 1,
        "return_rate_100d_physical": 0.1,
        "total_revenue": 500.0,
        "total_refunded_amount": 50.0,
        "refund_rate_monetary": 0.1,
    }
    with patch("main.get_db", fake_db_ctx), \
         patch("main.query", return_value=[fake_row]):
        r = client.get("/api/returns", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    row = body[0]
    for col in [
        "returned_30d_physical", "return_rate_30d_physical",
        "returned_100d_physical", "return_rate_100d_physical",
        "total_revenue", "total_refunded_amount", "refund_rate_monetary",
    ]:
        assert col in row, f"Response missing column: {col}"


# ── B3: CSV export fieldnames must match SELECT columns ──────────────────────

def test_csv_export_fieldnames_match_returns_select(client, auth_headers, fake_db_ctx):
    """Verify no drift between DictWriter fieldnames and SELECT column list."""
    import re
    import inspect
    import main as main_mod

    src = inspect.getsource(main_mod.export_returns)
    # Extract fieldnames=[...] list from source
    fn_match = re.search(r'fieldnames=\[([^\]]+)\]', src, re.DOTALL)
    assert fn_match, "Could not find fieldnames=[...] in export_returns source"
    fn_raw = fn_match.group(1)
    fieldnames = [s.strip().strip('"').strip("'") for s in fn_raw.split(",") if s.strip().strip('"').strip("'")]

    # Extract SELECT columns from get_returns SQL
    get_src = inspect.getsource(main_mod.get_returns)
    sel_match = re.search(r'SELECT\s+(.*?)\s+FROM\s+sku_monthly_stats', get_src, re.DOTALL | re.IGNORECASE)
    assert sel_match, "Could not find SELECT...FROM sku_monthly_stats in get_returns"
    select_text = sel_match.group(1)
    # Extract the aliased/bare column names from the SELECT clause
    select_cols = set()
    for part in select_text.split(","):
        part = part.strip()
        # Handle "expr AS alias" or bare "colname"
        as_match = re.search(r'\bAS\s+(\w+)\s*$', part, re.IGNORECASE)
        if as_match:
            select_cols.add(as_match.group(1))
        else:
            # Bare column reference like "total_ordered" — take last word token
            tokens = re.findall(r'\b[a-zA-Z_]\w*\b', part)
            if tokens:
                select_cols.add(tokens[-1])

    for fn in fieldnames:
        assert fn in select_cols, (
            f"CSV fieldname '{fn}' is not in the SELECT column list; drift detected"
        )


# ── B5: Empty PORTAL_PASSWORD must reject blank header ───────────────────────

def test_empty_portal_password_rejects_blank_header(client):
    with patch("main.PORTAL_PASSWORD", ""):
        r = client.get("/api/returns", headers={"X-Portal-Password": ""})
    assert r.status_code == 401


# ── B6: Invalid month format → 400 ──────────────────────────────────────────

def test_returns_rejects_invalid_from_month(client, auth_headers):
    r = client.get("/api/returns?from=2024-13", headers=auth_headers)
    assert r.status_code == 400


def test_returns_rejects_invalid_to_month(client, auth_headers):
    r = client.get("/api/returns?to=not-a-date", headers=auth_headers)
    assert r.status_code == 400


def test_returns_accepts_valid_month_format(client, auth_headers, fake_db_ctx):
    with patch("main.get_db", fake_db_ctx), \
         patch("main.query", return_value=[]):
        r = client.get("/api/returns?from=2024-01&to=2024-12", headers=auth_headers)
    assert r.status_code == 200
