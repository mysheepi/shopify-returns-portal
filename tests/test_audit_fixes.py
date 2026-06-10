"""
Regression tests for the June 2026 audit fixes.

F1  settings.value widened to TEXT (migration 008) — the threshold-override
    JSON exceeds VARCHAR(255) once ~7 SKUs are overridden, which made saving
    settings fail with a truncation error.
F2  Forced full resync no longer wipes the watermark when a sync is already
    running: the destructive UPDATE used to run in the route handler BEFORE
    the 409 check, so a rejected trigger still destroyed the watermark.
F3  Completed syncs persist a watermark taken at sync START, not completion —
    orders updated mid-sync are re-scanned by the next incremental run.
F4  run_aggregation deletes sku_monthly_stats rows for SKUs that were removed
    from sku_config (migration 007 left orphans that /api/returns kept
    serving forever).
F5  _fetch_return_dates logs a warning instead of silently swallowing errors.
F6  _fetch_page retries on ConnectionError, not just Timeout.
F7  The '_default' threshold source reports 'default' until it has actually
    been saved (it used to always report 'override').
"""

import os
import re
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import requests
from fastapi.testclient import TestClient

import sync
import aggregate


MIGRATION_008 = (Path(__file__).resolve().parent.parent
                 / "migrations" / "008_settings_text_and_stats_cleanup.sql")


@pytest.fixture
def client():
    with patch("main.init_db"):
        import main
        with TestClient(main.app) as c:
            yield c


@pytest.fixture
def auth_headers():
    return {"X-Portal-Password": os.environ["PORTAL_PASSWORD"]}


@pytest.fixture
def fake_db_ctx():
    @contextmanager
    def _ctx():
        yield "fake-conn"
    return _ctx


@pytest.fixture(autouse=True)
def reset_sync_status():
    """Each test starts and ends with the in-memory sync status idle."""
    def _reset():
        sync._set_status(
            running=False, type=None,
            orders_synced=0, refunds_synced=0,
            started_at=None, completed_at=None, error=None,
        )
    _reset()
    yield
    _reset()


# ── F1: migration 008 ────────────────────────────────────────────────────────

class TestMigration008:
    @pytest.fixture(scope="class")
    def sql(self):
        return MIGRATION_008.read_text()

    def test_widens_settings_value_to_text(self, sql):
        assert re.search(
            r"ALTER TABLE settings\s+ALTER COLUMN value TYPE TEXT", sql
        ), "settings.value must be widened to TEXT for threshold JSON"

    def test_deletes_orphaned_monthly_stats(self, sql):
        normalized = re.sub(r"\s+", " ", sql)
        assert "DELETE FROM sku_monthly_stats" in normalized
        assert "NOT IN (SELECT sku FROM sku_config)" in normalized

    def test_migration_runs_after_007(self):
        """init_db applies migrations in sorted filename order."""
        migrations = sorted(
            f.name for f in MIGRATION_008.parent.glob("*.sql")
        )
        assert migrations.index(MIGRATION_008.name) > migrations.index(
            "007_final_sku_list.sql"
        )


# ── F2: forced resync vs. running sync ───────────────────────────────────────

