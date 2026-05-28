"""
Tests for sync.py — pagination cursor parsing, order upsert logic,
sync-type determination, and the in-memory status state machine.

External dependencies (psycopg2, requests) are mocked.
"""

from datetime import date, datetime, timezone
from unittest.mock import patch, MagicMock, call

import pytest

import sync


# ── _parse_next_cursor ───────────────────────────────────────────────────────

class TestParseNextCursor:
    def test_returns_none_for_empty_header(self):
        assert sync._parse_next_cursor("") is None
        assert sync._parse_next_cursor(None) is None

    def test_extracts_page_info_from_rel_next(self):
        link = (
            '<https://x.myshopify.com/admin/api/2024-07/orders.json'
            '?page_info=abc123&limit=250>; rel="next"'
        )
        assert sync._parse_next_cursor(link) == "abc123"

    def test_ignores_rel_previous(self):
        link = (
            '<https://x.myshopify.com/admin/api/2024-07/orders.json'
            '?page_info=prev_cursor&limit=250>; rel="previous"'
        )
        assert sync._parse_next_cursor(link) is None

    def test_picks_next_when_both_rels_present(self):
        link = (
            '<https://x/orders.json?page_info=PREV>; rel="previous", '
            '<https://x/orders.json?page_info=NEXT>; rel="next"'
        )
        assert sync._parse_next_cursor(link) == "NEXT"

    def test_handles_complex_cursor_chars(self):
        link = '<https://x/orders.json?page_info=eyJsYXN0X2lk_xyz>; rel="next"'
        assert sync._parse_next_cursor(link) == "eyJsYXN0X2lk_xyz"

    def test_decodes_url_encoded_cursor(self):
        link = '<https://x/orders.json?page_info=abc%2B123%2F%3D%3D&limit=250>; rel="next"'
        assert sync._parse_next_cursor(link) == "abc+123/=="

    def test_returns_none_when_no_page_info_param(self):
        link = '<https://x/orders.json?limit=250>; rel="next"'
        assert sync._parse_next_cursor(link) is None


# ── _determine_sync_type ─────────────────────────────────────────────────────

class TestDetermineSyncType:
    def test_returns_full_when_no_prior_complete_sync(self):
        with patch("sync.get_db") as mock_db, \
             patch("sync.fetchone", return_value=None):
            mock_db.return_value.__enter__.return_value = object()
            assert sync._determine_sync_type() == ("full", None)

    def test_returns_full_when_prior_watermark_is_null(self):
        with patch("sync.get_db") as mock_db, \
             patch("sync.fetchone", return_value={"watermark": None}):
            mock_db.return_value.__enter__.return_value = object()
            assert sync._determine_sync_type() == ("full", None)

    def test_returns_incremental_with_watermark_iso(self):
        wm = datetime(2025, 1, 15, 12, 30, 0, tzinfo=timezone.utc)
        with patch("sync.get_db") as mock_db, \
             patch("sync.fetchone", return_value={"watermark": wm}):
            mock_db.return_value.__enter__.return_value = object()
            kind, watermark = sync._determine_sync_type()
            assert kind == "incremental"
            assert watermark == wm.isoformat()


# ── _resume_cursor ───────────────────────────────────────────────────────────

class TestResumeCursor:
    def test_returns_none_when_no_interrupted_sync(self):
        with patch("sync.get_db") as mock_db, \
             patch("sync.fetchone", return_value=None):
            mock_db.return_value.__enter__.return_value = object()
            assert sync._resume_cursor() == (None, None)

    def test_returns_id_and_cursor_when_full_sync_running(self):
        with patch("sync.get_db") as mock_db, \
             patch("sync.fetchone", return_value={"id": "abc-uuid", "cursor": "page_3_cursor"}):
            mock_db.return_value.__enter__.return_value = object()
            assert sync._resume_cursor() == ("abc-uuid", "page_3_cursor")

    def test_returns_none_cursor_when_running_but_no_progress(self):
        with patch("sync.get_db") as mock_db, \
             patch("sync.fetchone", return_value={"id": "abc-uuid", "cursor": None}):
            mock_db.return_value.__enter__.return_value = object()
            assert sync._resume_cursor() == (None, None)


