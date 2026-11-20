-- Additive queue/context layer. Existing snapshots and registry IDs remain unchanged.
CREATE TABLE live_alert_dispatch_config (
 singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK(singleton), enabled BOOLEAN NOT NULL DEFAULT false
);
INSERT INTO live_alert_dispatch_config(singleton) VALUES(true);
CREATE TABLE live_alert_context (
 source TEXT NOT NULL CHECK(source IN ('api_football','fonbet')), source_event_id TEXT NOT NULL,
 observed_at TIMESTAMPTZ NOT NULL, status TEXT NOT NULL, elapsed SMALLINT,
 present BOOLEAN NOT NULL DEFAULT true, format_tags JSONB NOT NULL DEFAULT '[]',
 PRIMARY KEY(source,source_event_id)
);
CREATE TABLE live_alert_matches (
 alert_match_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 merged_into BIGINT REFERENCES live_alert_matches(alert_match_id),
 sent_at TIMESTAMPTZ, uncertain_at TIMESTAMPTZ,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), CHECK(merged_into IS DISTINCT FROM alert_match_id)
);
CREATE TABLE live_alert_aliases (
 source TEXT NOT NULL CHECK(source IN ('api_football','fonbet')), source_event_id TEXT NOT NULL,
 alert_match_id BIGINT NOT NULL REFERENCES live_alert_matches(alert_match_id),
 PRIMARY KEY(source,source_event_id)
);
CREATE INDEX live_alert_alias_match_idx ON live_alert_aliases(alert_match_id);
CREATE TABLE live_alert_outbox (
 alert_match_id BIGINT PRIMARY KEY REFERENCES live_alert_matches(alert_match_id),
 alert_id INTEGER NOT NULL UNIQUE DEFAULT nextval('live_alerts_sent_alert_id_seq'),
 state TEXT NOT NULL CHECK(state IN ('pending','sending','sent','retry','expired','uncertain','identity_ambiguous')),
 payload JSONB NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, reason TEXT,
 telegram_message_id BIGINT, next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 lease_until TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 sent_at TIMESTAMPTZ, updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX live_alert_outbox_pending_idx ON live_alert_outbox(state,next_attempt_at);
-- One advisory key serializes identity changes and delivery, including the mapping writer.
CREATE FUNCTION live_alert_identity(p_source TEXT, p_id TEXT) RETURNS BIGINT LANGUAGE plpgsql AS $$
DECLARE m BIGINT;
BEGIN
 PERFORM pg_advisory_xact_lock(27001);
 SELECT alert_match_id INTO m FROM live_alert_aliases WHERE source=p_source AND source_event_id=p_id;
 IF m IS NULL THEN
  INSERT INTO live_alert_matches DEFAULT VALUES RETURNING alert_match_id INTO m;
  INSERT INTO live_alert_aliases VALUES(p_source,p_id,m);
 END IF;
 RETURN m;
END $$;
CREATE FUNCTION live_alert_link(p_fonbet TEXT,p_fixture INTEGER) RETURNS BIGINT LANGUAGE plpgsql AS $$
DECLARE a BIGINT; b BIGINT; winner BIGINT; loser BIGINT;
BEGIN
 PERFORM pg_advisory_xact_lock(27001);
 a:=live_alert_identity('api_football',p_fixture::text);
 b:=live_alert_identity('fonbet',p_fonbet);
 IF a=b THEN RETURN a; END IF;
 winner:=least(a,b); loser:=greatest(a,b);
 UPDATE live_alert_matches w SET
  sent_at=least(w.sent_at,l.sent_at), uncertain_at=least(w.uncertain_at,l.uncertain_at)
 FROM live_alert_matches l WHERE w.alert_match_id=winner AND l.alert_match_id=loser;
 UPDATE live_alert_aliases SET alert_match_id=winner WHERE alert_match_id=loser;
 UPDATE live_alert_matches SET merged_into=winner WHERE alert_match_id=loser;
 -- Historical sent/uncertain rows remain auditable under the merged identity.
 UPDATE live_alert_outbox SET state='expired',reason='identity_merged',updated_at=now()
 WHERE alert_match_id=loser AND state IN ('pending','retry','identity_ambiguous');
 UPDATE live_alert_outbox SET state='expired',reason='merged_delivery_exists',updated_at=now()
 WHERE alert_match_id=winner AND state IN ('pending','retry','identity_ambiguous') AND EXISTS(
  SELECT 1 FROM live_alert_matches WHERE alert_match_id=winner AND (sent_at IS NOT NULL OR uncertain_at IS NOT NULL));
 RETURN winner;
END $$;
-- Mapping changes use the same lock as sender, even outside the Python reconciler.
CREATE FUNCTION live_alert_mapping_lock() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 PERFORM pg_advisory_xact_lock(27001);
 IF NEW.mapping_status='confirmed' THEN PERFORM live_alert_link(NEW.fonbet_event_id,NEW.fixture_id); END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER live_alert_mapping_serialized BEFORE INSERT OR UPDATE OF mapping_status,fixture_id
 ON fonbet_live_match_statistics_75 FOR EACH ROW EXECUTE FUNCTION live_alert_mapping_lock();
-- Import all old categories: one delivered match blocks any new source/category replay.
DO $$ DECLARE r RECORD; m BIGINT; BEGIN
 FOR r IN SELECT fixture_id,min(sent_at) AS sent_at FROM live_alerts_sent GROUP BY fixture_id LOOP
  m:=live_alert_identity('api_football',r.fixture_id::text);
  UPDATE live_alert_matches SET sent_at=r.sent_at WHERE alert_match_id=m;
 END LOOP;
 FOR r IN SELECT fonbet_event_id,fixture_id FROM fonbet_live_match_statistics_75 WHERE mapping_status='confirmed' LOOP
  PERFORM live_alert_link(r.fonbet_event_id,r.fixture_id);
 END LOOP;
END $$;
