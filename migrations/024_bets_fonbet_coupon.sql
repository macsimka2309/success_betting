-- Stable external identity; NULL remains valid for manually entered bets.
ALTER TABLE bets ADD COLUMN fonbet_coupon_id TEXT;
CREATE UNIQUE INDEX idx_bets_fonbet_coupon ON bets(fonbet_coupon_id) WHERE fonbet_coupon_id IS NOT NULL;
