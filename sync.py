import os
import re
import time
import threading
from datetime import datetime, timezone
from dateutil.parser import parse as parse_dt

import requests

from database import get_db, execute, fetchone, query
from aggregate import run_aggregation

SHOPIFY_STORE   = os.environ.get("SHOPIFY_STORE", "mysheepi")
SHOPIFY_TOKEN   = os.environ.get("SHOPIFY_TOKEN", "")
API_VERSION     = os.environ.get("SHOPIFY_API_VERSION", "2024-07")
FULL_SYNC_START = "2024-07-15T00:00:00Z"
BASE_URL        = f"https://{SHOPIFY_STORE}.myshopify.com/admin/api/{API_VERSION}/orders.json"

# In-memory status (shared between background thread and API handlers)
_status_lock = threading.Lock()
_status = {
    "running": False,
    "type": None,
    "orders_synced": 0,
    "refunds_synced": 0,
    "started_at": None,
    "completed_at": None,
    "error": None,
}


def get_status():
    with _status_lock:
        return dict(_status)


def _set_status(**kwargs):
    with _status_lock:
        _status.update(kwargs)


# ── Shopify HTTP helpers ─────────────────────────────────────────────────────

def _headers():
    return {"X-Shopify-Access-Token": SHOPIFY_TOKEN}


def _fetch_page(url, params, max_retries=3):
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, params=params, headers=_headers(), timeout=30)

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 4))
                time.sleep(retry_after)
                continue

            resp.raise_for_status()

            # Throttle if nearing rate limit
            rate_header = resp.headers.get("X-Shopify-Shop-Api-Call-Limit", "")
            if rate_header:
                used, total = map(int, rate_header.split("/"))
                if total > 0 and used / total > 0.8:
                    time.sleep(0.5)

            return resp

        except requests.Timeout:
            if attempt == max_retries - 1:
                raise
            time.sleep(2 ** attempt)

    raise RuntimeError("Shopify API: max retries exceeded")


def _parse_next_cursor(link_header):
    if not link_header:
        return None
    for part in link_header.split(","):
        if 'rel="next"' in part:
            m = re.search(r'page_info=([^>&"]+)', part)
            if m:
                return m.group(1)
    return None


# ── Order upsert ─────────────────────────────────────────────────────────────

def _fetch_return_dates(order_id, return_ids):
    """One API call per order: return {return_id: date_customer_registered_return}."""
    if not return_ids:
        return {}
    url = f"https://{SHOPIFY_STORE}.myshopify.com/admin/api/{API_VERSION}/orders/{order_id}/returns.json"
    try:
        resp = _fetch_page(url, {"limit": 250})
        result = {}
        for ret in resp.json().get("returns", []):
            rid = ret.get("id")
            if rid in return_ids and ret.get("created_at"):
                result[rid] = parse_dt(ret["created_at"]).date()
        return result
    except Exception:
        return {}


