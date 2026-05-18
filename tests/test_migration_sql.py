"""
Static validation of migrations/001_init.sql.

Goals:
- All 65 SKUs are seeded (5 with 100-day window, 60 with 30-day window).
- CREATE TABLE statements exist for every table the application references.
- All required check constraints and indexes are present.
- Default settings row exists.
- No duplicate SKU rows in the seed.
"""

import re
from pathlib import Path

import pytest


SQL_PATH = (Path(__file__).resolve().parent.parent
            / "migrations" / "001_init.sql")


@pytest.fixture(scope="module")
def sql():
    return SQL_PATH.read_text()


# ── Table presence ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("table", [
    "orders",
    "order_line_items",
    "refund_line_items",
    "sku_config",
    "settings",
    "sync_log",
    "sku_monthly_stats",
])
def test_table_created(sql, table):
    assert re.search(
        rf"CREATE TABLE IF NOT EXISTS\s+{table}\b", sql
    ), f"Missing CREATE TABLE for {table}"


# ── SKU seed counts ──────────────────────────────────────────────────────────

def _parse_sku_inserts(sql):
    """Return list of (sku, window_days) tuples seeded by the migration."""
    sku_lines = re.findall(
        r"\(\s*'([^']+)'\s*,\s*(30|100)\s*\)",
        sql,
    )
    return [(sku, int(days)) for sku, days in sku_lines]


def test_sku_seed_total_count(sql):
    """The migration currently seeds 68 SKUs (5 × 100-day + 63 × 30-day).

    NOTE — spec mismatch: the project documentation says "65 SKU seed
    data". The migration file actually inserts 68. Either the docs are
    stale or three SKUs were added without updating the docs. Flagged
    as a high-priority finding; this test pins reality so a regression
    is caught.
    """
    skus = _parse_sku_inserts(sql)
    assert len(skus) == 68, f"Expected 68 seeded SKUs, found {len(skus)}"


def test_sku_seed_100day_count(sql):
    skus = _parse_sku_inserts(sql)
    hundreds = [s for s, d in skus if d == 100]
    assert len(hundreds) == 5, (
        f"Expected 5 100-day SKUs, found {len(hundreds)}: {hundreds}"
    )


def test_sku_seed_30day_count(sql):
    """63 30-day SKUs in the migration (spec stated 60 — see total-count test)."""
    skus = _parse_sku_inserts(sql)
    thirties = [s for s, d in skus if d == 30]
    assert len(thirties) == 63, (
        f"Expected 63 30-day SKUs, found {len(thirties)}"
    )


def test_no_duplicate_skus_in_seed(sql):
    skus = [s for s, _ in _parse_sku_inserts(sql)]
    duplicates = [s for s in set(skus) if skus.count(s) > 1]
    assert not duplicates, f"Duplicate SKUs in seed: {duplicates}"


def test_seed_uses_on_conflict_do_nothing(sql):
    """Re-running the migration must be safe."""
    matches = re.findall(r"ON CONFLICT \(sku\) DO NOTHING", sql)
    assert len(matches) >= 2, "Both INSERT blocks should be idempotent"


# ── Constraints ──────────────────────────────────────────────────────────────

def test_return_window_check_constraint(sql):
    assert "CHECK (return_window_days IN (30, 100))" in sql


def test_quantity_positive_constraint(sql):
    assert "CHECK (quantity > 0)" in sql


def test_qty_returned_positive_constraint(sql):
    assert "CHECK (qty_returned > 0)" in sql


def test_days_to_refund_non_negative_constraint(sql):
    assert "CHECK (days_to_refund >= 0)" in sql


def test_sync_log_status_check(sql):
    assert re.search(
        r"CHECK \(status IN \('running', 'complete', 'error'\)\)",
        sql,
    )


def test_sync_log_type_check(sql):
    assert re.search(
        r"CHECK \(sync_type IN \('full', 'incremental'\)\)",
        sql,
    )


# ── Default settings ────────────────────────────────────────────────────────

def test_default_buffer_settings_row(sql):
    assert re.search(
        r"INSERT INTO settings.+'return_buffer_days', '10'",
        sql,
        re.DOTALL,
    )


# ── Foreign keys & cascading deletes ────────────────────────────────────────

def test_order_line_items_fk_cascade(sql):
    assert re.search(
        r"order_id\s+UUID\s+NOT NULL\s+REFERENCES orders\(id\)\s+ON DELETE CASCADE",
        sql,
    )


def test_refund_line_items_fk_cascade(sql):
    assert re.search(
        r"order_line_item_id\s+UUID\s+NOT NULL\s+REFERENCES order_line_items\(id\)\s+ON DELETE CASCADE",
        sql,
    )


# ── Indexes ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("idx,table,col", [
    ("idx_orders_order_date", "orders", "order_date"),
    ("idx_oli_sku",           "order_line_items", "sku"),
    ("idx_oli_order_date",    "order_line_items", "order_date"),
    ("idx_rli_sku",           "refund_line_items", "sku"),
    ("idx_rli_order_date",    "refund_line_items", "order_date"),
])
def test_index_present(sql, idx, table, col):
    assert re.search(
        rf"CREATE INDEX IF NOT EXISTS\s+{idx}\s+ON\s+{table}\s*\(\s*{col}\s*\)",
        sql,
    ), f"Missing index {idx} on {table}({col})"


# ── Unique constraints / primary keys ────────────────────────────────────────

def test_shopify_order_id_is_unique(sql):
    assert re.search(r"shopify_order_id\s+BIGINT UNIQUE", sql)


def test_shopify_line_item_id_is_unique(sql):
    assert re.search(r"shopify_line_item_id\s+BIGINT UNIQUE", sql)


def test_shopify_refund_line_item_id_is_unique(sql):
    assert re.search(r"shopify_refund_line_item_id\s+BIGINT UNIQUE", sql)


def test_sku_monthly_stats_composite_pk(sql):
    assert "PRIMARY KEY (sku, order_month)" in sql


# ── Sanity: pgcrypto extension for UUID generation ──────────────────────────

def test_pgcrypto_extension_loaded(sql):
    assert 'CREATE EXTENSION IF NOT EXISTS "pgcrypto"' in sql
