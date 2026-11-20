CREATE FUNCTION live_signal_merge() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE earliest INTEGER; first_time TIMESTAMPTZ; blocked BOOLEAN;
BEGIN
 IF NEW.merged_into IS NULL OR NEW.merged_into IS NOT DISTINCT FROM OLD.merged_into THEN RETURN NEW; END IF;
 PERFORM pg_advisory_xact_lock(27001);
 UPDATE live_signal_tracking SET alert_match_id=NEW.merged_into WHERE alert_match_id=NEW.alert_match_id;
 SELECT signal_id,first_sent_at INTO earliest,first_time FROM live_signal_tracking
  WHERE alert_match_id=NEW.merged_into ORDER BY first_sent_at,signal_id LIMIT 1;
 IF earliest IS NULL THEN RETURN NEW; END IF;
 SELECT EXISTS(SELECT 1 FROM live_signal_reminders r JOIN live_signal_tracking t USING(signal_id)
  WHERE t.alert_match_id=NEW.merged_into AND r.state IN ('sent','uncertain','sending')) INTO blocked;
 -- An older, untracked legacy send must not be backfilled by a newer alias.
 IF EXISTS(SELECT 1 FROM live_alert_matches WHERE alert_match_id=NEW.merged_into AND sent_at<first_time) THEN
  earliest:=NULL;
 END IF;
 UPDATE live_signal_reminders r SET state='cancelled',reason='identity_merged',updated_at=now()
 FROM live_signal_tracking t WHERE t.signal_id=r.signal_id AND t.alert_match_id=NEW.merged_into
 AND r.state IN ('waiting','retry') AND (blocked OR earliest IS NULL OR r.signal_id<>earliest);
 INSERT INTO live_signal_check_attempts(signal_id,attempted_at,stage,reason)
 SELECT signal_id,now(),'identity_merge','superseded_by_first_signal' FROM live_signal_tracking
 WHERE alert_match_id=NEW.merged_into AND (earliest IS NULL OR signal_id<>earliest);
 UPDATE live_signal_tracking SET goals_enabled=false,status='void',incomplete_reason='identity_superseded'
 WHERE alert_match_id=NEW.merged_into AND (earliest IS NULL OR signal_id<>earliest);
 RETURN NEW;
END $$;
CREATE TRIGGER live_signal_canonical_merge AFTER UPDATE OF merged_into ON live_alert_matches
 FOR EACH ROW EXECUTE FUNCTION live_signal_merge();
