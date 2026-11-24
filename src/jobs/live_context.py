"""Current live context independent of retained 65–75 snapshots."""
from datetime import datetime, timezone
from psycopg.types.json import Jsonb
from tools.fonbet_live.match_clock import api_period

UPSERT = """INSERT INTO live_alert_context(source,source_event_id,observed_at,status,elapsed,present,format_tags)
 VALUES(%s,%s,%s,%s,%s,true,%s) ON CONFLICT(source,source_event_id) DO UPDATE SET
 observed_at=EXCLUDED.observed_at,status=EXCLUDED.status,elapsed=EXCLUDED.elapsed,present=true,format_tags=EXCLUDED.format_tags
 WHERE EXCLUDED.observed_at >= live_alert_context.observed_at"""


def available(conn):
    return conn is not None and conn.execute("SELECT to_regclass('live_alert_context')").fetchone()[0] is not None


def enabled(conn):
    return available(conn) and conn.execute('SELECT enabled FROM live_alert_dispatch_config WHERE singleton').fetchone()[0]


def record_api_context(conn, items, receipt):
    if receipt is None or not available(conn):
        return
    observed = datetime.fromtimestamp(receipt.observed_at, timezone.utc)
    ids = []
    with conn.transaction():
        for item in items:
            fixture = item.get('fixture') or {}
            if not fixture.get('id'):
                continue
            event_id = str(fixture['id']); ids.append(event_id)
            status = fixture.get('status') or {}
            conn.execute(UPSERT, ('api_football', event_id, observed, status.get('short') or 'unknown', status.get('elapsed'), Jsonb([])))
            # Old archives without request start remain ineligible for reminder selection.
            requested=getattr(receipt,'requested_at',None)
            if conn.execute("SELECT EXISTS(SELECT 1 FROM information_schema.columns WHERE table_name='live_alert_context' AND column_name='poll_id')").fetchone()[0]:
                conn.execute("""UPDATE live_alert_context SET poll_id=%s,requested_at=%s,completed_at=%s,
                 period=%s,added_minute=%s WHERE source='api_football' AND source_event_id=%s AND observed_at=%s""",
                 (str(getattr(receipt,'observation_id',None) or receipt.observed_at),
                  datetime.fromtimestamp(requested,timezone.utc) if requested is not None else None,
                  observed,api_period(status.get('short')),status.get('extra'),event_id,observed))
        conn.execute("""UPDATE live_alert_context SET present=false,status='absent',observed_at=%s
         WHERE source='api_football' AND NOT(source_event_id=ANY(%s)) AND observed_at<%s""", (observed,ids,observed))
