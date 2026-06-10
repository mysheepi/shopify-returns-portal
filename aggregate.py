from database import execute, fetchone


def run_aggregation(conn):
    buf_row = fetchone(conn, "SELECT value::INT AS days FROM settings WHERE key = 'return_buffer_days'")
    buffer = buf_row["days"] if buf_row else 10

    # Drop stats for SKUs removed from sku_config — /api/returns reads
    # sku_monthly_stats directly, so stale rows would linger forever.
    execute(conn, """
        DELETE FROM sku_monthly_stats
        WHERE sku NOT IN (SELECT sku FROM sku_config)
    """)

    sql = """
    WITH ordered AS (
        SELECT
            oli.sku,
            DATE_TRUNC('month', oli.order_date)::DATE  AS order_month,
            SUM(oli.quantity)                          AS total_ordered,
            SUM(oli.unit_price * oli.quantity)         AS total_revenue,
            MAX(oli.order_date)                        AS last_order_date
        FROM order_line_items oli
        JOIN sku_config sc ON sc.sku = oli.sku
        WHERE DATE_TRUNC('month', oli.order_date) < DATE_TRUNC('month', CURRENT_DATE)
        GROUP BY oli.sku, DATE_TRUNC('month', oli.order_date)
    ),
    returned AS (
        SELECT
            oli.sku,
            DATE_TRUNC('month', oli.order_date)::DATE  AS order_month,
            SUM(CASE WHEN rli.days_to_refund <= 30  + %(buf)s
                     THEN rli.qty_returned END)                                    AS returned_30d,
            SUM(CASE WHEN rli.days_to_refund <= 100 + %(buf)s
                     THEN rli.qty_returned END)                                    AS returned_100d,
            SUM(CASE WHEN rli.days_to_refund <= 30  + %(buf)s
                      AND rli.return_id IS NOT NULL
                     THEN rli.qty_returned END)                                    AS returned_30d_physical,
            SUM(CASE WHEN rli.days_to_refund <= 100 + %(buf)s
                      AND rli.return_id IS NOT NULL
                     THEN rli.qty_returned END)                                    AS returned_100d_physical,
            SUM(rli.refund_subtotal)                                                AS total_refunded_amount
        FROM refund_line_items rli
        JOIN order_line_items oli ON oli.id = rli.order_line_item_id
        GROUP BY oli.sku, DATE_TRUNC('month', oli.order_date)
    )
    INSERT INTO sku_monthly_stats (
        sku, order_month, return_window_days, total_ordered,
        returned_30d,           return_rate_30d,          is_30d_closed,
        returned_100d,          return_rate_100d,         is_100d_closed,
        returned_30d_physical,  return_rate_30d_physical,
        returned_100d_physical, return_rate_100d_physical,
        total_revenue,
        total_refunded_amount,  refund_rate_monetary,
        last_order_date, refreshed_at
    )
    SELECT
        o.sku,
        o.order_month,
        sc.return_window_days,
        o.total_ordered,

        COALESCE(r.returned_30d,  0)                                         AS returned_30d,
        COALESCE(r.returned_30d,  0)::NUMERIC / NULLIF(o.total_ordered, 0)   AS return_rate_30d,
        (o.last_order_date + (30  + %(buf)s)) < CURRENT_DATE                 AS is_30d_closed,

        COALESCE(r.returned_100d, 0)                                         AS returned_100d,
        COALESCE(r.returned_100d, 0)::NUMERIC / NULLIF(o.total_ordered, 0)   AS return_rate_100d,
        (o.last_order_date + (100 + %(buf)s)) < CURRENT_DATE                 AS is_100d_closed,

        COALESCE(r.returned_30d_physical,  0)                                        AS returned_30d_physical,
        COALESCE(r.returned_30d_physical,  0)::NUMERIC / NULLIF(o.total_ordered, 0)  AS return_rate_30d_physical,

        COALESCE(r.returned_100d_physical, 0)                                        AS returned_100d_physical,
        COALESCE(r.returned_100d_physical, 0)::NUMERIC / NULLIF(o.total_ordered, 0)  AS return_rate_100d_physical,

        COALESCE(o.total_revenue, 0)                                                 AS total_revenue,

        COALESCE(r.total_refunded_amount, 0)                                         AS total_refunded_amount,
        COALESCE(r.total_refunded_amount, 0) / NULLIF(o.total_revenue, 0)            AS refund_rate_monetary,

        o.last_order_date,
        now()                                                                  AS refreshed_at

    FROM ordered o
    JOIN sku_config sc ON sc.sku = o.sku
    LEFT JOIN returned r ON r.sku = o.sku AND r.order_month = o.order_month

    ON CONFLICT (sku, order_month) DO UPDATE SET
        return_window_days         = EXCLUDED.return_window_days,
        total_ordered              = EXCLUDED.total_ordered,
        returned_30d               = EXCLUDED.returned_30d,
        return_rate_30d            = EXCLUDED.return_rate_30d,
        is_30d_closed              = EXCLUDED.is_30d_closed,
        returned_100d              = EXCLUDED.returned_100d,
        return_rate_100d           = EXCLUDED.return_rate_100d,
        is_100d_closed             = EXCLUDED.is_100d_closed,
        returned_30d_physical      = EXCLUDED.returned_30d_physical,
        return_rate_30d_physical   = EXCLUDED.return_rate_30d_physical,
        returned_100d_physical     = EXCLUDED.returned_100d_physical,
        return_rate_100d_physical  = EXCLUDED.return_rate_100d_physical,
        total_revenue              = EXCLUDED.total_revenue,
        total_refunded_amount      = EXCLUDED.total_refunded_amount,
        refund_rate_monetary       = EXCLUDED.refund_rate_monetary,
        last_order_date            = EXCLUDED.last_order_date,
        refreshed_at               = EXCLUDED.refreshed_at
    """
    execute(conn, sql, {"buf": buffer})
