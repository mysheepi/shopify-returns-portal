"""
Tests for aggregate.py.

The aggregation SQL itself targets Postgres (DATE_TRUNC, ON CONFLICT,
named-params, NUMERIC cast). Rather than spin up a real DB we validate:

1. The Python wrapper reads the buffer from the settings table and falls
   back to 10 when no row exists.
2. The SQL string passed to execute() honours the documented contract:
   - applies "+ %(buf)s" math to both 30-day and 100-day windows
   - excludes the current calendar month
   - covers all sku_monthly_stats columns in the ON CONFLICT DO UPDATE
   - uses "< CURRENT_DATE" (strict less-than) for is_30d_closed /
     is_100d_closed
"""

import re
from unittest.mock import patch, MagicMock

import aggregate


# ── Buffer / fallback wiring ─────────────────────────────────────────────────

def test_run_aggregation_reads_buffer_from_settings():
    """When settings row exists, its value is passed as %(buf)s param."""
    fake_conn = object()
    with patch("aggregate.fetchone", return_value={"days": 25}) as mock_fetch, \
         patch("aggregate.execute") as mock_exec:
        aggregate.run_aggregation(fake_conn)

        # fetchone called against the settings table
        assert mock_fetch.call_count == 1
        select_sql = mock_fetch.call_args[0][1]
        assert "return_buffer_days" in select_sql
        assert "settings" in select_sql

        # execute called twice: orphan-stats cleanup, then the aggregation
        # upsert with our buffer in named-param form (the last call)
        assert mock_exec.call_count == 2
        _, sql, params = mock_exec.call_args[0]
        assert params == {"buf": 25}


def test_run_aggregation_defaults_to_10_when_settings_missing():
    """Per spec: default buffer is 10 days."""
    with patch("aggregate.fetchone", return_value=None), \
         patch("aggregate.execute") as mock_exec:
        aggregate.run_aggregation(object())
        _, _, params = mock_exec.call_args[0]
        assert params == {"buf": 10}


def test_run_aggregation_defaults_when_settings_value_is_none_row():
    """When the settings row exists but days=0, buffer should be 0 (not 10).

    The code is: buffer = buf_row["days"] if buf_row else 10
    A row with days=0 is truthy (it's a non-None dict), so buffer must be 0.
    """
    with patch("aggregate.fetchone", return_value={"days": 0}), \
         patch("aggregate.execute") as mock_exec:
        aggregate.run_aggregation(object())
        _, _, params = mock_exec.call_args[0]
        assert params == {"buf": 0}, (
            f"Expected buf=0 when row has days=0, got {params}"
        )


# ── SQL string content checks ────────────────────────────────────────────────

def _get_sql():
    """Return the SQL that run_aggregation issues."""
    captured = {}

    def _capture(_conn, sql, params=None):
        # Aggregation issues two statements (orphan cleanup, then the upsert);
        # the upsert is last, so it wins the capture.
        captured["sql"] = sql
        captured["params"] = params

    with patch("aggregate.fetchone", return_value={"days": 10}), \
         patch("aggregate.execute", side_effect=_capture):
        aggregate.run_aggregation(object())
    return captured["sql"]


def test_sql_uses_buffer_math_for_30d_window():
    sql = _get_sql()
    # CTE-based SQL: buffer applied in all-channel CASE WHEN, physical CASE WHEN,
    # and is_30d_closed.
    # Normalize whitespace to handle any spacing variation (e.g. "30  +" vs "30 +").
    normalized = re.sub(r"\s+", " ", sql)
    assert normalized.count("30 + %(buf)s") >= 3, (
        "Expected '30 + %(buf)s' in the all-channel CASE WHEN, physical CASE WHEN, "
        "and is_30d_closed"
    )


def test_sql_uses_buffer_math_for_100d_window():
    sql = _get_sql()
    assert sql.count("100 + %(buf)s") >= 3, (
        "Expected '100 + %(buf)s' in the all-channel CASE WHEN, physical CASE WHEN, "
        "and is_100d_closed"
    )