# ── Status state machine ────────────────────────────────────────────────────

class TestStatus:
    def setup_method(self):
        sync._set_status(
            running=False, type=None,
            orders_synced=0, refunds_synced=0,
            started_at=None, completed_at=None, error=None,
        )

    def test_get_status_returns_copy(self):
        s1 = sync.get_status()
        s1["running"] = True
        s2 = sync.get_status()
        assert s2["running"] is False, "get_status() must return a copy"

    def test_set_status_partial_update(self):
        sync._set_status(running=True, type="full")
        s = sync.get_status()
        assert s["running"] is True
        assert s["type"] == "full"
        assert s["orders_synced"] == 0  # untouched


# ── trigger_sync / trigger_reaggregate ───────────────────────────────────────

class TestTriggers:
    def setup_method(self):
        sync._set_status(
            running=False, type=None,
            orders_synced=0, refunds_synced=0,
            started_at=None, completed_at=None, error=None,
        )

    def test_trigger_sync_returns_false_when_already_running(self):
        sync._set_status(running=True)
        assert sync.trigger_sync() is False

    def test_trigger_sync_spawns_thread_when_idle(self):
        with patch("sync.threading.Thread") as mock_thread:
            mock_instance = MagicMock()
            mock_thread.return_value = mock_instance
            assert sync.trigger_sync() is True
            mock_thread.assert_called_once()
            # daemon=True so the worker is killed on shutdown
            assert mock_thread.call_args.kwargs["daemon"] is True
            mock_instance.start.assert_called_once()

    def test_trigger_sync_marks_running_before_worker_starts(self):
        with patch("sync.threading.Thread") as mock_thread:
            mock_instance = MagicMock()
            mock_thread.return_value = mock_instance
            assert sync.trigger_sync() is True
            assert sync.trigger_sync() is False
            assert mock_thread.call_count == 1

    def test_trigger_reaggregate_blocked_when_running(self):
        sync._set_status(running=True)
        assert sync.trigger_reaggregate() is False

    def test_trigger_reaggregate_spawns_thread(self):
        with patch("sync.threading.Thread") as mock_thread:
            mock_instance = MagicMock()
            mock_thread.return_value = mock_instance
            assert sync.trigger_reaggregate() is True
            mock_thread.assert_called_once()


# ── _upsert_order: business rules ────────────────────────────────────────────

def _make_fetchone_factory(known_skus, line_item_lookup=None):
    """Return a side_effect callable for sync.fetchone that simulates DB rows.

    The first fetchone call in _upsert_order inserts the order and returns
    the order's UUID. Subsequent calls fall into three categories:
      (a) "SELECT 1 FROM sku_config WHERE sku = %s" → returns {} if known
      (b) "INSERT INTO order_line_items ... RETURNING id" → returns {"id": ...}
      (c) "SELECT id FROM order_line_items WHERE shopify_line_item_id = %s"
          → returns {"id": ...} or None
    """
    order_id_counter = {"n": 0}
    line_id_counter = {"n": 0}

    def _fn(_conn, sql, params=None):
        s = sql.strip()
        if s.startswith("INSERT INTO orders"):
            order_id_counter["n"] += 1
            return {"id": f"order-uuid-{order_id_counter['n']}"}
        if "FROM sku_config WHERE sku" in s:
            sku = params[0]
            return {"?column?": 1} if sku in known_skus else None
        if s.startswith("INSERT INTO order_line_items"):
            line_id_counter["n"] += 1
            return {"id": f"li-uuid-{line_id_counter['n']}"}
        if s.startswith("SELECT id FROM order_line_items"):
            shopify_li_id = params[0]
            if line_item_lookup and shopify_li_id in line_item_lookup:
                return {"id": line_item_lookup[shopify_li_id]}
            return None
        return None

    return _fn


