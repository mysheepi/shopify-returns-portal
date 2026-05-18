from database import execute, fetchone


def run_aggregation(conn):
    buf_row = fetchone(conn, "SELECT value::INT AS days FROM settings WHERE key = 'return_buffer_days'")
    buffer = buf_row["days"] if buf_row else 10

    sql = """
    INSERT INTO sku_monthly_stats (
        sku, order_month, return_window_days, total_ordered,
        returned_30d,  return_rate_30d,  is_30d_closed,
        returned_100d, return_rate_100d, is_100d_closed,
        last_order_date, refreshed_at
    )
    SELECT
        oli.sku,
        DATE_TRUNC('month', oli.order_date)::DATE          AS order_month,
        sc.return_window_days,
        SUM(oli.quantity)                                   AS total_ordered,

        -- 30-day window + buffer
        COALESCE(SUM(
            CASE WHEN rli.days_to_refund <= 30 + %(buf)s
                 THEN rli.qty_returned END
        ), 0)                                               AS returned_30d,

        COALESCE(SUM(
            CASE WHEN rli.days_to_refund <= 30 + %(buf)s
                 THEN rli.qty_returned END
        ), 0)::NUMERIC
            / NULLIF(SUM(oli.quantity), 0)                 AS return_rate_30d,

        (MAX(oli.order_date) + (30 + %(buf)s)) < CURRENT_DATE  AS is_30d_closed,

        -- 100-day window + buffer
        COALESCE(SUM(
            CASE WHEN rli.days_to_refund <= 100 + %(buf)s
                 THEN rli.qty_returned END
        ), 0)                                               AS returned_100d,

        COALESCE(SUM(
            CASE WHEN rli.days_to_refund <= 100 + %(buf)s
                 THEN rli.qty_returned END
        ), 0)::NUMERIC
            / NULLIF(SUM(oli.quantity), 0)                 AS return_rate_100d,

        (MAX(oli.order_date) + (100 + %(buf)s)) < CURRENT_DATE AS is_100d_closed,

        MAX(oli.order_date)                                 AS last_order_date,
        now()                                               AS refreshed_at

    FROM order_line_items oli
    JOIN sku_config sc ON sc.sku = oli.sku
    LEFT JOIN refund_line_items rli ON rli.order_line_item_id = oli.id

    -- Never include the current calendar month (data still arriving)
    WHERE DATE_TRUNC('month', oli.order_date) < DATE_TRUNC('month', CURRENT_DATE)

    GROUP BY oli.sku, DATE_TRUNC('month', oli.order_date), sc.return_window_days

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
