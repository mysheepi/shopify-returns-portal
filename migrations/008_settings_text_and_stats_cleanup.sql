-- The return_rate_thresholds JSON lives in settings.value; with per-SKU
-- overrides it easily exceeds VARCHAR(255), which made saving settings fail.
ALTER TABLE settings ALTER COLUMN value TYPE TEXT;

-- Migration 007 removed SKUs from sku_config, but /api/returns reads
-- sku_monthly_stats directly, so rows for removed SKUs lingered forever
-- (aggregation only refreshes SKUs still present in sku_config).
DELETE FROM sku_monthly_stats
WHERE sku NOT IN (SELECT sku FROM sku_config);
