ALTER TABLE live_alert_context
 ADD COLUMN poll_id TEXT,
 ADD COLUMN requested_at TIMESTAMPTZ,
 ADD COLUMN completed_at TIMESTAMPTZ,
 ADD COLUMN period TEXT DEFAULT 'unknown',
 ADD COLUMN added_minute SMALLINT CHECK (added_minute >= 0);
