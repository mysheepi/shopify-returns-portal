-- Replace windowed monetary columns (30d/100d) with a single cumulative refund column.
-- Goodwill refunds have no trial window, so the monetary rate is total-to-date.
ALTER TABLE sku_monthly_stats
    DROP COLUMN IF EXISTS refunded_30d_amount,
    DROP COLUMN IF EXISTS refund_rate_30d_monetary,
    DROP COLUMN IF EXISTS refunded_100d_amount,
    DROP COLUMN IF EXISTS refund_rate_100d_monetary,
    ADD COLUMN IF NOT EXISTS total_refunded_amount NUMERIC(12,2) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS refund_rate_monetary  NUMERIC(8,5);
