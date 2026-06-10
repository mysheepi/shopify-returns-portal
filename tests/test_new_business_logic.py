"""
Comprehensive tests for new business logic in sync.py and aggregate.py.

Covers:
  - _fetch_return_dates: empty input, normal mapping, exception fallback, ID filtering
  - _upsert_order days_to_refund computation for all 4 scenarios
  - unit_price capture on order_line_items INSERT
  - refund_subtotal capture on refund_line_items INSERT
  - aggregate SQL structural checks for monetary refund fields
"""

import re
from datetime import date
from unittest.mock import patch, MagicMock, call

import pytest

import sync
import aggregate


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_fetchone_factory(known_skus, line_item_lookup=None):
    """Simulate DB fetchone for _upsert_order tests.

    Returns:
      - INSERT INTO orders          → {"id": "order-uuid-1"}
      - SELECT 1 FROM sku_config    → {"?column?": 1} if sku in known_skus
      - INSERT INTO order_line_items → {"id": "li-uuid-<n>"}
      - SELECT id FROM order_line_items → {"id": <value>} if in line_item_lookup
      - anything else               → None
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


def _get_agg_sql():
    """Capture the SQL issued by run_aggregation."""
    captured = {}

    def _capture(_conn, sql, params=None):
        # run_aggregation issues the orphan cleanup first, then the upsert;
        # the upsert is last, so it wins the capture.
        captured["sql"] = sql

    with patch("aggregate.fetchone", return_value={"days": 10}), \
         patch("aggregate.execute", side_effect=_capture):
        aggregate.run_aggregation(object())
    return captured["sql"]


# ── _fetch_return_dates ───────────────────────────────────────────────────────

class TestFetchReturnDates:
    def test_returns_empty_dict_when_no_return_ids(self):
        """No API call when return_ids is empty — returns {} immediately."""
        with patch("sync._fetch_page") as mock_fetch:
            result = sync._fetch_return_dates(1001, set())
        assert result == {}
        mock_fetch.assert_not_called()

    def test_returns_id_to_date_mapping_from_api(self):
        """Happy path: API returns returns; result maps return_id → date."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "returns": [
                {"id": 98765, "created_at": "2024-01-20T10:00:00Z"},
                {"id": 11111, "created_at": "2024-02-05T08:30:00Z"},
            ]
        }
        with patch("sync._fetch_page", return_value=mock_resp):
            result = sync._fetch_return_dates(1001, {98765, 11111})

        assert result[98765] == date(2024, 1, 20)
        assert result[11111] == date(2024, 2, 5)

    def test_returns_empty_dict_on_exception(self):
        """Any exception from the API is swallowed; returns {} as fallback."""
        with patch("sync._fetch_page", side_effect=RuntimeError("network error")):
            result = sync._fetch_return_dates(1001, {98765})
        assert result == {}

    def test_ignores_return_ids_not_in_requested_set(self):
        """API may return more returns than we requested; only keep matching IDs."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "returns": [
                {"id": 98765, "created_at": "2024-01-20T10:00:00Z"},
                {"id": 99999, "created_at": "2024-01-25T10:00:00Z"},  # not requested
            ]
        }
        with patch("sync._fetch_page", return_value=mock_resp):
            result = sync._fetch_return_dates(1001, {98765})

        assert 98765 in result
        assert 99999 not in result

    def test_ignores_returns_with_no_created_at(self):
        """Returns without created_at are silently skipped."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "returns": [
                {"id": 98765},  # no created_at
            ]
        }
        with patch("sync._fetch_page", return_value=mock_resp):
            result = sync._fetch_return_dates(1001, {98765})
        assert result == {}


# ── days_to_refund scenarios ──────────────────────────────────────────────────

