-- Unit price on each fulfilled line item (for revenue denominator)
ALTER TABLE order_line_items
    ADD COLUMN IF NOT EXISTS unit_price NUMERIC(10,2) NOT NULL DEFAULT 0;

-- Monetary amount refunded per refund line item (for refund numerator)
ALTER TABLE refund_line_items
    ADD COLUMN IF NOT EXISTS refund_subtotal NUMERIC(10,2) NOT NULL DEFAULT 0;

-- Monetary refund stats on the monthly summary
ALTER TABLE sku_monthly_stats
    ADD COLUMN IF NOT EXISTS total_revenue             NUMERIC(12,2) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS refunded_30d_amount       NUMERIC(12,2) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS refund_rate_30d_monetary  NUMERIC(8,5),
    ADD COLUMN IF NOT EXISTS refunded_100d_amount      NUMERIC(12,2) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS refund_rate_100d_monetary NUMERIC(8,5);
