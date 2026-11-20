"""Enrollment of new confirmed signals; no network calls or historical backfill."""
from dataclasses import dataclass
from datetime import datetime, timezone

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


@dataclass(frozen=True)
class DeliveryReceipt:
    message_id: int
    sent_at: datetime | None = None


def configure(conn, *, tracking: bool, reminders: bool, now: datetime):
    """Explicit operator action; preserve the initial activation boundary forever."""
    conn.execute("""UPDATE live_signal_config SET tracking_enabled=%s,reminders_enabled=%s,
      activated_at=CASE WHEN %s OR %s THEN COALESCE(activated_at,%s) ELSE activated_at END
      WHERE singleton""", (tracking,reminders,tracking,reminders,now))


def enrollment(conn, delivery_started_at):
    # Supports a staged code rollout before this additive migration is installed.
    if conn.execute("SELECT to_regclass('live_signal_config')").fetchone()[0] is None:
        return None
    with conn.cursor(row_factory=dict_row) as cur:
        config = cur.execute('SELECT * FROM live_signal_config WHERE singleton').fetchone()
    if not config or not (config['tracking_enabled'] or config['reminders_enabled']):
        return None
    if config['activated_at'] is None or delivery_started_at < config['activated_at']:
        return None
    return config


def record_confirmed(conn, *, signal_id, match_id, receipt, started_at, confirmed_at, context, config):
    """Caller owns the transaction that marks the initial outbox sent."""
    if config is None:
        return
    sent_at = receipt.sent_at or started_at
    precision = 'telegram_second' if receipt.sent_at is not None else 'delivery_interval'
    if config['version'] < 2:
        return
    source, event_id = context['source'], str(context['event_id'])
    track_goals = source == 'fonbet' and config['tracking_enabled']
    inserted = conn.execute("""INSERT INTO live_signal_tracking
      (signal_id,alert_match_id,feature_version,first_message_id,first_sent_at,confirmed_at,
       delivery_started_at,time_precision,first_context,goals_enabled,first_source,
       first_source_event_id,live_phase,next_attempt_at)
      VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(signal_id) DO NOTHING RETURNING signal_id""",
      (signal_id,match_id,config['version'],receipt.message_id,sent_at,confirmed_at,started_at,
       precision,Jsonb(context),track_goals,source,event_id,
       'ordinary' if track_goals else None,confirmed_at if track_goals else None)).fetchone()
    if inserted and config['reminders_enabled']:
        conn.execute('INSERT INTO live_signal_reminders(signal_id,next_attempt_at) VALUES(%s,%s) ON CONFLICT DO NOTHING',(signal_id,confirmed_at))
