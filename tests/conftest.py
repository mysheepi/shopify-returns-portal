"""
Shared pytest fixtures for the Shopify returns portal test suite.

Strategy
--------
- Postgres is not available in the test environment, so we mock psycopg2
  at the database.py layer (get_db / query / execute / fetchone) and only
  exercise the *Python* logic that surrounds those calls.
- For SQL-correctness checks (migration schema, aggregate query) we run
  them against an in-process SQLite database where possible. The aggregate
  query uses Postgres-only features (DATE_TRUNC, RealDict, named-params)
  so it is not unit-testable against SQLite — instead we assert structural
  properties of the SQL string (buffer math, current-month exclusion,
  on-conflict update list).
- Env vars are set before importing application modules so module-level
  globals (PORTAL_PASSWORD, SHOPIFY_STORE, etc.) pick up test values.
"""

import os
import sys
import pathlib

# Make project root importable as if running from the project dir.
ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Set required env vars BEFORE any application module is imported.
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("PORTAL_PASSWORD", "testpass")
os.environ.setdefault("SHOPIFY_STORE", "teststore")
os.environ.setdefault("SHOPIFY_TOKEN", "shpat_test_token")
os.environ.setdefault("SHOPIFY_API_VERSION", "2024-07")

import pytest


@pytest.fixture
def fake_conn():
    """A simple sentinel object that can stand in for a psycopg2 connection.

    We never call methods on it directly — database.query/execute/fetchone
    are themselves mocked in tests that need them.
    """
    class _FakeConn:
        committed = False
        rolled_back = False

        def commit(self):
            self.committed = True

        def rollback(self):
            self.rolled_back = True

    return _FakeConn()
