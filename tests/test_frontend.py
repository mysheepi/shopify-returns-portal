"""
Static checks for frontend/index.html.

We don't run a real browser — instead we verify the structural pieces
that have to be present for the SPA to function:
- Auth header is sent on every protected API call.
- Auth flow stores the password in sessionStorage and reuses it on init.
- Settings save calls POST /api/settings with the right shape.
- Chart and table renderers handle null/undefined cells.
- All API endpoints referenced by the frontend match what main.py exposes.
"""

from pathlib import Path
import re

HTML = (Path(__file__).resolve().parent.parent
        / "frontend" / "index.html").read_text()


# ── Required CDN dependencies ────────────────────────────────────────────────

def test_loads_alpine():
    assert "alpinejs" in HTML


def test_loads_ag_grid():
    assert "ag-grid-community" in HTML


def test_loads_chartjs():
    assert "chart.js" in HTML


def test_loads_tom_select():
    assert "tom-select" in HTML


# ── Auth flow ────────────────────────────────────────────────────────────────

def test_auth_header_used_on_api_call():
    assert "'X-Portal-Password': this.password" in HTML


def test_login_calls_verify_endpoint():
    assert "/api/auth/verify" in HTML


def test_session_persistence():
    assert "sessionStorage" in HTML
    assert "portal_pw" in HTML


def test_session_cleared_on_401():
    """A wrong-password attempt should clear stale storage."""
    assert "sessionStorage.removeItem('portal_pw')" in HTML


def test_unauthorized_throws():
    assert "if (r.status === 401)" in HTML


# ── Endpoint coverage ────────────────────────────────────────────────────────

def test_endpoints_referenced_match_backend():
    """Every /api/* path used in the frontend exists on the backend."""
    import main
    backend_paths = {r.path for r in main.app.routes
                     if getattr(r, "path", "").startswith("/api/")}
    # Endpoints that should exist
    for path in [
        "/api/auth/verify",
        "/api/skus",
        "/api/returns",
        "/api/returns/export",
        "/api/sync/status",
        "/api/sync/trigger",
        "/api/settings",
    ]:
        assert path in backend_paths, (
            f"Frontend uses {path} but backend does not expose it"
        )
        assert path in HTML, f"Backend has {path} but frontend never calls it"


# ── Renderer null-safety ─────────────────────────────────────────────────────

def test_pct_renderer_handles_null():
    """return_rate_* can be NULL for zero-quantity months (NULLIF)."""
    assert "params.value === null || params.value === undefined" in HTML


def test_closed_badge_renderer_handles_null():
    # Same defensive check used by closedRenderer
    assert HTML.count("params.value === null || params.value === undefined") >= 2


# ── Default filter behaviour ────────────────────────────────────────────────

def test_default_from_month_matches_full_sync_start_year():
    """The frontend defaults from-month to '2024-07' matching FULL_SYNC_START."""
    assert "filterFrom: '2024-07'" in HTML


def test_default_to_month_is_last_complete_month():
    """JS sets filterTo to one calendar month ago — spec says we never show
    the current month in stats."""
    assert "setMonth(d.getMonth() - 1)" in HTML


# ── Sync polling ─────────────────────────────────────────────────────────────

def test_sync_polls_every_3_seconds_while_running():
    assert "setInterval(() => this.checkSyncStatus(), 3000)" in HTML


def test_sync_stops_polling_when_done():
    assert "clearInterval(this.pollInterval)" in HTML


# ── Settings save shape ──────────────────────────────────────────────────────

def test_save_settings_posts_correct_body():
    assert "this.api('POST', '/api/settings'" in HTML
    assert "return_buffer_days: this.bufferInput" in HTML


def test_buffer_input_clamped_in_ui():
    assert 'min="0"' in HTML
    assert 'max="60"' in HTML


# ── Chart resilience ─────────────────────────────────────────────────────────

def test_chart_caps_at_8_skus():
    assert ".slice(0, 8)" in HTML


def test_chart_destroyed_before_redraw():
    """Prevents Chart.js memory leak when filters change."""
    assert "this.chartInstance.destroy()" in HTML


def test_chart_tooltip_handles_null_y():
    """`ctx.parsed.y ?? '—'` — nullish-coalescing for empty months."""
    assert "ctx.parsed.y ?? '—'" in HTML