class TestDaysToRefundScenarios:
    """Test params[8]=days_to_refund, params[7]=return_date, params[10]=return_id."""

    def _run_order(self, order, fetch_return_dates_retval=None):
        """Execute _upsert_order and return the refund INSERT params."""
        executed = []
        patch_target = fetch_return_dates_retval if fetch_return_dates_retval is not None else {}

        with patch("sync.fetchone", side_effect=_make_fetchone_factory({"SKU-A"})), \
             patch("sync.execute",
                   side_effect=lambda c, sql, p=None: executed.append((sql, p))), \
             patch("sync._fetch_return_dates", return_value=patch_target):
            ref_count = sync._upsert_order(object(), order)

        refund_inserts = [p for sql, p in executed if "refund_line_items" in sql]
        return ref_count, refund_inserts

    def test_scenario_a_physical_return_uses_return_date(self):
        """Scenario A: Physical return — days = return_date - order_date (not refund_date - order_date)."""
        # order_date=Jan 1, return_date=Jan 20 (19 days), refund_date=Feb 5 (35 days)
        order = {
            "id": 2001,
            "created_at": "2024-01-01T00:00:00Z",
            "line_items": [
                {"id": 5001, "sku": "SKU-A", "quantity": 2,
                 "fulfillment_status": "fulfilled", "price": "49.99"},
            ],
            "refunds": [
                {
                    "id": 9001,
                    "created_at": "2024-02-05T00:00:00Z",  # 35 days from order
                    "return": {"id": 98765},
                    "refund_line_items": [
                        {"id": 7001, "line_item_id": 5001, "quantity": 1,
                         "subtotal": "49.99",
                         "line_item": {"sku": "SKU-A", "fulfillment_status": "fulfilled"}},
                    ],
                },
            ],
        }

        ref_count, refund_inserts = self._run_order(
            order,
            fetch_return_dates_retval={98765: date(2024, 1, 20)},
        )

        assert ref_count == 1
        assert len(refund_inserts) == 1
        params = refund_inserts[0]

        # days_to_refund = Jan 20 - Jan 1 = 19 (NOT 35 = Feb 5 - Jan 1)
        assert params[8] == 19, f"Expected 19 days (return_date), got {params[8]}"
        # return_date stored
        assert params[7] == date(2024, 1, 20)
        # return_id stored (not None)
        assert params[10] == 98765

    def test_scenario_b_goodwill_refund_uses_refund_date(self):
        """Scenario B: Goodwill refund (no return_id) — days = refund_date - order_date."""
        order = {
            "id": 2002,
            "created_at": "2024-01-01T00:00:00Z",
            "line_items": [
                {"id": 5002, "sku": "SKU-A", "quantity": 1,
                 "fulfillment_status": "fulfilled", "price": "29.99"},
            ],
            "refunds": [
                {
                    "id": 9002,
                    "created_at": "2024-01-25T00:00:00Z",  # 24 days from order
                    # no "return" key → goodwill
                    "refund_line_items": [
                        {"id": 7002, "line_item_id": 5002, "quantity": 1,
                         "subtotal": "14.99",
                         "line_item": {"sku": "SKU-A", "fulfillment_status": "fulfilled"}},
                    ],
                },
            ],
        }

        ref_count, refund_inserts = self._run_order(order, fetch_return_dates_retval={})

        assert ref_count == 1
        params = refund_inserts[0]

        # days_to_refund = Jan 25 - Jan 1 = 24
        assert params[8] == 24, f"Expected 24 days (refund_date), got {params[8]}"
        # return_date is None (no return object)
        assert params[7] is None
        # return_id is None (no return object)
        assert params[10] is None

    def test_scenario_c_physical_return_api_fails_fallback_to_refund_date(self):
        """Scenario C: return_id exists but API fails → days = refund_date - order_date."""
        order = {
            "id": 2003,
            "created_at": "2024-01-01T00:00:00Z",
            "line_items": [
                {"id": 5003, "sku": "SKU-A", "quantity": 1,
                 "fulfillment_status": "fulfilled", "price": "59.99"},
            ],
            "refunds": [
                {
                    "id": 9003,
                    "created_at": "2024-02-10T00:00:00Z",  # 40 days from order
                    "return": {"id": 77777},
                    "refund_line_items": [
                        {"id": 7003, "line_item_id": 5003, "quantity": 1,
                         "subtotal": "59.99",
                         "line_item": {"sku": "SKU-A", "fulfillment_status": "fulfilled"}},
                    ],
                },
            ],
        }

        # API failed: _fetch_return_dates returns {} (return_id not in lookup)
        ref_count, refund_inserts = self._run_order(order, fetch_return_dates_retval={})

        assert ref_count == 1
        params = refund_inserts[0]

        # Fallback: days = refund_date - order_date = 40
        assert params[8] == 40, f"Expected fallback 40 days, got {params[8]}"
        # return_date is None (lookup miss)
        assert params[7] is None
        # return_id is still stored
        assert params[10] == 77777

    def test_negative_days_clamped_to_zero(self):
        """Clock skew: if return_date < order_date, days_to_refund must be 0 (not negative)."""
        order = {
            "id": 2005,
            "created_at": "2024-01-15T00:00:00Z",  # order_date = Jan 15
            "line_items": [
                {"id": 5005, "sku": "SKU-A", "quantity": 1,
                 "fulfillment_status": "fulfilled", "price": "29.99"},
            ],
            "refunds": [
                {
                    "id": 9006,
                    "created_at": "2024-01-20T00:00:00Z",
                    "return": {"id": 11111},
                    "refund_line_items": [
                        {"id": 7006, "line_item_id": 5005, "quantity": 1,
                         "subtotal": "29.99",
                         "line_item": {"sku": "SKU-A", "fulfillment_status": "fulfilled"}},
                    ],
                },
            ],
        }

        # return_date = Jan 10 — five days BEFORE order_date Jan 15
        ref_count, refund_inserts = self._run_order(
            order,
            fetch_return_dates_retval={11111: date(2024, 1, 10)},
        )

        assert ref_count == 1
        params = refund_inserts[0]
        assert params[8] == 0, (
            f"Expected 0 (clamped from negative), got {params[8]}"
        )

    def test_scenario_d_order_with_physical_and_goodwill_refunds(self):
        """Scenario D: One order has two refunds — one physical, one goodwill."""
        order = {
            "id": 2004,
            "created_at": "2024-01-01T00:00:00Z",
            "line_items": [
                {"id": 5004, "sku": "SKU-A", "quantity": 3,
                 "fulfillment_status": "fulfilled", "price": "39.99"},
            ],
            "refunds": [
                {
                    # Physical return: return_id present
                    "id": 9004,
                    "created_at": "2024-02-01T00:00:00Z",  # 31 days from order
                    "return": {"id": 55555},
                    "refund_line_items": [
                        {"id": 7004, "line_item_id": 5004, "quantity": 1,
                         "subtotal": "39.99",
                         "line_item": {"sku": "SKU-A", "fulfillment_status": "fulfilled"}},
                    ],
                },
                {
                    # Goodwill: no return
                    "id": 9005,
                    "created_at": "2024-01-15T00:00:00Z",  # 14 days from order
                    "refund_line_items": [
                        {"id": 7005, "line_item_id": 5004, "quantity": 1,
                         "subtotal": "10.00",
                         "line_item": {"sku": "SKU-A", "fulfillment_status": "fulfilled"}},
                    ],
                },
            ],
        }

        # Physical return registered on Jan 10 (9 days from order)
        ref_count, refund_inserts = self._run_order(
            order,
            fetch_return_dates_retval={55555: date(2024, 1, 10)},
        )

        assert ref_count == 2
        assert len(refund_inserts) == 2

        # Find by return_id in params
        physical = next(p for p in refund_inserts if p[10] == 55555)
        goodwill = next(p for p in refund_inserts if p[10] is None)

        # Physical: days from return_date (Jan 10 - Jan 1 = 9)
        assert physical[8] == 9
        assert physical[7] == date(2024, 1, 10)

        # Goodwill: days from refund_date (Jan 15 - Jan 1 = 14)
        assert goodwill[8] == 14
        assert goodwill[7] is None


