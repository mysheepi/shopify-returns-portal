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

    cur.execute.assert_called_once()
    executed_sql = cur.execute.call_args.args[0]
    assert "CREATE TABLE IF NOT EXISTS orders" in executed_sql
    assert "CREATE TABLE IF NOT EXISTS sku_monthly_stats" in executed_sql