class TestUpsertOrder:
    def test_days_to_refund_uses_order_date_not_refund_date(self):
        """Critical spec: days = refund_date - order_date, computed at write time."""
        order = {
            "id": 1001,
            "created_at": "2024-01-01T10:00:00Z",
            "updated_at": "2024-01-15T10:00:00Z",
            "line_items": [
                {"id": 5001, "sku": "SKU-A", "quantity": 2,
                 "fulfillment_status": "fulfilled"},
            ],
            "refunds": [
                {
                    "id": 9001,
                    "created_at": "2024-01-21T10:00:00Z",  # 20 days later
                    "refund_line_items": [
                        {"id": 7001, "line_item_id": 5001, "quantity": 1,
                         "line_item": {"sku": "SKU-A", "fulfillment_status": "fulfilled"}},
                    ],
                },
            ],
        }

        executed = []
        with patch("sync.fetchone", side_effect=_make_fetchone_factory({"SKU-A"})), \
             patch("sync.execute", side_effect=lambda c, sql, p=None: executed.append((sql, p))):
            ref_count = sync._upsert_order(object(), order)

        assert ref_count == 1
        # Find the refund insert
        refund_inserts = [p for sql, p in executed if "refund_line_items" in sql]
        assert len(refund_inserts) == 1
        params = refund_inserts[0]
        # Tuple layout: (shopify_refund_id[0], shopify_refund_line_item_id[1],
        #   order_line_item_id[2], sku[3], qty_returned[4],
        #   order_date[5], refund_date[6], return_date[7], days_to_refund[8],
        #   restock_type[9], return_id[10], refund_subtotal[11])
        assert params[8] == 20, f"days_to_refund should be 20, got {params[8]}"
        # order_date and refund_date are at indices 5 and 6
        assert params[5] == date(2024, 1, 1)
        assert params[6] == date(2024, 1, 21)

    def test_skips_unfulfilled_line_items(self):
        order = {
            "id": 1002,
            "created_at": "2024-02-01T00:00:00Z",
            "line_items": [
                {"id": 5101, "sku": "SKU-A", "quantity": 1,
                 "fulfillment_status": None},  # NOT fulfilled
                {"id": 5102, "sku": "SKU-A", "quantity": 3,
                 "fulfillment_status": "fulfilled"},
            ],
            "refunds": [],
        }

        fetchone_calls = []

        def _recording_fetchone(c, sql, p=None):
            fetchone_calls.append((sql, p))
            return _make_fetchone_factory({"SKU-A"})(c, sql, p)

        with patch("sync.fetchone", side_effect=_recording_fetchone), \
             patch("sync.execute"):
            sync._upsert_order(object(), order)

        # Only one *fulfilled* line item (id=5102, qty=3) should reach the DB.
        li_inserts = [
            p for sql, p in fetchone_calls
            if "INSERT INTO order_line_items" in sql
        ]
        assert len(li_inserts) == 1, (
            f"Expected exactly 1 order_line_items INSERT, got {len(li_inserts)}"
        )
        # The inserted item must be the fulfilled one (id=5102, qty=3)
        inserted_shopify_li_id = li_inserts[0][0]
        inserted_qty = li_inserts[0][3]
        assert inserted_shopify_li_id == 5102, (
            f"Expected shopify_line_item_id=5102, got {inserted_shopify_li_id}"
        )
        assert inserted_qty == 3, (
            f"Expected quantity=3, got {inserted_qty}"
        )

    def test_skips_unknown_skus(self):
        """An SKU not in sku_config must not be inserted."""
        order = {
            "id": 1003,
            "created_at": "2024-03-01T00:00:00Z",
            "line_items": [
                {"id": 5201, "sku": "UNKNOWN-SKU", "quantity": 1,
                 "fulfillment_status": "fulfilled"},
                {"id": 5202, "sku": "SKU-A", "quantity": 1,
                 "fulfillment_status": "fulfilled"},
            ],
            "refunds": [],
        }

        fetch_calls = []

        fetch_factory = _make_fetchone_factory({"SKU-A"})

        def _fetch(c, sql, p=None):
            fetch_calls.append((sql, p))
            return fetch_factory(c, sql, p)

        with patch("sync.fetchone", side_effect=_fetch), \
             patch("sync.execute"):
            sync._upsert_order(object(), order)

        # The order_line_items INSERT should only have happened for SKU-A.
        li_inserts = [
            p for sql, p in fetch_calls
            if "INSERT INTO order_line_items" in sql
        ]
        assert len(li_inserts) == 1
        assert li_inserts[0][2] == "SKU-A"

    def test_skips_blank_sku(self):
        order = {
            "id": 1004,
            "created_at": "2024-04-01T00:00:00Z",
            "line_items": [
                {"id": 5301, "sku": "", "quantity": 1,
                 "fulfillment_status": "fulfilled"},
                {"id": 5302, "sku": None, "quantity": 1,
                 "fulfillment_status": "fulfilled"},
            ],
            "refunds": [],
        }

        with patch("sync.fetchone", side_effect=_make_fetchone_factory({"SKU-A"})) as mock_f, \
             patch("sync.execute") as mock_e:
            sync._upsert_order(object(), order)

        # No INSERT into order_line_items at all.
        for cl in mock_f.call_args_list:
            sql = cl.args[1]
            assert "INSERT INTO order_line_items" not in sql

    def test_refund_skipped_when_qty_zero(self):
        order = {
            "id": 1005,
            "created_at": "2024-05-01T00:00:00Z",
            "line_items": [
                {"id": 5401, "sku": "SKU-A", "quantity": 2,
                 "fulfillment_status": "fulfilled"},
            ],
            "refunds": [
                {
                    "id": 9101,
                    "created_at": "2024-05-05T00:00:00Z",
                    "refund_line_items": [
                        {"id": 7101, "line_item_id": 5401, "quantity": 0,
                         "line_item": {"sku": "SKU-A", "fulfillment_status": "fulfilled"}},
                    ],
                },
            ],
        }
        with patch("sync.fetchone", side_effect=_make_fetchone_factory({"SKU-A"})), \
             patch("sync.execute") as mock_e:
            count = sync._upsert_order(object(), order)
        assert count == 0
        # No INSERT into refund_line_items
        for cl in mock_e.call_args_list:
            assert "INSERT INTO refund_line_items" not in cl.args[1]

    def test_refund_for_unfulfilled_line_item_is_skipped(self):
        """The refund_line_items.line_item.fulfillment_status must be 'fulfilled'."""
        order = {
            "id": 1006,
            "created_at": "2024-06-01T00:00:00Z",
            "line_items": [
                {"id": 5501, "sku": "SKU-A", "quantity": 2,
                 "fulfillment_status": "fulfilled"},
            ],
            "refunds": [
                {
                    "id": 9201,
                    "created_at": "2024-06-10T00:00:00Z",
                    "refund_line_items": [
                        {"id": 7201, "line_item_id": 5501, "quantity": 1,
                         "line_item": {"sku": "SKU-A", "fulfillment_status": "unfulfilled"}},
                    ],
                },
            ],
        }
        with patch("sync.fetchone", side_effect=_make_fetchone_factory({"SKU-A"})), \
             patch("sync.execute") as mock_e:
            count = sync._upsert_order(object(), order)
        assert count == 0

    def test_refund_for_skipped_line_item_falls_back_to_db_lookup(self):
        """If a refund references a line item not in our map, look it up by shopify id."""
        order = {
            "id": 1007,
            "created_at": "2024-07-01T00:00:00Z",
            "line_items": [
                # NOT included in line_items array — simulates an order
                # whose previously-synced LI we already have in DB.
            ],
            "refunds": [
                {
                    "id": 9301,
                    "created_at": "2024-07-15T00:00:00Z",
                    "refund_line_items": [
                        {"id": 7301, "line_item_id": 5601, "quantity": 1,
                         "line_item": {"sku": "SKU-A", "fulfillment_status": "fulfilled"}},
                    ],
                },
            ],
        }

        with patch("sync.fetchone",
                   side_effect=_make_fetchone_factory(
                       {"SKU-A"}, line_item_lookup={5601: "existing-li-uuid"}
                   )), \
             patch("sync.execute") as mock_e:
            count = sync._upsert_order(object(), order)

        assert count == 1
        refund_inserts = [
            cl for cl in mock_e.call_args_list
            if "INSERT INTO refund_line_items" in cl.args[1]
        ]
        assert len(refund_inserts) == 1

    def test_refund_skipped_when_line_item_truly_unknown(self):
        order = {
            "id": 1008,
            "created_at": "2024-08-01T00:00:00Z",
            "line_items": [],
            "refunds": [
                {
                    "id": 9401,
                    "created_at": "2024-08-15T00:00:00Z",
                    "refund_line_items": [
                        {"id": 7401, "line_item_id": 9999, "quantity": 1,
                         "line_item": {"sku": "SKU-A", "fulfillment_status": "fulfilled"}},
                    ],
                },
            ],
        }
        with patch("sync.fetchone",
                   side_effect=_make_fetchone_factory({"SKU-A"})), \
             patch("sync.execute") as mock_e:
            count = sync._upsert_order(object(), order)
        assert count == 0

    def test_updated_at_falls_back_to_created_at(self):
        """If Shopify omits updated_at, we use created_at."""
        order = {
            "id": 1009,
            "created_at": "2024-09-01T00:00:00Z",
            # no updated_at
            "line_items": [],
            "refunds": [],
        }
        fetchone_calls = []

        factory = _make_fetchone_factory(set())  # no known SKUs; line_items is empty

        def _recording_fetchone(c, sql, p=None):
            fetchone_calls.append((sql, p))
            return factory(c, sql, p)

        with patch("sync.fetchone", side_effect=_recording_fetchone), \
             patch("sync.execute"):
            sync._upsert_order(object(), order)

        order_insert = next(c for c in fetchone_calls if "INSERT INTO orders" in c[0])
        # params[2] is the updated_at value — must equal created_at when missing
        assert order_insert[1][2] == "2024-09-01T00:00:00Z"


