"""
Unit tests for database.py helpers — connection pool, get_db transaction
semantics, and query/execute/fetchone wrappers.

psycopg2 is mocked out entirely.
"""

import os
from unittest.mock import patch, MagicMock

import pytest


# Make sure module-level _pool is reset between tests.
@pytest.fixture(autouse=True)
def reset_pool():
    import database
    database._pool = None
    yield
    database._pool = None


def test_get_pool_initialises_threaded_pool():
    import database
    with patch("database.psycopg2.pool.ThreadedConnectionPool") as mock_pool:
        database._get_pool()
        mock_pool.assert_called_once()
        kwargs = mock_pool.call_args.kwargs
        assert kwargs["minconn"] == 1
        assert kwargs["maxconn"] == 10
        assert kwargs["dsn"] == os.environ["DATABASE_URL"]


def test_get_pool_singleton():
    import database
    with patch("database.psycopg2.pool.ThreadedConnectionPool") as mock_pool:
        p1 = database._get_pool()
        p2 = database._get_pool()
        assert p1 is p2
        assert mock_pool.call_count == 1


def test_get_db_commits_on_success():
    import database
    fake_conn = MagicMock()
    fake_pool = MagicMock()
    fake_pool.getconn.return_value = fake_conn
    with patch("database._get_pool", return_value=fake_pool):
        with database.get_db() as conn:
            assert conn is fake_conn
    fake_conn.commit.assert_called_once()
    fake_conn.rollback.assert_not_called()
    fake_pool.putconn.assert_called_once_with(fake_conn)


def test_get_db_rolls_back_on_exception():
    import database
    fake_conn = MagicMock()
    fake_pool = MagicMock()
    fake_pool.getconn.return_value = fake_conn
    with patch("database._get_pool", return_value=fake_pool):
        with pytest.raises(RuntimeError):
            with database.get_db():
                raise RuntimeError("boom")
    fake_conn.rollback.assert_called_once()
    fake_conn.commit.assert_not_called()
    fake_pool.putconn.assert_called_once_with(fake_conn)  # still returned


def test_get_db_always_returns_conn_to_pool():
    """Even on commit failure, conn must go back to the pool."""
    import database
    fake_conn = MagicMock()
    fake_conn.commit.side_effect = RuntimeError("commit blew up")
    fake_pool = MagicMock()
    fake_pool.getconn.return_value = fake_conn
    with patch("database._get_pool", return_value=fake_pool):
        with pytest.raises(RuntimeError):
            with database.get_db():
                pass
    fake_pool.putconn.assert_called_once_with(fake_conn)


def test_query_uses_realdict_cursor():
    import database
    fake_conn = MagicMock()
    cur = MagicMock()
    fake_conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchall.return_value = [{"a": 1}]

    result = database.query(fake_conn, "SELECT 1", (1,))
    assert result == [{"a": 1}]
    fake_conn.cursor.assert_called_once()
    # The kwargs include cursor_factory=RealDictCursor
    kwargs = fake_conn.cursor.call_args.kwargs
    assert kwargs.get("cursor_factory").__name__ == "RealDictCursor"
    cur.execute.assert_called_once_with("SELECT 1", (1,))


def test_execute_passes_params():
    import database
    fake_conn = MagicMock()
    cur = MagicMock()
    fake_conn.cursor.return_value.__enter__.return_value = cur

    database.execute(fake_conn, "UPDATE x SET y=%s", (5,))
    cur.execute.assert_called_once_with("UPDATE x SET y=%s", (5,))


def test_fetchone_returns_dict():
    import database
    fake_conn = MagicMock()
    cur = MagicMock()
    fake_conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchone.return_value = {"id": 42}

    assert database.fetchone(fake_conn, "SELECT") == {"id": 42}


def test_init_db_reads_migration_and_executes():
    import database
    fake_conn = MagicMock()
    cur = MagicMock()
    fake_conn.cursor.return_value.__enter__.return_value = cur
    fake_pool = MagicMock()
    fake_pool.getconn.return_value = fake_conn

    with patch("database._get_pool", return_value=fake_pool):
        database.init_db()

    # init_db runs one execute per migration file; with 6 files, execute is called 6 times.
    assert cur.execute.call_count >= 1
    # The initial schema migration (001_init.sql) must contain these tables.
    all_sql = " ".join(call.args[0] for call in cur.execute.call_args_list)
    assert "CREATE TABLE IF NOT EXISTS orders" in all_sql
    assert "CREATE TABLE IF NOT EXISTS sku_monthly_stats" in all_sql


def test_init_db_runs_migrations_in_lexical_order():
    """Migrations must execute in sorted filename order (001 before 002, etc.)."""
    import database
    fake_conn = MagicMock()
    cur = MagicMock()
    fake_conn.cursor.return_value.__enter__.return_value = cur
    fake_pool = MagicMock()
    fake_pool.getconn.return_value = fake_conn

    with patch("database._get_pool", return_value=fake_pool):
        database.init_db()

    sqls = [call.args[0] for call in cur.execute.call_args_list]
    assert len(sqls) >= 2, f"Expected at least 2 migration executions, got {len(sqls)}"

    # 001_init.sql: creates base tables — must be first
    idx_001 = next(
        (i for i, s in enumerate(sqls) if "CREATE TABLE IF NOT EXISTS orders" in s),
        None,
    )
    # 002_restock_type.sql: adds restock_type column
    idx_002 = next(
        (i for i, s in enumerate(sqls) if "restock_type" in s),
        None,
    )
    assert idx_001 is not None, "Migration 001 content not found in execute calls"
    assert idx_002 is not None, "Migration 002 content (restock_type) not found"
    assert idx_001 < idx_002, (
        f"Migration 001 (idx={idx_001}) must execute before 002 (idx={idx_002})"
    )