class TestForcedResync:
    def test_force_clears_watermark_and_stale_running_rows(self, fake_db_ctx):
        with patch("sync.get_db", fake_db_ctx), \
             patch("sync.execute") as m_exec, \
             patch("sync.threading.Thread") as m_thread:
            m_thread.return_value = MagicMock()
            assert sync.trigger_sync(force=True) is True

        sqls = " ".join(re.sub(r"\s+", " ", c.args[1]) for c in m_exec.call_args_list)
        assert "SET watermark = NULL" in sqls
        assert "status = 'error'" in sqls, (
            "force must abandon interrupted full syncs so the new sync "
            "starts from scratch instead of resuming a stale cursor"
        )

    def test_force_rejected_when_running_leaves_db_untouched(self):
        """The original bug: a 409-rejected force trigger still wiped the
        watermark, silently turning the NEXT normal sync into a full resync."""
        sync._set_status(running=True)
        with patch("sync.get_db") as m_db, patch("sync.execute") as m_exec:
            assert sync.trigger_sync(force=True) is False
        m_db.assert_not_called()
        m_exec.assert_not_called()

    def test_force_failure_resets_running_flag(self, fake_db_ctx):
        with patch("sync.get_db", fake_db_ctx), \
             patch("sync.execute", side_effect=RuntimeError("db down")):
            with pytest.raises(RuntimeError):
                sync.trigger_sync(force=True)
        assert sync.get_status()["running"] is False

    def test_plain_trigger_does_not_touch_watermark(self):
        with patch("sync.get_db") as m_db, \
             patch("sync.execute") as m_exec, \
             patch("sync.threading.Thread") as m_thread:
            m_thread.return_value = MagicMock()
            assert sync.trigger_sync() is True
        m_db.assert_not_called()
        m_exec.assert_not_called()

    def test_endpoint_delegates_force_flag(self, client, auth_headers):
        with patch("main.trigger_sync", return_value=True) as m_t:
            r = client.post("/api/sync/trigger?force=true", headers=auth_headers)
        assert r.status_code == 200
        m_t.assert_called_once_with(force=True)

    def test_endpoint_default_is_not_forced(self, client, auth_headers):
        with patch("main.trigger_sync", return_value=True) as m_t:
            client.post("/api/sync/trigger", headers=auth_headers)
        m_t.assert_called_once_with(force=False)

    def test_endpoint_409_runs_no_sql(self, client, auth_headers):
        with patch("main.trigger_sync", return_value=False), \
             patch("main.get_db") as m_db, \
             patch("main.execute") as m_exec:
            r = client.post("/api/sync/trigger?force=true", headers=auth_headers)
        assert r.status_code == 409
        m_db.assert_not_called()
        m_exec.assert_not_called()


# ── F3: watermark anchored at sync start ─────────────────────────────────────

class TestWatermarkAtSyncStart:
    def _empty_page(self):
        page = MagicMock()
        page.json.return_value = {"orders": []}
        page.headers = {}
        return page

    def test_complete_sync_uses_provided_watermark(self, fake_db_ctx):
        wm = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with patch("sync.get_db", fake_db_ctx), patch("sync.execute") as m_exec:
            sync._complete_sync("log-id", 3, 1, wm)
        params = m_exec.call_args[0][2]
        assert params[0] is wm

    def test_complete_sync_defaults_to_now_when_omitted(self, fake_db_ctx):
        before = datetime.now(timezone.utc)
        with patch("sync.get_db", fake_db_ctx), patch("sync.execute") as m_exec:
            sync._complete_sync("log-id", 0, 0)
        after = datetime.now(timezone.utc)
        params = m_exec.call_args[0][2]
        assert before <= params[0] <= after

    def test_worker_passes_start_time_as_watermark(self, fake_db_ctx):
        before = datetime.now(timezone.utc)
        with patch("sync.get_db", fake_db_ctx), \
             patch("sync.fetchone", return_value=None), \
             patch("sync.execute"), \
             patch("sync._fetch_page", return_value=self._empty_page()), \
             patch("sync.run_aggregation"), \
             patch("sync._create_sync_log", return_value="log-id"), \
             patch("sync._complete_sync") as m_cs, \
             patch("sync._determine_sync_type", return_value=("full", None)), \
             patch("sync._resume_cursor", return_value=(None, None)), \
             patch("sync._update_sync_progress"):
            sync._run_sync_worker()
        after = datetime.now(timezone.utc)

        m_cs.assert_called_once()
        wm = m_cs.call_args.args[3]
        assert before <= wm <= after, (
            "watermark must be captured when the sync starts, not on completion"
        )

    def test_resumed_sync_uses_original_started_at(self, fake_db_ctx):
        original_start = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
        resume_row = {"orders_synced": 5, "refunds_synced": 2,
                      "started_at": original_start}
        with patch("sync.get_db", fake_db_ctx), \
             patch("sync.fetchone", return_value=resume_row), \
             patch("sync.execute"), \
             patch("sync._fetch_page", return_value=self._empty_page()), \
             patch("sync.run_aggregation"), \
             patch("sync._create_sync_log") as m_csl, \
             patch("sync._complete_sync") as m_cs, \
             patch("sync._determine_sync_type", return_value=("full", None)), \
             patch("sync._resume_cursor", return_value=("old-log-id", "cursor-x")), \
             patch("sync._update_sync_progress"):
            sync._run_sync_worker()

        m_csl.assert_not_called()
        assert m_cs.call_args.args[3] is original_start


# ── F4: orphaned sku_monthly_stats cleanup ───────────────────────────────────

