-- Disable new dispatch and synchronize sent API messages to the legacy registry before rollback.
DROP TRIGGER live_alert_mapping_serialized ON fonbet_live_match_statistics_75;
DROP FUNCTION live_alert_mapping_lock();
DROP FUNCTION live_alert_link(TEXT,INTEGER);
DROP FUNCTION live_alert_identity(TEXT,TEXT);
DROP TABLE live_alert_outbox;
DROP TABLE live_alert_aliases;
DROP TABLE live_alert_matches;
DROP TABLE live_alert_context;
DROP TABLE live_alert_dispatch_config;