def _upsert_order(conn, order):
    order_date     = parse_dt(order["created_at"]).date()
    updated_at_raw = order.get("updated_at", order["created_at"])

    row = fetchone(conn, """
        INSERT INTO orders (shopify_order_id, order_date, shopify_updated_at)
        VALUES (%s, %s, %s)
        ON CONFLICT (shopify_order_id) DO UPDATE
            SET shopify_updated_at = EXCLUDED.shopify_updated_at,
                synced_at = now()
        RETURNING id
    """, (order["id"], order_date, updated_at_raw))
    order_db_id = row["id"]

    # Build shopify_line_item_id → db_id map for fulfilled items
    line_item_map = {}
    for item in order.get("line_items", []):
        if item.get("fulfillment_status") != "fulfilled":
            continue
        sku = (item.get("sku") or "").strip()
        if not sku:
            continue

        # Only track SKUs we know about
        known = fetchone(conn, "SELECT 1 FROM sku_config WHERE sku = %s", (sku,))
        if not known:
            continue

        unit_price = float(item.get("price") or 0)
        db_row = fetchone(conn, """
            INSERT INTO order_line_items
                (shopify_line_item_id, order_id, sku, quantity, order_date, unit_price)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (shopify_line_item_id) DO UPDATE
                SET quantity   = EXCLUDED.quantity,
                    unit_price = EXCLUDED.unit_price
            RETURNING id
        """, (item["id"], order_db_id, sku, item["quantity"], order_date, unit_price))
        line_item_map[item["id"]] = db_row["id"]

    # Pre-collect all return_ids so we can fetch their registration dates in one API call.
    all_return_ids = set()
    for refund in order.get("refunds", []):
        rid = (refund.get("return") or {}).get("id")
        if rid:
            all_return_ids.add(rid)
    return_date_lookup = _fetch_return_dates(order["id"], all_return_ids)

    # Upsert refunds
    refund_count = 0
    for refund in order.get("refunds", []):
        refund_date = parse_dt(refund["created_at"]).date()
        # return_id is set only when the merchant created a Shopify return (DHL label issued).
        # NULL means standalone refund (goodwill / partial discount — customer keeps the item).
        return_obj  = refund.get("return") or {}
        return_id   = return_obj.get("id")  # BIGINT or None

        for rli in refund.get("refund_line_items", []):
            line_item_obj = rli.get("line_item") or {}
            if line_item_obj.get("fulfillment_status") != "fulfilled":
                continue

            sku = (line_item_obj.get("sku") or "").strip()
            if not sku:
                continue

            shopify_li_id = rli.get("line_item_id")
            db_li_id = line_item_map.get(shopify_li_id)

            if db_li_id is None:
                # Might be an unfulfilled item we skipped — look up by shopify id
                db_row = fetchone(conn,
                    "SELECT id FROM order_line_items WHERE shopify_line_item_id = %s",
                    (shopify_li_id,))
                if not db_row:
                    continue
                db_li_id = db_row["id"]

            qty = rli.get("quantity", 0)
            if qty <= 0:
                continue

            # For physical returns: days counted from when customer registered the return.
            # For goodwill refunds (no return object): days counted from when refund was issued.
            return_date = return_date_lookup.get(return_id) if return_id else None
            days = max(0, ((return_date or refund_date) - order_date).days)
            restock_type     = rli.get("restock_type") or "return"
            refund_subtotal  = float(rli.get("subtotal") or 0)

            execute(conn, """
                INSERT INTO refund_line_items (
                    shopify_refund_id, shopify_refund_line_item_id,
                    order_line_item_id, sku, qty_returned,
                    order_date, refund_date, return_date, days_to_refund,
                    restock_type, return_id, refund_subtotal
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (shopify_refund_line_item_id) DO UPDATE
                    SET qty_returned    = EXCLUDED.qty_returned,
                        refund_date     = EXCLUDED.refund_date,
                        return_date     = EXCLUDED.return_date,
                        days_to_refund  = EXCLUDED.days_to_refund,
                        restock_type    = EXCLUDED.restock_type,
                        return_id       = EXCLUDED.return_id,
                        refund_subtotal = EXCLUDED.refund_subtotal
            """, (
                refund["id"], rli["id"],
                db_li_id, sku, qty,
                order_date, refund_date, return_date, days,
                restock_type, return_id, refund_subtotal,
            ))
            refund_count += 1

    return refund_count


# ── Sync orchestration ───────────────────────────────────────────────────────

def _determine_sync_type():
    """Return ('full', None) or ('incremental', watermark_iso_string)."""
    with get_db() as conn:
        row = fetchone(conn, """
            SELECT watermark FROM sync_log
            WHERE status = 'complete'
            ORDER BY completed_at DESC
            LIMIT 1
        """)
    if row and row["watermark"]:
        return "incremental", row["watermark"].isoformat()
    return "full", None


def _resume_cursor():
    """If a previous full sync was interrupted, return its cursor."""
    with get_db() as conn:
        row = fetchone(conn, """
            SELECT id, cursor FROM sync_log
            WHERE sync_type = 'full' AND status = 'running'
            ORDER BY started_at DESC
            LIMIT 1
        """)
    if row and row["cursor"]:
        return row["id"], row["cursor"]
    return None, None


def _create_sync_log(sync_type):
    with get_db() as conn:
        row = fetchone(conn,
            "INSERT INTO sync_log (sync_type, status) VALUES (%s, 'running') RETURNING id",
            (sync_type,))
        return row["id"]


