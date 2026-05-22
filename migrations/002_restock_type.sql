-- Add restock_type to refund_line_items (existing rows default to 'return'
-- to preserve backward-compatibility; re-sync will correct historical data)
ALTER TABLE refund_line_items
    ADD COLUMN IF NOT EXISTS restock_type VARCHAR(20) NOT NULL DEFAULT 'return';

-- Physical-return columns on sku_monthly_stats
ALTER TABLE sku_monthly_stats
    ADD COLUMN IF NOT EXISTS returned_30d_physical     INT         NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS return_rate_30d_physical  NUMERIC(8,5),
    ADD COLUMN IF NOT EXISTS returned_100d_physical    INT         NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS return_rate_100d_physical NUMERIC(8,5);