# ── unit_price and refund_subtotal capture ────────────────────────────────────

class TestPriceCapture:
    """Verify monetary fields are correctly extracted from Shopify payloads."""

    def _get_li_insert_params(self, order):
        """Return params from the first INSERT INTO order_line_items fetchone call."""
        li_params = []

        original_factory = _make_fetchone_factory({"SKU-A", "SKU-B"})

        def _capturing_fetchone(conn, sql, params=None):
            if sql.strip().startswith("INSERT INTO order_line_items"):
                li_params.append(params)
            return original_factory(conn, sql, params)

        with patch("sync.fetchone", side_effect=_capturing_fetchone), \
             patch("sync.execute"), \
             patch("sync._fetch_return_dates", return_value={}):
            sync._upsert_order(object(), order)

        return li_params

    def test_unit_price_captured_from_price_field(self):
        """No discount: unit_price = price (formula reduces to price × qty / qty)."""
        order = {
            "id": 3001,
            "created_at": "2024-03-01T00:00:00Z",
            "line_items": [
                {"id": 6001, "sku": "SKU-A", "quantity": 2,
                 "fulfillment_status": "fulfilled", "price": "49.99"},
            ],
            "refunds": [],
        }
        li_params = self._get_li_insert_params(order)
        assert len(li_params) == 1
        # (shopify_line_item_id, order_id, sku, quantity, order_date, unit_price)
        assert li_params[0][5] == 49.99

    def test_unit_price_accounts_for_discount(self):
        """15% discount: unit_price = (price × qty − total_discount) / qty."""
        order = {
            "id": 3006,
            "created_at": "2024-03-01T00:00:00Z",
            "line_items": [
                {"id": 6006, "sku": "SKU-A", "quantity": 2,
                 "fulfillment_status": "fulfilled",
                 "price": "120.00", "total_discount": "36.00"},
            ],
            "refunds": [],
        }
        li_params = self._get_li_insert_params(order)
        assert len(li_params) == 1
        # (120 × 2 − 36) / 2 = 102.00
        assert li_params[0][5] == 102.00, (
            f"unit_price should be post-discount effective price, got {li_params[0][5]}"
        )

    def test_unit_price_defaults_to_zero_when_price_missing(self):
        """Missing price field → float(None or 0) = 0.0."""
        order = {
            "id": 3002,
            "created_at": "2024-03-01T00:00:00Z",
            "line_items": [
                {"id": 6002, "sku": "SKU-A", "quantity": 1,
                 "fulfillment_status": "fulfilled"},  # no 'price' key
            ],
            "refunds": [],
        }
        li_params = self._get_li_insert_params(order)
        assert len(li_params) == 1
        assert li_params[0][5] == 0.0

    def test_unit_price_defaults_to_zero_when_price_is_none(self):
        """Explicit None price → 0.0."""
        order = {
            "id": 3003,
            "created_at": "2024-03-01T00:00:00Z",
            "line_items": [
                {"id": 6003, "sku": "SKU-A", "quantity": 1,
                 "fulfillment_status": "fulfilled", "price": None},
            ],
            "refunds": [],
        }
        li_params = self._get_li_insert_params(order)
        assert li_params[0][5] == 0.0

    def test_refund_subtotal_includes_total_tax(self):
        """params[11] = subtotal + total_tax (tax-inclusive, consistent with unit_price)."""
        order = {
            "id": 3004,
            "created_at": "2024-03-01T00:00:00Z",
            "line_items": [
                {"id": 6004, "sku": "SKU-A", "quantity": 2,
                 "fulfillment_status": "fulfilled", "price": "120.00"},
            ],
            "refunds": [
                {
                    "id": 9010,
                    "created_at": "2024-03-15T00:00:00Z",
                    "refund_line_items": [
                        {"id": 7010, "line_item_id": 6004, "quantity": 1,
                         "subtotal": "100.00", "total_tax": "20.00",
                         "line_item": {"sku": "SKU-A", "fulfillment_status": "fulfilled"}},
                    ],
                },
            ],
        }
        executed = []
        with patch("sync.fetchone", side_effect=_make_fetchone_factory({"SKU-A"})), \
             patch("sync.execute",
                   side_effect=lambda c, sql, p=None: executed.append((sql, p))), \
             patch("sync._fetch_return_dates", return_value={}):
            sync._upsert_order(object(), order)

        refund_inserts = [p for sql, p in executed if "refund_line_items" in sql]
        assert len(refund_inserts) == 1
        assert refund_inserts[0][11] == 120.00, (
            "refund_subtotal must be subtotal + total_tax to match tax-inclusive unit_price"
        )

    def test_refund_subtotal_defaults_to_zero_when_none(self):
        """subtotal=None and total_tax=None → 0.0."""
        order = {
            "id": 3005,
            "created_at": "2024-03-01T00:00:00Z",
            "line_items": [
                {"id": 6005, "sku": "SKU-A", "quantity": 1,
                 "fulfillment_status": "fulfilled", "price": "19.99"},
            ],
            "refunds": [
                {
                    "id": 9011,
                    "created_at": "2024-03-10T00:00:00Z",
                    "refund_line_items": [
                        {"id": 7011, "line_item_id": 6005, "quantity": 1,
                         "subtotal": None, "total_tax": None,
                         "line_item": {"sku": "SKU-A", "fulfillment_status": "fulfilled"}},
                    ],
                },
            ],
        }
        executed = []
        with patch("sync.fetchone", side_effect=_make_fetchone_factory({"SKU-A"})), \
             patch("sync.execute",
                   side_effect=lambda c, sql, p=None: executed.append((sql, p))), \
             patch("sync._fetch_return_dates", return_value={}):
            sync._upsert_order(object(), order)

        refund_inserts = [p for sql, p in executed if "refund_line_items" in sql]
        assert refund_inserts[0][11] == 0.0