class TestOrphanedStatsCleanup:
    def test_aggregation_deletes_orphans_before_upsert(self):
        calls = []

        def _capture(_conn, sql, params=None):
            calls.append(re.sub(r"\s+", " ", sql))

        with patch("aggregate.fetchone", return_value={"days": 10}), \
             patch("aggregate.execute", side_effect=_capture):
            aggregate.run_aggregation(object())

        assert len(calls) == 2
        assert "DELETE FROM sku_monthly_stats" in calls[0]
        assert "NOT IN (SELECT sku FROM sku_config)" in calls[0]
        assert "INSERT INTO sku_monthly_stats" in calls[1]


# ── F5: return-date fetch failures are logged ────────────────────────────────

class TestReturnDateFetchLogging:
    def test_logs_warning_and_returns_empty_on_failure(self, caplog):
        with patch("sync._fetch_page", side_effect=RuntimeError("shopify 500")):
            with caplog.at_level(logging.WARNING):
                out = sync._fetch_return_dates(12345, {678})
        assert out == {}
        messages = [r.getMessage() for r in caplog.records]
        assert any("12345" in m and "return dates" in m for m in messages)


# ── F6: connection errors are retried ────────────────────────────────────────

class TestFetchPageConnectionRetry:
    def _ok_response(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {}
        resp.raise_for_status = MagicMock()
        return resp

    def test_retries_connection_error_then_succeeds(self):
        ok = self._ok_response()
        with patch("sync.requests.get",
                   side_effect=[requests.ConnectionError("reset"), ok]), \
             patch("sync.time.sleep") as m_sleep:
            resp = sync._fetch_page("http://example", {})
        assert resp is ok
        m_sleep.assert_called_once()

    def test_raises_after_exhausting_retries(self):
        with patch("sync.requests.get",
                   side_effect=requests.ConnectionError("reset")), \
             patch("sync.time.sleep"):
            with pytest.raises(requests.ConnectionError):
                sync._fetch_page("http://example", {}, max_retries=3)


# ── F7: '_default' threshold source reporting ────────────────────────────────

class TestDefaultThresholdSource:
    def test_source_is_default_when_never_saved(self, client, auth_headers, fake_db_ctx):
        with patch("main.get_db", fake_db_ctx), \
             patch("main.fetchone", side_effect=[{"return_buffer_days": 10}, None]), \
             patch("main.query", return_value=[]):
            r = client.get("/api/settings", headers=auth_headers)
        body = r.json()
        assert body["return_rate_threshold_sources"]["_default"]["source"] == "default"
        # The response still exposes the effective fallback for the UI
        assert body["return_rate_threshold_overrides"]["_default"] == 15.0
        assert body["return_rate_thresholds"]["_default"] == 15.0

    def test_source_is_override_when_explicitly_stored(self, client, auth_headers, fake_db_ctx):
        thr_row = {"value": '{"_default": 22.5}'}
        with patch("main.get_db", fake_db_ctx), \
             patch("main.fetchone", side_effect=[{"return_buffer_days": 10}, thr_row]), \
             patch("main.query", return_value=[]):
            r = client.get("/api/settings", headers=auth_headers)
        body = r.json()
        assert body["return_rate_threshold_sources"]["_default"]["source"] == "override"
        assert body["return_rate_thresholds"]["_default"] == 22.5

    def _saved_threshold_json(self, client, auth_headers, fake_db_ctx, thresholds):
        import json
        stored = {}

        def _capture(conn, sql, params=None):
            if params and "return_rate_thresholds" in sql:
                stored["value"] = params[0]

        with patch("main.get_db", fake_db_ctx), \
             patch("main.execute", side_effect=_capture), \
             patch("main.trigger_reaggregate", return_value=True):
            r = client.post("/api/settings", headers=auth_headers,
                            json={"return_buffer_days": 10,
                                  "return_rate_thresholds": thresholds})
        assert r.status_code == 200
        return json.loads(stored["value"])

    def test_save_drops_untouched_default(self, client, auth_headers, fake_db_ctx):
        """The frontend always echoes '_default' back; persisting the built-in
        value would flip the source label to 'override' after the first save."""
        saved = self._saved_threshold_json(
            client, auth_headers, fake_db_ctx, {"_default": 15, "SKU-A": 12})
        assert "_default" not in saved
        assert saved["SKU-A"] == 12.0

    def test_save_keeps_customized_default(self, client, auth_headers, fake_db_ctx):
        saved = self._saved_threshold_json(
            client, auth_headers, fake_db_ctx, {"_default": 22.5})
        assert saved["_default"] == 22.5
