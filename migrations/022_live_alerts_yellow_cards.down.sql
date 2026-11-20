-- Downgrade requires removing or archiving any yellow_cards rows first.
ALTER TABLE live_alerts_sent DROP CONSTRAINT live_alerts_sent_category_check;
ALTER TABLE live_alerts_sent ADD CONSTRAINT live_alerts_sent_category_check
    CHECK (category IN ('stats','half','half_attack','favorite','half_shots','half_pressure'));