# ── _fetch_page rate-limit handling ──────────────────────────────────────────

class TestFetchPage:
    def test_retries_on_429(self):
        mock_429 = MagicMock(status_code=429, headers={"Retry-After": "0"})
        mock_200 = MagicMock(status_code=200,
                             headers={"X-Shopify-Shop-Api-Call-Limit": "2/40"})
        mock_200.raise_for_status = MagicMock()

        with patch("sync.requests.get", side_effect=[mock_429, mock_200]), \
             patch("sync.time.sleep") as mock_sleep:
            resp = sync._fetch_page("http://x", {})
        assert resp is mock_200
        mock_sleep.assert_any_call(0)

    def test_throttles_when_call_limit_exceeded_80_percent(self):
        mock_200 = MagicMock(status_code=200,
                             headers={"X-Shopify-Shop-Api-Call-Limit": "33/40"})
        mock_200.raise_for_status = MagicMock()
        with patch("sync.requests.get", return_value=mock_200), \
             patch("sync.time.sleep") as mock_sleep:
            sync._fetch_page("http://x", {})
        mock_sleep.assert_called_with(0.5)

    def test_no_throttle_under_80_percent(self):
        mock_200 = MagicMock(status_code=200,
                             headers={"X-Shopify-Shop-Api-Call-Limit": "10/40"})
        mock_200.raise_for_status = MagicMock()
        with patch("sync.requests.get", return_value=mock_200), \
             patch("sync.time.sleep") as mock_sleep:
            sync._fetch_page("http://x", {})
        assert not mock_sleep.called

    def test_ignores_malformed_rate_limit_header(self):
        mock_200 = MagicMock(status_code=200,
                             headers={"X-Shopify-Shop-Api-Call-Limit": "not-a-ratio"})
        mock_200.raise_for_status = MagicMock()
        with patch("sync.requests.get", return_value=mock_200), \
             patch("sync.time.sleep") as mock_sleep:
            assert sync._fetch_page("http://x", {}) is mock_200
        mock_sleep.assert_not_called()

    def test_malformed_retry_after_uses_default_delay(self):
        mock_429 = MagicMock(status_code=429, headers={"Retry-After": "later"})
        mock_200 = MagicMock(status_code=200, headers={})
        mock_200.raise_for_status = MagicMock()
        with patch("sync.requests.get", side_effect=[mock_429, mock_200]), \
             patch("sync.time.sleep") as mock_sleep:
            assert sync._fetch_page("http://x", {}) is mock_200
        mock_sleep.assert_called_with(4)

    def test_timeout_retries_with_backoff(self):
        import requests as real_requests
        mock_ok = MagicMock(status_code=200, headers={})
        mock_ok.raise_for_status = MagicMock()
        with patch("sync.requests.get",
                   side_effect=[real_requests.Timeout(), mock_ok]), \
             patch("sync.time.sleep"):
            resp = sync._fetch_page("http://x", {})
        assert resp is mock_ok

    def test_raises_after_max_timeout_retries(self):
        import requests as real_requests
        with patch("sync.requests.get",
                   side_effect=real_requests.Timeout()), \
             patch("sync.time.sleep"):
            with pytest.raises(real_requests.Timeout):
                sync._fetch_page("http://x", {}, max_retries=3)

    def test_propagates_5xx_error(self):
        """A 5xx response triggers raise_for_status which must propagate to the caller."""
        import requests as real_requests
        mock_500 = MagicMock(status_code=500, headers={})
        mock_500.raise_for_status.side_effect = real_requests.HTTPError("500 Server Error")
        with patch("sync.requests.get", return_value=mock_500):
            with pytest.raises(real_requests.HTTPError):
                sync._fetch_page("http://x", {})


