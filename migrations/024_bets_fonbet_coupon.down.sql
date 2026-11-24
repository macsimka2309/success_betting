-- Explicit rollback only: removes external coupon identities from existing bets.
DROP INDEX idx_bets_fonbet_coupon;
ALTER TABLE bets DROP COLUMN fonbet_coupon_id;
