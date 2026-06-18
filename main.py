import os
import re as _re
import json
import logging
import secrets
from contextlib import asynccontextmanager
from typing import Optional

_MONTH_RE = _re.compile(r'^\d{4}-(0[1-9]|1[0-2])$')

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
import io
import csv

from database import init_db, get_db, query, execute, fetchone
from sync import trigger_sync, trigger_reaggregate, get_status

DEFAULT_RETURN_RATE_THRESHOLD = 15.0
THRESHOLD_LOOKBACK_MONTHS = 12

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
    if not PORTAL_PASSWORD or not x_portal_password or not secrets.compare_digest(
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

class SkuCreatePayload(BaseModel):
    sku: str = Field(..., min_length=1, max_length=120)
    product_name: str = Field(..., min_length=1, max_length=255)
    return_window_days: int
    is_active: bool = True


class SkuUpdatePayload(BaseModel):
    product_name: str = Field(..., min_length=1, max_length=255)
    return_window_days: int
    is_active: bool = True


def _validate_sku_values(sku, product_name, return_window_days):
    sku = str(sku or "").strip()
    product_name = str(product_name or "").strip()
    if not sku:
        raise HTTPException(status_code=400, detail="SKU is required")
    if not product_name:
        raise HTTPException(status_code=400, detail="Product name is required")
    if return_window_days not in (30, 100):
        raise HTTPException(status_code=400, detail="Return window must be 30 or 100 days")
    return sku, product_name, return_window_days


@app.get("/api/skus")
def get_skus(
    include_inactive: bool = Query(default=False),
    x_portal_password: Optional[str] = Header(default=None),
):
    require_auth(x_portal_password)
    where = "" if include_inactive else "WHERE is_active = TRUE"
    with get_db() as conn:
        rows = query(conn, f"""
            SELECT sku, product_name, return_window_days, is_active
            FROM sku_config
            {where}
            ORDER BY product_name, sku
        """)
    return [dict(r) for r in rows]


@app.post("/api/skus")
def create_sku(
    payload: SkuCreatePayload,
    reaggregate: bool = Query(default=True),
    x_portal_password: Optional[str] = Header(default=None),
):
    require_auth(x_portal_password)
    sku, product_name, return_window_days = _validate_sku_values(
        payload.sku, payload.product_name, payload.return_window_days
    )
    with get_db() as conn:
        row = fetchone(conn, """
            INSERT INTO sku_config (sku, product_name, return_window_days, is_active, updated_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (sku) DO UPDATE
                SET product_name = EXCLUDED.product_name,
                    return_window_days = EXCLUDED.return_window_days,
                    is_active = EXCLUDED.is_active,
                    updated_at = now()
            RETURNING sku, product_name, return_window_days, is_active
        """, (sku, product_name, return_window_days, payload.is_active))

    started = trigger_reaggregate() if reaggregate else False
    return {"saved": True, "reaggregating": started, "sku": dict(row)}


@app.patch("/api/skus/{sku}")
def update_sku(
    sku: str,
    payload: SkuUpdatePayload,
    reaggregate: bool = Query(default=True),
    x_portal_password: Optional[str] = Header(default=None),
):
    require_auth(x_portal_password)
    sku, product_name, return_window_days = _validate_sku_values(
        sku, payload.product_name, payload.return_window_days
    )
    with get_db() as conn:
        row = fetchone(conn, """
            UPDATE sku_config
               SET product_name = %s,
                   return_window_days = %s,
                   is_active = %s,
                   updated_at = now()
             WHERE sku = %s
             RETURNING sku, product_name, return_window_days, is_active
        """, (product_name, return_window_days, payload.is_active, sku))
    if not row:
        raise HTTPException(status_code=404, detail="SKU not found")

    started = trigger_reaggregate() if reaggregate else False
    return {"saved": True, "reaggregating": started, "sku": dict(row)}


@app.delete("/api/skus/{sku}")
def deactivate_sku(
    sku: str,
    reaggregate: bool = Query(default=True),
    x_portal_password: Optional[str] = Header(default=None),
):
    require_auth(x_portal_password)
    with get_db() as conn:
        row = fetchone(conn, """
            UPDATE sku_config
               SET is_active = FALSE,
                   updated_at = now()
             WHERE sku = %s
             RETURNING sku, product_name, return_window_days, is_active
        """, (sku.strip(),))
    if not row:
        raise HTTPException(status_code=404, detail="SKU not found")

    started = trigger_reaggregate() if reaggregate else False
    return {"saved": True, "reaggregating": started, "sku": dict(row)}


# ── Returns data ─────────────────────────────────────────────────────────────

@app.get("/api/returns")
def get_returns(
    skus:  Optional[str] = Query(default=None, description="Comma-separated SKU list"),
    from_month: Optional[str] = Query(default=None, alias="from", description="YYYY-MM"),
    to_month:   Optional[str] = Query(default=None, alias="to",   description="YYYY-MM"),
    x_portal_password: Optional[str] = Header(default=None),
):
    require_auth(x_portal_password)

    if from_month and not _MONTH_RE.match(from_month):
        raise HTTPException(status_code=400, detail="Invalid 'from' month, expected YYYY-MM")
    if to_month and not _MONTH_RE.match(to_month):
        raise HTTPException(status_code=400, detail="Invalid 'to' month, expected YYYY-MM")

    conditions = ["sc.is_active = TRUE"]
    params: list = []

    if skus:
        sku_list = [s.strip() for s in skus.split(",") if s.strip()]
        if sku_list:
            conditions.append("sms.sku = ANY(%s)")
            params.append(sku_list)

    if from_month:
        conditions.append("sms.order_month >= DATE_TRUNC('month', %s::DATE)")
        params.append(f"{from_month}-01")

    if to_month:
        conditions.append("sms.order_month <= DATE_TRUNC('month', %s::DATE)")
        params.append(f"{to_month}-01")

    where = "WHERE " + " AND ".join(conditions)
    sql = f"""
        SELECT sms.sku, TO_CHAR(sms.order_month, 'YYYY-MM') AS order_month,
               sc.return_window_days, sms.total_ordered,
               sms.returned_30d,           sms.return_rate_30d,           sms.is_30d_closed,
               sms.returned_100d,          sms.return_rate_100d,          sms.is_100d_closed,
               sms.returned_30d_physical,  sms.return_rate_30d_physical,
               sms.returned_100d_physical, sms.return_rate_100d_physical,
               sms.total_revenue,
               sms.total_refunded_amount,  sms.refund_rate_monetary
        FROM sku_monthly_stats sms
        JOIN sku_config sc ON sc.sku = sms.sku
        {where}
        ORDER BY sms.sku, sms.order_month
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
        "returned_30d",          "return_rate_30d",          "is_30d_closed",
        "returned_100d",         "return_rate_100d",         "is_100d_closed",
        "returned_30d_physical", "return_rate_30d_physical",
        "returned_100d_physical","return_rate_100d_physical",
        "total_revenue",
        "total_refunded_amount", "refund_rate_monetary",
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
def sync_trigger(
    force: bool = Query(default=False),
    x_portal_password: Optional[str] = Header(default=None),
):
    require_auth(x_portal_password)
    # Watermark clearing for force=True lives inside trigger_sync, AFTER the
    # running-lock is acquired — wiping it here would destroy the watermark
    # even when the trigger is rejected with 409.
    started = trigger_sync(force=force)
    if not started:
        raise HTTPException(status_code=409, detail="Sync already running")
    return {"started": True}


@app.post("/api/returns/reaggregate")
def reaggregate_returns(x_portal_password: Optional[str] = Header(default=None)):
    require_auth(x_portal_password)
    started = trigger_reaggregate()
    if not started:
        raise HTTPException(status_code=409, detail="Sync already running")
    return {"started": True}


# ── Settings ──────────────────────────────────────────────────────────────────

class SettingsPayload(BaseModel):
    return_buffer_days: int
    return_rate_thresholds: dict = Field(default_factory=dict)


def _coerce_threshold(value):
    threshold = float(value)
    if threshold < 0 or threshold > 100:
        raise ValueError("threshold out of range")
    return threshold


def _load_threshold_overrides(raw_value):
    try:
        loaded = json.loads(raw_value) if raw_value else {}
    except (TypeError, json.JSONDecodeError):
        loaded = {}

    if not isinstance(loaded, dict):
        loaded = {}

    overrides = {}
    for key, value in loaded.items():
        try:
            overrides[str(key)] = _coerce_threshold(value)
        except (TypeError, ValueError):
            continue

    # No setdefault here: _resolved_thresholds uses "_default" membership to
    # report whether the fallback threshold was explicitly overridden.
    return overrides


def _validate_threshold_overrides(payload):
    overrides = {}
    for key, value in payload.items():
        if value in (None, ""):
            continue
        try:
            overrides[str(key)] = _coerce_threshold(value)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Thresholds must be between 0 and 100")

    # Don't persist the fallback when it just equals the built-in default —
    # the frontend always echoes it back, and storing it would make the
    # '_default' source read "override" for a value the user never touched.
    if overrides.get("_default") == DEFAULT_RETURN_RATE_THRESHOLD:
        overrides.pop("_default")
    return overrides


def _threshold_sql():
    return """
        WITH eligible_stats AS (
            SELECT
                sms.sku,
                CASE
                    WHEN sc.return_window_days = 100 THEN sms.return_rate_100d
                    ELSE sms.return_rate_30d
                END AS return_rate,
                ROW_NUMBER() OVER (
                    PARTITION BY sms.sku
                    ORDER BY sms.order_month DESC
                ) AS rn
            FROM sku_monthly_stats sms
            JOIN sku_config sc ON sc.sku = sms.sku
            WHERE sc.is_active = TRUE
              AND (
                    (
                        sc.return_window_days = 100
                        AND sms.is_100d_closed
                        AND sms.return_rate_100d IS NOT NULL
                    )
                    OR (
                        sc.return_window_days <> 100
                        AND sms.is_30d_closed
                        AND sms.return_rate_30d IS NOT NULL
                    )
                )
        ),
        trailing_thresholds AS (
            SELECT
                sku,
                AVG(return_rate) * 100 AS threshold_pct,
                COUNT(*) AS months_count
            FROM eligible_stats
            WHERE rn <= %(months)s
            GROUP BY sku
        )
        SELECT
            sc.sku,
            sc.return_window_days,
            tt.threshold_pct,
            COALESCE(tt.months_count, 0) AS months_count
        FROM sku_config sc
        LEFT JOIN trailing_thresholds tt ON tt.sku = sc.sku
        WHERE sc.is_active = TRUE
        ORDER BY sc.sku
    """


def _resolved_thresholds(conn, overrides):
    default_threshold = overrides.get("_default", DEFAULT_RETURN_RATE_THRESHOLD)
    rows = query(conn, _threshold_sql(), {"months": THRESHOLD_LOOKBACK_MONTHS})

    thresholds = {"_default": default_threshold}
    sources = {
        "_default": {
            "value": default_threshold,
            "source": "override" if "_default" in overrides else "default",
            "auto_value": None,
            "basis_months": None,
            "months_count": 0,
        }
    }

    for row in rows:
        sku = row["sku"]
        auto_value = row["threshold_pct"]
        auto_value = float(auto_value) if auto_value is not None else None
        months_count = int(row["months_count"] or 0)

        if sku in overrides:
            value = overrides[sku]
            source = "override"
        elif auto_value is not None:
            value = auto_value
            source = "auto"
        else:
            value = default_threshold
            source = "default"

        thresholds[sku] = value
        sources[sku] = {
            "value": value,
            "source": source,
            "auto_value": auto_value,
            "basis_months": THRESHOLD_LOOKBACK_MONTHS,
            "months_count": months_count,
            "return_window_days": row["return_window_days"],
        }

    return thresholds, sources


@app.get("/api/settings")
def get_settings(x_portal_password: Optional[str] = Header(default=None)):
    require_auth(x_portal_password)
    with get_db() as conn:
        buf_row = fetchone(conn,
            "SELECT value::INT AS return_buffer_days FROM settings WHERE key = 'return_buffer_days'")
        thr_row = fetchone(conn,
            "SELECT value FROM settings WHERE key = 'return_rate_thresholds'")
        overrides = _load_threshold_overrides(thr_row["value"] if thr_row else None)
        thresholds, threshold_sources = _resolved_thresholds(conn, overrides)

    return {
        "return_buffer_days":    buf_row["return_buffer_days"] if buf_row else 10,
        "return_rate_thresholds": thresholds,
        "return_rate_threshold_overrides": {
            "_default": DEFAULT_RETURN_RATE_THRESHOLD, **overrides,
        },
        "return_rate_threshold_sources": threshold_sources,
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
        threshold_overrides = _validate_threshold_overrides(payload.return_rate_thresholds)
        execute(conn, """
            INSERT INTO settings (key, value) VALUES ('return_buffer_days', %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (str(payload.return_buffer_days),))
        execute(conn, """
            INSERT INTO settings (key, value) VALUES ('return_rate_thresholds', %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (json.dumps(threshold_overrides),))

    started = trigger_reaggregate()
    return {"saved": True, "reaggregating": started}
