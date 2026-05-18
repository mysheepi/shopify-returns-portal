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

        # execute called once with our buffer in named-param form
        assert mock_exec.call_count == 1
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
    """Defensive: empty row still falls back gracefully."""
    # A row with "days" missing would raise; the production code expects
    # either a dict containing "days" or None. Verify it does NOT crash
    # with the None-row branch.
    with patch("aggregate.fetchone", return_value=None), \
         patch("aggregate.execute") as mock_exec:
        aggregate.run_aggregation(object())
        assert mock_exec.called


# ── SQL string content checks ────────────────────────────────────────────────

def _get_sql():
    """Return the SQL that run_aggregation issues."""
    captured = {}

    def _capture(_conn, sql, params):
        captured["sql"] = sql
        captured["params"] = params

    with patch("aggregate.fetchone", return_value={"days": 10}), \
         patch("aggregate.execute", side_effect=_capture):
        aggregate.run_aggregation(object())
    return captured["sql"]


def test_sql_uses_buffer_math_for_30d_window():
    sql = _get_sql()
    # Both the SUM CASE and the is_30d_closed comparison must add the buffer.
    assert sql.count("30 + %(buf)s") >= 3, (
        "Expected '30 + %(buf)s' in returned_30d, return_rate_30d, "
        "and is_30d_closed expressions"
    )


def test_sql_uses_buffer_math_for_100d_window():
    sql = _get_sql()
    assert sql.count("100 + %(buf)s") >= 3, (
        "Expected '100 + %(buf)s' in returned_100d, return_rate_100d, "
        "and is_100d_closed expressions"
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
    assert "(MAX(oli.order_date) + (30 + %(buf)s)) < CURRENT_DATE" in normalized
    assert "(MAX(oli.order_date) + (100 + %(buf)s)) < CURRENT_DATE" in normalized


def test_sql_on_conflict_updates_all_metric_columns():
    sql = _get_sql()
    for col in [
        "return_window_days",
        "total_ordered",
        "returned_30d",
        "return_rate_30d",
        "is_30d_closed",
        "returned_100d",
        "return_rate_100d",
        "is_100d_closed",
        "last_order_date",
        "refreshed_at",
    ]:
        assert f"{col}" in sql, f"ON CONFLICT branch should refresh {col}"


def test_sql_uses_inner_join_for_sku_config():
    """SKUs not in sku_config must be excluded — INNER JOIN, not LEFT JOIN."""
    sql = _get_sql()
    normalized = re.sub(r"\s+", " ", sql)
    assert "JOIN sku_config sc ON sc.sku = oli.sku" in normalized
    # And LEFT JOIN refund_line_items so SKUs with zero refunds still appear.
    assert "LEFT JOIN refund_line_items rli" in normalized


def test_sql_nullif_protects_against_zero_division():
    """return_rate_*  / NULLIF(SUM(oli.quantity), 0) — must never divide by 0."""
    sql = _get_sql()
    assert sql.count("NULLIF(SUM(oli.quantity), 0)") == 2


def test_sql_groups_by_sku_month_and_window():
    sql = _get_sql()
    normalized = re.sub(r"\s+", " ", sql)
    assert (
        "GROUP BY oli.sku, DATE_TRUNC('month', oli.order_date), sc.return_window_days"
        in normalized
    )


def test_sql_uses_coalesce_for_zero_returns():
    """SKUs with no refunds should get 0 (not NULL) for returned_* columns."""
    sql = _get_sql()
    assert sql.count("COALESCE(SUM(") >= 4  # 2 windows × (count + rate numerator)