def _update_sync_progress(sync_id, cursor, orders, refunds):
    with get_db() as conn:
        execute(conn, """
            UPDATE sync_log
               SET cursor = %s, orders_synced = %s, refunds_synced = %s
             WHERE id = %s
        """, (cursor, orders, refunds, sync_id))


def _complete_sync(sync_id, orders, refunds):
    watermark = datetime.now(timezone.utc)
    with get_db() as conn:
        execute(conn, """
            UPDATE sync_log
               SET status = 'complete', completed_at = now(),
                   watermark = %s, orders_synced = %s, refunds_synced = %s,
                   cursor = NULL
             WHERE id = %s
        """, (watermark, orders, refunds, sync_id))


def _fail_sync(sync_id, error_msg):
    with get_db() as conn:
        execute(conn, """
            UPDATE sync_log
               SET status = 'error', completed_at = now(), error_message = %s
             WHERE id = %s
        """, (str(error_msg)[:1000], sync_id))


def _run_sync_worker():
    sync_type, watermark = _determine_sync_type()

    # Resume an interrupted full sync if one exists
    resume_id, resume_cursor = _resume_cursor()

    if resume_id:
        sync_id = resume_id
        # Read from DB — _status resets to 0 on process restart
        with get_db() as conn:
            row = fetchone(conn,
                "SELECT orders_synced, refunds_synced FROM sync_log WHERE id = %s",
                (resume_id,))
        orders_done  = row["orders_synced"]  if row else 0
        refunds_done = row["refunds_synced"] if row else 0
    else:
        sync_id      = _create_sync_log(sync_type)
        orders_done  = 0
        refunds_done = 0

    _set_status(
        running=True, type=sync_type,
        orders_synced=0, refunds_synced=0,
        started_at=datetime.now(timezone.utc).isoformat(),
        completed_at=None, error=None,
    )

    try:
        if resume_cursor:
            # Resume mid-pagination (full sync only)
            params = {"limit": 250, "page_info": resume_cursor}
        elif sync_type == "full":
            params = {
                "status": "any",
                "limit": 250,
                "created_at_min": FULL_SYNC_START,
                "order": "created_at asc",
            }
        else:
            params = {
                "status": "any",
                "limit": 250,
                "updated_at_min": watermark,
                "order": "updated_at asc",
            }

        while True:
            resp  = _fetch_page(BASE_URL, params)
            data  = resp.json()
            orders = data.get("orders", [])

            if not orders:
                break

            with get_db() as conn:
                for order in orders:
                    ref_count = _upsert_order(conn, order)
                    orders_done  += 1
                    refunds_done += ref_count

            _set_status(orders_synced=orders_done, refunds_synced=refunds_done)

            next_cursor = _parse_next_cursor(resp.headers.get("Link", ""))

            # Save checkpoint after each page (full sync only)
            if sync_type == "full":
                _update_sync_progress(sync_id, next_cursor, orders_done, refunds_done)

            if not next_cursor:
                break

            params = {"limit": 250, "page_info": next_cursor}

        # Rebuild monthly stats
        with get_db() as conn:
            run_aggregation(conn)

        _complete_sync(sync_id, orders_done, refunds_done)
        _set_status(running=False, completed_at=datetime.now(timezone.utc).isoformat())

    except Exception as exc:
        _fail_sync(sync_id, exc)
        _set_status(running=False, error=str(exc))
        raise


def trigger_sync():
    """Start sync in background thread. Returns False if already running."""
    if get_status()["running"]:
        return False
    t = threading.Thread(target=_run_sync_worker, daemon=True)
    t.start()
    return True


def trigger_reaggregate():
    """Re-run aggregation without touching Shopify (used after buffer change)."""
    def _worker():
        _set_status(running=True, type="reaggregate",
                    started_at=datetime.now(timezone.utc).isoformat(),
                    completed_at=None, error=None)
        try:
            with get_db() as conn:
                run_aggregation(conn)
            _set_status(running=False, completed_at=datetime.now(timezone.utc).isoformat())
        except Exception as exc:
            _set_status(running=False, error=str(exc))

    if get_status()["running"]:
        return False
    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return True
