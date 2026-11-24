ALTER TABLE bets ALTER COLUMN fixture_id DROP NOT NULL;
ALTER TABLE bets ADD COLUMN manual_home_name TEXT, ADD COLUMN manual_away_name TEXT;
ALTER TABLE bets ADD CONSTRAINT bets_manual_match_check CHECK (
    fixture_id IS NOT NULL OR (
        alert_id IS NULL AND status <> 'open'
        AND NULLIF(btrim(manual_home_name), '') IS NOT NULL
        AND NULLIF(btrim(manual_away_name), '') IS NOT NULL
    )
);
