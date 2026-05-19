from database import execute, fetchone


def run_aggregation(conn):
    buf_row = fetchone(conn, "SELECT value::INT AS days FROM settings WHERE key = 'return_buffer_days'")
    buffer = buf_row["days"] if buf_row else 10

    # Two separate CTEs avoid the fan-out that occurs when a single order_line_item
    # has multiple refund_line_items rows: a LEFT JOIN would count oli.quantity once
    # per refund row, inflating total_ordered and distorting return rates.
    sql = """
    WITH ordered AS (
        -- One row per (sku, order_month): clean sum of units ordered, no fan-out.
        SELECT
            oli.sku,
            DATE_TRUNC('month', oli.order_date)::DATE  AS order_month,
            SUM(oli.quantity)                          AS total_ordered,
            MAX(oli.order_date)                        AS last_order_date
        FROM order_line_items oli
        JOIN sku_config sc ON sc.sku = oli.sku
        WHERE DATE_TRUNC('month', oli.order_date) < DATE_TRUNC('month', CURRENT_DATE)
        GROUP BY oli.sku, DATE_TRUNC('month', oli.order_date)
    ),
    returned AS (
        -- One row per refund_line_item: sum returned qty by the order's month.
        -- Starts from rli so there is no fan-out — each rli row contributes exactly once.
        SELECT
            oli.sku,
            DATE_TRUNC('month', oli.order_date)::DATE  AS order_month,
            SUM(CASE WHEN rli.days_to_refund <= 30 + %(buf)s  THEN rli.qty_returned END) AS returned_30d,
            SUM(CASE WHEN rli.days_to_refund <= 100 + %(buf)s THEN rli.qty_returned END) AS returned_100d
        FROM refund_line_items rli
        JOIN order_line_items oli ON oli.id = rli.order_line_item_id
        GROUP BY oli.sku, DATE_TRUNC('month', oli.order_date)
    )
    INSERT INTO sku_monthly_stats (
        sku, order_month, return_window_days, total_ordered,
        returned_30d,  return_rate_30d,  is_30d_closed,
        returned_100d, return_rate_100d, is_100d_closed,
        last_order_date, refreshed_at
    )
    SELECT
        o.sku,
        o.order_month,
        sc.return_window_days,
        o.total_ordered,

        COALESCE(r.returned_30d,  0)                                         AS returned_30d,
        COALESCE(r.returned_30d,  0)::NUMERIC / NULLIF(o.total_ordered, 0)   AS return_rate_30d,
        (o.last_order_date + (30 + %(buf)s)) < CURRENT_DATE                  AS is_30d_closed,

        COALESCE(r.returned_100d, 0)                                         AS returned_100d,
        COALESCE(r.returned_100d, 0)::NUMERIC / NULLIF(o.total_ordered, 0)   AS return_rate_100d,
        (o.last_order_date + (100 + %(buf)s)) < CURRENT_DATE                 AS is_100d_closed,

        o.last_order_date,
        now()                                                                 AS refreshed_at

    FROM ordered o
    JOIN sku_config sc ON sc.sku = o.sku
    LEFT JOIN returned r ON r.sku = o.sku AND r.order_month = o.order_month

    ON CONFLICT (sku, order_month) DO UPDATE SET
        return_window_days = EXCLUDED.return_window_days,
        total_ordered      = EXCLUDED.total_ordered,
        returned_30d       = EXCLUDED.returned_30d,
        return_rate_30d    = EXCLUDED.return_rate_30d,
        is_30d_closed      = EXCLUDED.is_30d_closed,
        returned_100d      = EXCLUDED.returned_100d,
        return_rate_100d   = EXCLUDED.return_rate_100d,
        is_100d_closed     = EXCLUDED.is_100d_closed,
        last_order_date    = EXCLUDED.last_order_date,
        refreshed_at       = EXCLUDED.refreshed_at
    """
    execute(conn, sql, {"buf": buffer})
