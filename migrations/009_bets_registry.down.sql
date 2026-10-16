-- Откат 009: удаляет реестр ставок и диалоги вместе с данными. Выполнять только по
-- явному решению владельца: ставки восстановить нечем.
DROP TABLE IF EXISTS bot_dialogs;
DROP TABLE IF EXISTS bets;
ALTER TABLE live_alerts_sent DROP COLUMN IF EXISTS kickoff_at;
ALTER TABLE live_alerts_sent DROP CONSTRAINT IF EXISTS live_alerts_sent_alert_id_key;
ALTER TABLE live_alerts_sent DROP COLUMN IF EXISTS alert_id;