# ── Headers helper ───────────────────────────────────────────────────────────

def test_headers_includes_shopify_access_token():
    h = sync._headers()
    assert "X-Shopify-Access-Token" in h
    assert h["X-Shopify-Access-Token"] == "shpat_test_token"


# ── _run_sync_worker orchestration ──────────────────────────────────────────

def _make_sync_worker_patches(
    fetch_page_side_effect=None,
    upsert_order_return=0,
    fail_fetch=False,
):
    """Return a dict of patch targets for _run_sync_worker tests."""
    fake_db_cm = MagicMock()
    fake_db_cm.__enter__ = MagicMock(return_value="fake-conn")
    fake_db_cm.__exit__ = MagicMock(return_value=False)

    return fake_db_cm


class TestRunSyncWorker:
    """Tests for the _run_sync_worker orchestration function."""

    def _base_patches(self):
        """Context-manager helper: apply all standard patches and yield them."""
        from contextlib import ExitStack
        return ExitStack()

    def _run_with_patches(
        self,
        fetch_page_side_effect,
        upsert_order_return_value=1,
    ):
        """Run _run_sync_worker with controlled mocks; return the patched mocks."""
        fake_conn_cm = MagicMock()
        fake_conn_cm.__enter__ = MagicMock(return_value="fake-conn")
        fake_conn_cm.__exit__ = MagicMock(return_value=False)

        mocks = {}

        with patch("sync.get_db", return_value=fake_conn_cm) as m_get_db, \
             patch("sync.fetchone", return_value={"orders_synced": 0, "refunds_synced": 0}) as m_fetchone, \
             patch("sync.execute") as m_execute, \
             patch("sync._fetch_page", side_effect=fetch_page_side_effect) as m_fp, \
             patch("sync._upsert_order", return_value=upsert_order_return_value) as m_uo, \
             patch("sync.run_aggregation") as m_agg, \
             patch("sync._create_sync_log", return_value="sync-log-uuid") as m_csl, \
             patch("sync._complete_sync") as m_cs, \
             patch("sync._fail_sync") as m_fs, \
             patch("sync._determine_sync_type", return_value=("full", None)) as m_dst, \
             patch("sync._resume_cursor", return_value=(None, None)) as m_rc, \
             patch("sync._update_sync_progress") as m_usp:
            mocks.update(
                get_db=m_get_db, fetchone=m_fetchone, execute=m_execute,
                fetch_page=m_fp, upsert_order=m_uo, run_aggregation=m_agg,
                create_sync_log=m_csl, complete_sync=m_cs, fail_sync=m_fs,
                determine_sync_type=m_dst, resume_cursor=m_rc,
                update_sync_progress=m_usp,
            )
            try:
                sync._run_sync_worker()
            except Exception:
                pass  # allow error path tests to capture without re-raising

        return mocks

    def _make_page_responses(self, orders_on_first_page):
        """Build side_effect list: first call returns orders, second returns []."""
        page1 = MagicMock()
        page1.json.return_value = {"orders": orders_on_first_page}
        page1.headers = {}

        page2 = MagicMock()
        page2.json.return_value = {"orders": []}
        page2.headers = {}

        return [page1, page2]

    def test_calls_aggregation_after_page_loop(self):
        """run_aggregation must be called exactly once after the pagination loop."""
        one_order = [{"id": 99, "created_at": "2024-01-01T00:00:00Z",
                      "updated_at": "2024-01-01T00:00:00Z",
                      "line_items": [], "refunds": []}]
        responses = self._make_page_responses(one_order)
        mocks = self._run_with_patches(fetch_page_side_effect=responses)
        mocks["run_aggregation"].assert_called_once()

    def test_marks_complete_on_success(self):
        """_complete_sync must be called when the worker finishes without error."""
        responses = self._make_page_responses([])
        mocks = self._run_with_patches(fetch_page_side_effect=responses)
        mocks["complete_sync"].assert_called_once()
        mocks["fail_sync"].assert_not_called()

    def test_marks_error_on_exception(self):
        """When _fetch_page raises, _fail_sync must be called with the error message."""
        mocks = self._run_with_patches(
            fetch_page_side_effect=RuntimeError("boom")
        )
        mocks["fail_sync"].assert_called_once()
        # The first positional arg after sync_id is the exception
        call_args = mocks["fail_sync"].call_args
        error_arg = call_args.args[1] if call_args.args else call_args.kwargs.get("error_msg")
        assert "boom" in str(error_arg)
        # Status error field should also be set
        status = sync.get_status()
        assert status["error"] == "boom"

    def test_resume_uses_saved_cursor(self):
        """When _resume_cursor returns a cursor, _fetch_page starts at that cursor
        and _create_sync_log is skipped (we reuse the existing sync log row)."""
        fake_conn_cm = MagicMock()
        fake_conn_cm.__enter__ = MagicMock(return_value="fake-conn")
        fake_conn_cm.__exit__ = MagicMock(return_value=False)

        page_resp = MagicMock()
        page_resp.json.return_value = {"orders": []}
        page_resp.headers = {}

        with patch("sync.get_db", return_value=fake_conn_cm), \
             patch("sync.fetchone",
                   return_value={"orders_synced": 5, "refunds_synced": 2}), \
             patch("sync.execute"), \
             patch("sync._fetch_page", return_value=page_resp) as m_fp, \
             patch("sync.run_aggregation"), \
             patch("sync._create_sync_log") as m_csl, \
             patch("sync._complete_sync"), \
             patch("sync._fail_sync"), \
             patch("sync._determine_sync_type", return_value=("full", None)), \
             patch("sync._resume_cursor",
                   return_value=("resume-log-id", "page_cursor_xyz")), \
             patch("sync._update_sync_progress"):
            sync._run_sync_worker()

        m_csl.assert_not_called()
        first_call_params = m_fp.call_args_list[0].args[1]
        assert first_call_params.get("page_info") == "page_cursor_xyz"
        assert first_call_params.get("limit") == 250
