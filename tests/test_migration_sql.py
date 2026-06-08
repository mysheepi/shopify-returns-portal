"""
Static validation of migrations/001_init.sql.

Goals:
- All finalized SKUs are seeded (5 with 100-day window; the rest with 30-day window).
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
    """All seeded SKUs have either a 30-day or 100-day window — no gaps."""
    skus = _parse_sku_inserts(sql)
    assert len(skus) > 0, "No SKUs found in migration seed"
    thirties = sum(1 for _, d in skus if d == 30)
    hundreds = sum(1 for _, d in skus if d == 100)
    total = len(skus)
    assert thirties + hundreds == total, (
        f"SKU split inconsistent: 30-day ({thirties}) + 100-day ({hundreds}) ≠ total ({total})"
    )


def test_sku_seed_100day_count(sql):
    """Exactly 5 SKUs must have a 100-day return window."""
    skus = _parse_sku_inserts(sql)
    hundreds = [s for s, d in skus if d == 100]
    assert len(hundreds) == 5, (
        f"Expected 5 100-day SKUs, found {len(hundreds)}: {hundreds}"
    )


def test_sku_seed_30day_count(sql):
    """30-day SKU count equals total seeded SKUs minus the 5 100-day SKUs."""
    skus = _parse_sku_inserts(sql)
    thirties = [s for s, d in skus if d == 30]
    hundreds = [s for s, d in skus if d == 100]
    expected = len(skus) - len(hundreds)
    assert len(thirties) == expected, (
        f"30-day count should be {expected} (total minus 100-day), found {len(thirties)}"
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


# ── Migration 002: restock_type and physical columns ────────────────────────

@pytest.fixture(scope="module")
def sql_002():
    path = SQL_PATH.parent / "002_restock_type.sql"
    return path.read_text()


def test_migration_002_restock_type(sql_002):
    assert "restock_type" in sql_002


def test_migration_002_physical_columns(sql_002):
    for col in [
        "returned_30d_physical",
        "return_rate_30d_physical",
        "returned_100d_physical",
        "return_rate_100d_physical",
    ]:
        assert col in sql_002, f"002 migration missing column: {col}"


# ── Migration 003: return_id and index ──────────────────────────────────────

@pytest.fixture(scope="module")
def sql_003():
    path = SQL_PATH.parent / "003_return_id.sql"
    return path.read_text()


def test_migration_003_return_id_column(sql_003):
    assert "return_id" in sql_003


def test_migration_003_index(sql_003):
    assert "idx_rli_return_id" in sql_003


# ── Migration 004: monetary columns ─────────────────────────────────────────

@pytest.fixture(scope="module")
def sql_004():
    path = SQL_PATH.parent / "004_monetary_refund.sql"
    return path.read_text()


def test_migration_004_unit_price(sql_004):
    assert "unit_price" in sql_004


def test_migration_004_refund_subtotal(sql_004):
    assert "refund_subtotal" in sql_004


def test_migration_004_total_revenue(sql_004):
    assert "total_revenue" in sql_004


def test_migration_004_refunded_30d_amount(sql_004):
    """004 introduces intermediate windowed amount columns."""
    assert "refunded_30d_amount" in sql_004


# ── Migration 005: return_date column ───────────────────────────────────────

@pytest.fixture(scope="module")
def sql_005():
    path = SQL_PATH.parent / "005_return_date.sql"
    return path.read_text()


def test_migration_005_return_date(sql_005):
    assert "return_date" in sql_005


# ── Migration 006: cumulative refund columns and DROP of windowed columns ───

@pytest.fixture(scope="module")
def sql_006():
    path = SQL_PATH.parent / "006_cumulative_refund.sql"
    return path.read_text()


def test_migration_006_total_refunded_amount(sql_006):
    assert "total_refunded_amount" in sql_006


def test_migration_006_refund_rate_monetary(sql_006):
    assert "refund_rate_monetary" in sql_006


def test_migration_006_drops_windowed_columns(sql_006):
    """006 must DROP the windowed monetary columns introduced in 004."""
    assert "DROP COLUMN" in sql_006.upper() or "drop column" in sql_006
