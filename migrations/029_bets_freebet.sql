-- Funding metadata only; stake and unit-profit conventions stay unchanged.
ALTER TABLE bets ADD COLUMN is_freebet BOOLEAN NOT NULL DEFAULT false;
COMMENT ON COLUMN bets.is_freebet IS 'Fonbet marks this single bet as a freebet; stake/profit remain normalized units.';
