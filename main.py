import os
import json
import logging
import secrets
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import io
import csv

from database import init_db, get_db, query, execute, fetchone
from sync import trigger_sync, trigger_reaggregate, get_status

PORTAL_PASSWORD = os.environ.get("PORTAL_PASSWORD", "")
if not PORTAL_PASSWORD:
    logging.warning(
        "PORTAL_PASSWORD is not set — the portal is unprotected. "
        "Set this environment variable before exposing to your team."
    )


# ── App lifecycle ────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(title="Mysheepi Returns Portal", lifespan=lifespan)


# ── Auth dependency ──────────────────────────────────────────────────────────

def require_auth(x_portal_password: Optional[str] = Header(default=None)):
    if not x_portal_password or not secrets.compare_digest(
        x_portal_password, PORTAL_PASSWORD
    ):
        raise HTTPException(status_code=401, detail="Unauthorized")


# ── Static files & frontend ──────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/")
def serve_frontend():
    return FileResponse(os.path.join(os.path.dirname(__file__), "frontend", "index.html"))


# ── Auth ─────────────────────────────────────────────────────────────────────

@app.post("/api/auth/verify")
def auth_verify(x_portal_password: Optional[str] = Header(default=None)):
    if not x_portal_password or not secrets.compare_digest(
        x_portal_password, PORTAL_PASSWORD
    ):
        raise HTTPException(status_code=401, detail="Wrong password")
    return {"ok": True}


# ── SKUs ─────────────────────────────────────────────────────────────────────

@app.get("/api/skus")
def get_skus(x_portal_password: Optional[str] = Header(default=None)):
    require_auth(x_portal_password)
    with get_db() as conn:
        rows = query(conn, "SELECT sku, return_window_days FROM sku_config ORDER BY sku")
    return [dict(r) for r in rows]


# ── Returns data ─────────────────────────────────────────────────────────────

@app.get("/api/returns")
def get_returns(
    skus:  Optional[str] = Query(default=None, description="Comma-separated SKU list"),
    from_month: Optional[str] = Query(default=None, alias="from", description="YYYY-MM"),
    to_month:   Optional[str] = Query(default=None, alias="to",   description="YYYY-MM"),
    x_portal_password: Optional[str] = Header(default=None),
):
    require_auth(x_portal_password)

    conditions = []
    params: list = []

    if skus:
        sku_list = [s.strip() for s in skus.split(",") if s.strip()]
        conditions.append(f"sku = ANY(%s)")
        params.append(sku_list)

    if from_month:
        conditions.append("order_month >= DATE_TRUNC('month', %s::DATE)")
        params.append(f"{from_month}-01")

    if to_month:
        conditions.append("order_month <= DATE_TRUNC('month', %s::DATE)")
        params.append(f"{to_month}-01")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = f"""
        SELECT sku, TO_CHAR(order_month, 'YYYY-MM') AS order_month,
               return_window_days, total_ordered,
               returned_30d,  return_rate_30d,  is_30d_closed,
               returned_100d, return_rate_100d, is_100d_closed
        FROM sku_monthly_stats
        {where}
        ORDER BY sku, order_month
    """

    with get_db() as conn:
        rows = query(conn, sql, params if params else None)

    return [dict(r) for r in rows]


# ── CSV export ────────────────────────────────────────────────────────────────

@app.get("/api/returns/export")
def export_returns(
    skus:  Optional[str] = Query(default=None),
    from_month: Optional[str] = Query(default=None, alias="from"),
    to_month:   Optional[str] = Query(default=None, alias="to"),
    x_portal_password: Optional[str] = Header(default=None),
):
    require_auth(x_portal_password)
    rows = get_returns(skus, from_month, to_month, x_portal_password)

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[
        "sku", "order_month", "return_window_days", "total_ordered",
        "returned_30d", "return_rate_30d", "is_30d_closed",
        "returned_100d", "return_rate_100d", "is_100d_closed",
    ])
    writer.writeheader()
    writer.writerows(rows)
    output.seek(0)

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=returns_export.csv"},
    )


# ── Sync ──────────────────────────────────────────────────────────────────────

@app.get("/api/sync/status")
def sync_status(x_portal_password: Optional[str] = Header(default=None)):
    require_auth(x_portal_password)
    status = get_status()

    with get_db() as conn:
        last = fetchone(conn, """
            SELECT sync_type, status, orders_synced, refunds_synced,
                   started_at, completed_at, error_message
            FROM sync_log
            WHERE status = 'complete'
            ORDER BY completed_at DESC
            LIMIT 1
        """)

    return {
        "live":    status,
        "last_completed": dict(last) if last else None,
    }


@app.post("/api/sync/trigger")
def sync_trigger(x_portal_password: Optional[str] = Header(default=None)):
    require_auth(x_portal_password)
    started = trigger_sync()
    if not started:
        raise HTTPException(status_code=409, detail="Sync already running")
    return {"started": True}


# ── Settings ──────────────────────────────────────────────────────────────────

class SettingsPayload(BaseModel):
    return_buffer_days: int
    return_rate_thresholds: dict = {}


@app.get("/api/settings")
def get_settings(x_portal_password: Optional[str] = Header(default=None)):
    require_auth(x_portal_password)
    with get_db() as conn:
        buf_row = fetchone(conn,
            "SELECT value::INT AS return_buffer_days FROM settings WHERE key = 'return_buffer_days'")
        thr_row = fetchone(conn,
            "SELECT value FROM settings WHERE key = 'return_rate_thresholds'")
    thresholds = json.loads(thr_row["value"]) if thr_row else {"_default": 15}
    return {
        "return_buffer_days":    buf_row["return_buffer_days"] if buf_row else 10,
        "return_rate_thresholds": thresholds,
    }


@app.post("/api/settings")
def save_settings(
    payload: SettingsPayload,
    x_portal_password: Optional[str] = Header(default=None),
):
    require_auth(x_portal_password)
    if payload.return_buffer_days < 0 or payload.return_buffer_days > 60:
        raise HTTPException(status_code=400, detail="Buffer must be between 0 and 60 days")

    with get_db() as conn:
        execute(conn,
            "UPDATE settings SET value = %s WHERE key = 'return_buffer_days'",
            (str(payload.return_buffer_days),))
        execute(conn, """
            INSERT INTO settings (key, value) VALUES ('return_rate_thresholds', %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (json.dumps(payload.return_rate_thresholds),))

    started = trigger_reaggregate()
    return {"saved": True, "reaggregating": started}
