CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS orders (
    id                  UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    shopify_order_id    BIGINT UNIQUE NOT NULL,
    order_date          DATE NOT NULL,
    shopify_updated_at  TIMESTAMPTZ NOT NULL,
    synced_at           TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_orders_order_date ON orders(order_date);

CREATE TABLE IF NOT EXISTS order_line_items (
    id                    UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    shopify_line_item_id  BIGINT UNIQUE NOT NULL,
    order_id              UUID NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    sku                   VARCHAR(120) NOT NULL,
    quantity              INT NOT NULL CHECK (quantity > 0),
    order_date            DATE NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_oli_sku       ON order_line_items(sku);
CREATE INDEX IF NOT EXISTS idx_oli_order_date ON order_line_items(order_date);

CREATE TABLE IF NOT EXISTS refund_line_items (
    id                           UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    shopify_refund_id            BIGINT NOT NULL,
    shopify_refund_line_item_id  BIGINT UNIQUE NOT NULL,
    order_line_item_id           UUID NOT NULL REFERENCES order_line_items(id) ON DELETE CASCADE,
    sku                          VARCHAR(120) NOT NULL,
    qty_returned                 INT NOT NULL CHECK (qty_returned > 0),
    order_date                   DATE NOT NULL,
    refund_date                  DATE NOT NULL,
    days_to_refund               INT NOT NULL CHECK (days_to_refund >= 0)
);

CREATE INDEX IF NOT EXISTS idx_rli_sku        ON refund_line_items(sku);
CREATE INDEX IF NOT EXISTS idx_rli_order_date ON refund_line_items(order_date);

CREATE TABLE IF NOT EXISTS sku_config (
    sku                 VARCHAR(120) PRIMARY KEY,
    return_window_days  INT NOT NULL CHECK (return_window_days IN (30, 100))
);

CREATE TABLE IF NOT EXISTS settings (
    key    VARCHAR(100) PRIMARY KEY,
    value  VARCHAR(255) NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_log (
    id              UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    sync_type       VARCHAR(20) NOT NULL CHECK (sync_type IN ('full', 'incremental')),
    status          VARCHAR(20) NOT NULL DEFAULT 'running'
                        CHECK (status IN ('running', 'complete', 'error')),
    cursor          TEXT,
    watermark       TIMESTAMPTZ,
    orders_synced   INT DEFAULT 0,
    refunds_synced  INT DEFAULT 0,
    started_at      TIMESTAMPTZ DEFAULT now(),
    completed_at    TIMESTAMPTZ,
    error_message   TEXT
);

CREATE TABLE IF NOT EXISTS sku_monthly_stats (
    sku                VARCHAR(120) NOT NULL,
    order_month        DATE NOT NULL,
    return_window_days INT NOT NULL,
    total_ordered      INT NOT NULL,
    returned_30d       INT NOT NULL DEFAULT 0,
    return_rate_30d    NUMERIC(8,5),
    is_30d_closed      BOOLEAN NOT NULL,
    returned_100d      INT NOT NULL DEFAULT 0,
    return_rate_100d   NUMERIC(8,5),
    is_100d_closed     BOOLEAN NOT NULL,
    last_order_date    DATE NOT NULL,
    refreshed_at       TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (sku, order_month)
);

-- ── SKU config seed ──────────────────────────────────────────────────────────
-- 100-day return window SKUs (effective window = 100 + buffer)
INSERT INTO sku_config (sku, return_window_days) VALUES
    ('MS.HOME.60x40.A1', 100),
    ('MS.HOME.80x40.A1', 100),
    ('MS.HOME.COOL.80x40', 100),
    ('MS.KIDS.WHITE', 100),
    ('MS.PILW.TRVL.WHIT.40x35', 100)
ON CONFLICT (sku) DO NOTHING;

-- 30-day return window SKUs (effective window = 30 + buffer)
INSERT INTO sku_config (sku, return_window_days) VALUES
    ('MS.DUVT.COOL.135x200', 30),
    ('MS.DUVT.COOL.155x220', 30),
    ('MS.DUVT.WHIT.135x200', 30),
    ('MS.DUVT.WHIT.155x220', 30),
    ('MS.DUVT.WHIT.200x200', 30),
    ('MS.DUVT.WHIT.220x240', 30),
    ('MS.COVR.DUVT.BEIG.135x200', 30),
    ('MS.COVR.DUVT.BEIG.155x220', 30),
    ('MS.COVR.DUVT.BEIG.200x200', 30),
    ('MS.COVR.DUVT.BEIG.240x220', 30),
    ('MS.COVR.DUVT.BEIS.135x200', 30),
    ('MS.COVR.DUVT.BEIS.155x220', 30),
    ('MS.COVR.DUVT.BEIS.200x200', 30),
    ('MS.COVR.DUVT.BEIS.240x220', 30),
    ('MS.COVR.DUVT.GREY.135x200', 30),
    ('MS.COVR.DUVT.GREY.155x220', 30),
    ('MS.COVR.DUVT.GREY.200x200', 30),
    ('MS.COVR.DUVT.GREY.240x220', 30),
    ('MS.COVR.DUVT.GRYS.135x200', 30),
    ('MS.COVR.DUVT.GRYS.155x220', 30),
    ('MS.COVR.DUVT.GRYS.200x200', 30),
    ('MS.COVR.DUVT.GRYS.240x220', 30),
    ('MS.COVR.DUVT.WHIT.135x200', 30),
    ('MS.COVR.DUVT.WHIT.155x220', 30),
    ('MS.COVR.DUVT.WHIT.200x200', 30),
    ('MS.COVR.DUVT.WHIT.240x220', 30),
    ('MS.COVR.DUVT.WHTS.135x200', 30),
    ('MS.COVR.DUVT.WHTS.155x220', 30),
    ('MS.COVR.DUVT.WHTS.200x200', 30),
    ('MS.COVR.DUVT.WHTS.240x220', 30),
    ('MS.BAGS.FILL', 30),
    ('MS.BSHT.FITT.BEIG.100x200', 30),
    ('MS.BSHT.FITT.BEIG.140x200', 30),
    ('MS.BSHT.FITT.BEIG.160x200', 30),
    ('MS.BSHT.FITT.BEIG.180x200', 30),
    ('MS.BSHT.FITT.BEIS.100x200', 30),
    ('MS.BSHT.FITT.BEIS.140x200', 30),
    ('MS.BSHT.FITT.BEIS.160x200', 30),
    ('MS.BSHT.FITT.BEIS.180x200', 30),
    ('MS.BSHT.FITT.GREY.100x200', 30),
    ('MS.BSHT.FITT.GREY.140x200', 30),
    ('MS.BSHT.FITT.GREY.160x200', 30),
    ('MS.BSHT.FITT.GREY.180x200', 30),
    ('MS.BSHT.FITT.GRYS.100x200', 30),
    ('MS.BSHT.FITT.GRYS.140x200', 30),
    ('MS.BSHT.FITT.GRYS.160x200', 30),
    ('MS.BSHT.FITT.GRYS.180x200', 30),
    ('MS.BSHT.FITT.WHIT.100x200', 30),
    ('MS.BSHT.FITT.WHIT.140x200', 30),
    ('MS.BSHT.FITT.WHIT.160x200', 30),
    ('MS.BSHT.FITT.WHIT.180x200', 30),
    ('MS.BSHT.FITT.WHTS.100x200', 30),
    ('MS.BSHT.FITT.WHTS.140x200', 30),
    ('MS.BSHT.FITT.WHTS.160x200', 30),
    ('MS.BSHT.FITT.WHTS.180x200', 30),
    ('MS.GELCOMPRESS', 30),
    ('MS.CASE.HOME.BEIGE.60.40.REN', 30),
    ('MS.CASE.HOME.BEIGE.80.40.REN', 30),
    ('MS.CASE.HOME.DARKBLUE.60.40.REN', 30),
    ('MS.CASE.HOME.DARKBLUE.80.40.REN', 30),
    ('MS.CASE.HOME.DARKGREY.60.40.REN', 30),
    ('MS.CASE.HOME.DARKGREY.80.40.REN', 30),
    ('MS.CASE.HOME.WEISS.60.40', 30),
    ('MS.CASE.HOME.WEISS.80.40', 30),
    ('MS.CASE.HOME.BEIG.60x40', 30),
    ('MS.CASE.HOME.BEIG.80x40', 30),
    ('MS.CASE.HOME.BLUE.60x40', 30),
    ('MS.CASE.HOME.BLUE.80x40', 30),
    ('MS.CASE.HOME.GREY.60x40', 30),
    ('MS.CASE.HOME.GREY.80x40', 30),
    ('MS.CASE.HOME.NAVY.60x40', 30),
    ('MS.CASE.HOME.NAVY.80x40', 30),
    ('MS.CASE.HOME.REDD.60x40', 30),
    ('MS.CASE.HOME.REDD.80x40', 30),
    ('MS.CASE.HOME.WHIT.60x40', 30),
    ('MS.CASE.HOME.WHIT.80x40', 30),
    ('MS.HEADPART.H1.60', 30),
    ('MS.HEADPART.H1.80', 30),
    ('MS.HEADPART.H2.60', 30),
    ('MS.HEADPART.H2.80', 30),
    ('MS.HEADPART.KAP.60', 30),
    ('MS.HEADPART.KAP.80', 30),
    ('MS.HEADPART.STONEPINE.60', 30),
    ('MS.HEADPART.STONEPINE.80', 30),
    ('MS.HEADPART.WOOL.60', 30),
    ('MS.HEADPART.WOOL.80', 30),
    ('MS.NECKROLL.H1.60', 30),
    ('MS.NECKROLL.H1.80', 30),
    ('MS.NECKROLL.H2.60', 30),
    ('MS.NECKROLL.H2.80', 30),
    ('MS.NECKROLL.KAP.60', 30),
    ('MS.NECKROLL.KAP.80', 30),
    ('MS.NECKROLL.STONEPINE.60', 30),
    ('MS.NECKROLL.STONEPINE.80', 30),
    ('MS.NECKROLL.WOOL.60', 30),
    ('MS.NECKROLL.WOOL.80', 30),
    ('MS.FCOVER.40x35', 30),
    ('MS.FCOVER.60x40', 30),
    ('MS.FCOVER.80x40', 30),
    ('MS.HEAD.40x35', 30),
    ('MS.HEAD.60x40', 30),
    ('MS.HEAD.80x40', 30),
    ('MS.NECK.40x35', 30),
    ('MS.NECK.60x40', 30),
    ('MS.NECK.80x40', 30),
    ('MS.CASE.KIDS.CARS.30.40', 30),
    ('MS.CASE.KIDS.STARS.30.40', 30),
    ('MS.CASE.KIDS.STARS.MOON.30.40', 30),
    ('MS.COAT.VLVT.BLCK.102', 30),
    ('MS.COAT.VLVT.NAVY.102', 30),
    ('MS.FILLING.KAPOK', 30),
    ('MS.FILLING.MEDLINE', 30),
    ('MS.FILLING.SOFT', 30),
    ('MS.FILLING.STONEPINE', 30),
    ('MS.FILLING.WOOL', 30),
    ('MS.CSHN.BACK.BLCK', 30),
    ('MS.CSHN.SEAT.BLCK', 30),
    ('MS.CASE.SILK.GREY.60x40', 30),
    ('MS.CASE.SILK.GREY.80x40', 30),
    ('MS.CASE.SILK.IVRY.60x40', 30),
    ('MS.CASE.SILK.IVRY.80x40', 30),
    ('MS.CASE.SILK.LILA.60x40', 30),
    ('MS.CASE.SILK.LILA.80x40', 30),
    ('MS.CASE.SILK.NAVY.60x40', 30),
    ('MS.CASE.SILK.NAVY.80x40', 30),
    ('MS.CASE.SILK.PINK.60x40', 30),
    ('MS.CASE.SILK.PINK.80x40', 30),
    ('MS.CASE.SILK.PIST.60x40', 30),
    ('MS.CASE.SILK.PIST.80x40', 30),
    ('MS.CASE.SILK.ROSE.60x40', 30),
    ('MS.CASE.SILK.ROSE.80x40', 30),
    ('MS.MASK.GREY', 30),
    ('MS.MASK.IVRY', 30),
    ('MS.MASK.NAVY', 30),
    ('MS.MASK.ROSE', 30),
    ('MS.SPRAY.LAVENDEL', 30),
    ('MS.SPRAY.LEMEUKA', 30),
    ('MS.SPRAY.LEMROSE', 30),
    ('MS.SPRAY.ORANGE', 30),
    ('MS.SPRAY.STONEPINE', 30),
    ('MS.BAGS.TRVL.BLUE.40x35', 30),
    ('MS.CASE.TRAVEL.BLUE.MOTTLED.35.40', 30),
    ('MS.CASE.TRAVEL.GRAY.MOTTLED.35.40', 30),
    ('MS.CASE.TRAVEL.GREEN.MOTTLED.35.40', 30),
    ('MS.TRAVEL.WHITE.H1', 30),
    ('MS.CASE.TRVL.BEIG.40x35', 30),
    ('MS.CASE.TRVL.BLUE.40x35', 30),
    ('MS.CASE.TRVL.GREY.40x35', 30),
    ('MS.CASE.TRVL.NAVY.40x35', 30),
    ('MS.CASE.TRVL.REDD.40x35', 30)
ON CONFLICT (sku) DO NOTHING;

-- Default settings
INSERT INTO settings (key, value) VALUES
    ('return_buffer_days', '10')
ON CONFLICT (key) DO NOTHING;