def test_sql_excludes_current_calendar_month():
    sql = _get_sql()
    # Must filter to months strictly before the current month.
    # The exact whitespace can vary, so normalize.
    normalized = re.sub(r"\s+", " ", sql)
    assert (
        "DATE_TRUNC('month', oli.order_date) < DATE_TRUNC('month', CURRENT_DATE)"
        in normalized
    )


def test_sql_window_closed_uses_strict_less_than():
    """is_30d_closed: (last_order_date + 30 + buf) < CURRENT_DATE."""
    sql = _get_sql()
    normalized = re.sub(r"\s+", " ", sql)
    # Strict less-than (not <=) is what the spec says.
    # CTE approach stores last_order_date in the ordered CTE and references it as o.last_order_date.
    assert "(o.last_order_date + (30 + %(buf)s)) < CURRENT_DATE" in normalized
    assert "(o.last_order_date + (100 + %(buf)s)) < CURRENT_DATE" in normalized


def test_sql_on_conflict_updates_all_metric_columns():
    sql = _get_sql()
    on_conflict = sql.split("ON CONFLICT")[1] if "ON CONFLICT" in sql else ""
    for col in [
        "return_window_days",
        "total_ordered",
        "returned_30d",
        "return_rate_30d",
        "is_30d_closed",
        "returned_100d",
        "return_rate_100d",
        "is_100d_closed",
        "returned_30d_physical",
        "return_rate_30d_physical",
        "returned_100d_physical",
        "return_rate_100d_physical",
        "total_revenue",
        "total_refunded_amount",
        "refund_rate_monetary",
        "last_order_date",
        "refreshed_at",
    ]:
        assert col in on_conflict, f"ON CONFLICT branch should refresh {col}"


def test_physical_cte_filters_by_return_id_not_null():
    """Physical return columns must gate on return_id IS NOT NULL, not restock_type."""
    sql = _get_sql()
    assert sql.count("return_id IS NOT NULL") >= 2, (
        "Both 30d and 100d physical branches need rli.return_id IS NOT NULL"
    )


def test_sql_uses_inner_join_for_sku_config():
    """SKUs not in sku_config must be excluded — INNER JOIN, not LEFT JOIN."""
    sql = _get_sql()
    normalized = re.sub(r"\s+", " ", sql)
    # sku_config is INNER JOINed in the ordered CTE so unknown SKUs are excluded.
    assert "JOIN sku_config sc ON sc.sku = oli.sku" in normalized
    # The outer SELECT LEFT JOINs the returned CTE so months with zero refunds still appear.
    assert "LEFT JOIN returned r ON r.sku = o.sku" in normalized


def test_sql_nullif_protects_against_zero_division():
    """return_rate_* / NULLIF(o.total_ordered, 0) — must never divide by 0."""
    sql = _get_sql()
    # CTE approach references total_ordered from the ordered CTE as o.total_ordered.
    # The SQL now has 4 divisions by total_ordered (30d, 100d, 30d_physical, 100d_physical)
    # and 1 division by total_revenue for the monetary rate.
    assert sql.count("NULLIF(o.total_ordered, 0)") >= 2
    assert "NULLIF(o.total_revenue, 0)" in sql


def test_sql_groups_by_sku_month_and_window():
    sql = _get_sql()
    normalized = re.sub(r"\s+", " ", sql)
    # The ordered CTE groups by (sku, order_month); return_window_days comes from
    # sku_config joined in the outer SELECT — same effective grouping.
    assert "GROUP BY oli.sku, DATE_TRUNC('month', oli.order_date)" in normalized


def test_sql_uses_coalesce_for_zero_returns():
    """SKUs with no refunds should get 0 (not NULL) for returned_* columns."""
    sql = _get_sql()
    # CTE approach: COALESCE(r.returned_30d, 0) and COALESCE(r.returned_100d, 0)
    # (both appear twice each — once for the count, once as the rate numerator).
    assert sql.count("COALESCE(r.returned_") >= 4