# ── Aggregate SQL structural checks ──────────────────────────────────────────

class TestAggregateSqlMonetary:
    """Verify aggregate SQL handles monetary refund fields correctly."""

    def test_total_refunded_amount_uses_sum_of_refund_subtotal(self):
        """SQL computes total_refunded_amount as SUM(rli.refund_subtotal) — no window filter."""
        sql = _get_agg_sql()
        assert "SUM(rli.refund_subtotal)" in sql

    def test_total_refunded_amount_has_no_days_to_refund_filter(self):
        """Goodwill refunds have no window — SUM(rli.refund_subtotal) must not be
        wrapped in a CASE WHEN days_to_refund filter."""
        sql = _get_agg_sql()
        # Bare aggregate must be present
        assert re.search(r"SUM\s*\(\s*rli\.refund_subtotal\s*\)", sql) is not None
        # A windowed version would look like: THEN rli.refund_subtotal END
        # That pattern must not exist.
        assert not re.search(r"THEN\s+rli\.refund_subtotal", sql), (
            "refund_subtotal must not be inside a CASE WHEN ... THEN block"
        )

    def test_refund_rate_monetary_divides_by_nullif_total_revenue(self):
        """refund_rate_monetary denominator is NULLIF(o.total_revenue, 0) to prevent /0."""
        sql = _get_agg_sql()
        assert "NULLIF(o.total_revenue, 0)" in sql

    def test_total_refunded_amount_in_on_conflict_do_update(self):
        """ON CONFLICT branch must refresh total_refunded_amount."""
        sql = _get_agg_sql()
        assert "total_refunded_amount" in sql
        # Ensure it appears in the ON CONFLICT DO UPDATE SET block
        on_conflict_part = sql.split("ON CONFLICT")[1] if "ON CONFLICT" in sql else ""
        assert "total_refunded_amount" in on_conflict_part

    def test_refund_rate_monetary_in_on_conflict_do_update(self):
        """ON CONFLICT branch must refresh refund_rate_monetary."""
        sql = _get_agg_sql()
        on_conflict_part = sql.split("ON CONFLICT")[1] if "ON CONFLICT" in sql else ""
        assert "refund_rate_monetary" in on_conflict_part

    def test_refund_rate_monetary_numerator_is_total_refunded_amount(self):
        """refund_rate_monetary = COALESCE(r.total_refunded_amount, 0) / NULLIF(o.total_revenue, 0).
        Catches regressions that accidentally use e.g. returned_30d as the numerator."""
        sql = _get_agg_sql()
        normalized = re.sub(r"\s+", " ", sql)
        assert re.search(
            r"COALESCE\(r\.total_refunded_amount,\s*0\)\s*/\s*NULLIF\(o\.total_revenue,\s*0\)",
            normalized,
        ), "refund_rate_monetary must be COALESCE(r.total_refunded_amount,0) / NULLIF(o.total_revenue,0)"

    def test_sql_nullif_total_ordered_count(self):
        """SQL uses NULLIF(o.total_ordered, 0) for rate calculations (at least 2 times)."""
        sql = _get_agg_sql()
        # 30d, 100d, physical_30d, physical_100d all divide by total_ordered
        assert sql.count("NULLIF(o.total_ordered, 0)") >= 2
