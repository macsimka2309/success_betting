"""Import rendered Fonbet history. Stdlib only; no credentials or .env access."""
import argparse
import json
import re
import subprocess
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

MSK = ZoneInfo('Europe/Moscow')
SINGLE_TYPES = (['Одинар'], ['Одинар', 'Фрибет'])


def timestamp(text):
    return datetime.strptime(text.strip(), '%d.%m.%Y %H:%M:%S').replace(tzinfo=MSK).isoformat()


def is_nightly_total_over(raw):
    if not isinstance(raw, dict):
        return False
    label, legs = raw.get('type'), raw.get('legs')
    if not isinstance(label, str) or label.split() not in SINGLE_TYPES:
        return False
    if not isinstance(legs, list) or len(legs) != 1:
        return False
    leg = legs[0]
    if not isinstance(leg, list) or len(leg) < 3:
        return False
    prediction = leg[2]
    # A leading sign denotes the match total. Validate the line separately
    # so an unreadable line on a recognized over remains a reported error.
    return isinstance(prediction, str) and prediction.lstrip().startswith('>')


def parse_coupon(raw):
    coupon_id = raw.get('id')
    if not isinstance(coupon_id, str) or not coupon_id.strip():
        raise ValueError('неизвестный номер купона')
    labels = raw['type'].split()
    if labels not in SINGLE_TYPES or len(raw['legs']) != 1:
        raise ValueError('поддерживаются только одиночные ставки')
    is_freebet = labels == ['Одинар', 'Фрибет']
    kickoff, event, prediction, odds, score, result = raw['legs'][0]
    match = re.fullmatch(r'LIVE\s+\d+:\d+\s+(.+?)\s+–\s+(.+)', event)
    if not match:
        raise ValueError('неизвестный формат события')
    home, away = match.groups()
    market_match = re.fullmatch(r'(?:(.+?)\s+)?([<>])\s*(\d+(?:[.,]\d+)?)', prediction.strip())
    if not market_match:
        raise ValueError('неизвестный рынок')
    team, direction, line = market_match.groups()
    market = 'TB' if direction == '>' else 'TM'
    if team:
        if direction != '>' or team not in (home, away):
            raise ValueError('неизвестный индивидуальный тотал')
        market = 'ITB1' if team == home else 'ITB2'
    line, odds = Decimal(line.replace(',', '.')), Decimal(odds.replace(',', '.'))
    if not odds.is_finite() or line <= 0 or odds <= 1:
        raise ValueError('неверная линия или коэффициент')
    # Quarter handicaps need separate half-win/half-loss handling: never guess.
    if line % Decimal('.5'):
        raise ValueError('четвертная линия требует ручной проверки')
    statuses = {'Выигрышный прогноз': 'won', 'Проигрышный прогноз': 'lost',
                'Выигрыш': 'won', 'Проигрыш': 'lost',
                'Возврат': 'push', 'Не рассчитан': 'open', 'Не рассчитана': 'open'}
    result=result.strip()
    if result not in statuses:
        raise ValueError('неизвестный статус: ' + result)
    status = statuses[result]
    if len(raw['bar']) != 5:
        raise ValueError('неизвестный формат времени купона')
    created = timestamp(raw['bar'][3])
    settled = None
    goals_home = goals_away = None
    if status != 'open':
        settled = timestamp(re.sub(r'^Рассчитана\s+', '', raw['bar'][4]))
        goals = re.fullmatch(r'(\d+):(\d+)', score)
        if not goals:
            raise ValueError('неизвестный итоговый счёт')
        goals_home, goals_away = map(int, goals.groups())
    return dict(coupon=coupon_id, is_freebet=is_freebet, home=home, away=away, market=market,
                line=str(line), odds=str(odds), created=created,
                kickoff=datetime.strptime(kickoff, '%d.%m.%Y %H:%M').replace(tzinfo=MSK).isoformat(),
                status=status, settled=settled, goals_home=goals_home, goals_away=goals_away,
                profit=str(odds-1) if status == 'won' else '-1' if status == 'lost' else '0' if status == 'push' else None)


def psql(sql):
    cmd = ['sudo', '-n', 'docker', 'exec', '-i', 'football-postgres', 'sh', '-c',
           'exec psql -X -qAt -U "$POSTGRES_USER" -d football -v ON_ERROR_STOP=1 -f -']
    return subprocess.run(cmd, input=sql, text=True, capture_output=True, check=True).stdout


SQL = r"""
BEGIN;
SELECT pg_advisory_xact_lock(70110624);
LOCK TABLE bets IN SHARE ROW EXCLUSIVE MODE;
CREATE TEMP TABLE sync_result(action text, coupon text, detail text) ON COMMIT DROP;
DO $sync$
DECLARE r jsonb; b bets%ROWTYPE; n int; ids integer[]; fid integer;
        t timestamptz; same_day boolean;
BEGIN
FOR r IN SELECT value FROM jsonb_array_elements(__DATA__::jsonb) LOOP
 t := (r->>'created')::timestamptz;
 same_day := (t AT TIME ZONE 'Europe/Moscow')::date = __DAY__::date;
 SELECT * INTO b FROM bets WHERE fonbet_coupon_id=r->>'coupon';
 IF NOT FOUND THEN
   SELECT count(*) INTO n FROM bets WHERE abs(extract(epoch FROM created_at-t)) <= 2
     AND market=r->>'market' AND line=(r->>'line')::numeric AND odds=(r->>'odds')::numeric;
   IF n > 1 THEN
     INSERT INTO sync_result VALUES('skipped',r->>'coupon','несколько совпадений реестра'); CONTINUE;
   END IF;
   IF n = 1 THEN
     SELECT * INTO b FROM bets WHERE abs(extract(epoch FROM created_at-t)) <= 2
       AND market=r->>'market' AND line=(r->>'line')::numeric AND odds=(r->>'odds')::numeric;
     IF (
       (lower(b.manual_home_name)=lower(r->>'home') AND lower(b.manual_away_name)=lower(r->>'away'))
       OR EXISTS(SELECT 1 FROM live_alerts_sent a WHERE a.fixture_id=b.fixture_id
           AND lower(a.home_name)=lower(r->>'home') AND lower(a.away_name)=lower(r->>'away'))
     ) IS NOT TRUE THEN
       INSERT INTO sync_result VALUES('skipped',r->>'coupon','время совпало, названия не подтверждены'); CONTINUE;
     END IF;
   END IF;
 END IF;
 IF b.bet_id IS NOT NULL THEN
   IF b.market<>r->>'market' OR b.line<>(r->>'line')::numeric OR b.odds<>(r->>'odds')::numeric
      OR abs(extract(epoch FROM b.created_at-t))>2 THEN
     INSERT INTO sync_result VALUES('skipped',r->>'coupon','данные купона расходятся с реестром'); CONTINUE;
   END IF;
   IF b.is_freebet AND NOT COALESCE((r->>'is_freebet')::boolean,false) THEN
     INSERT INTO sync_result VALUES('skipped',r->>'coupon','признак фрибета расходится с реестром'); CONTINUE;
   END IF;
   IF b.status='open' AND r->>'status'<>'open' THEN
     UPDATE bets SET status=r->>'status',profit=(r->>'profit')::numeric,
       goals_home=(r->>'goals_home')::smallint,goals_away=(r->>'goals_away')::smallint,
       settled_at=(r->>'settled')::timestamptz,fonbet_coupon_id=r->>'coupon',
       is_freebet=COALESCE((r->>'is_freebet')::boolean,false) WHERE bet_id=b.bet_id;
     INSERT INTO sync_result VALUES('updated',r->>'coupon',b.bet_id::text);
   ELSIF b.status<>r->>'status' THEN
     INSERT INTO sync_result VALUES('skipped',r->>'coupon','расхождение рассчитанного статуса');
   ELSE
     UPDATE bets SET fonbet_coupon_id=r->>'coupon',is_freebet=COALESCE((r->>'is_freebet')::boolean,false)
       WHERE bet_id=b.bet_id AND (fonbet_coupon_id IS NULL OR is_freebet IS DISTINCT FROM COALESCE((r->>'is_freebet')::boolean,false));
     INSERT INTO sync_result VALUES('existing',r->>'coupon',b.bet_id::text);
   END IF;
   CONTINUE;
 END IF;
 IF NOT same_day THEN CONTINUE; END IF;
 SELECT array_agg(DISTINCT fixture_id) INTO ids FROM live_alerts_sent
   WHERE lower(home_name)=lower(r->>'home') AND lower(away_name)=lower(r->>'away')
     AND abs(extract(epoch FROM kickoff_at-(r->>'kickoff')::timestamptz)) < 21600;
 IF cardinality(ids)>1 THEN
   INSERT INTO sync_result VALUES('skipped',r->>'coupon','неоднозначный fixture_id'); CONTINUE;
 END IF;
 fid:=ids[1];
 IF fid IS NULL AND r->>'status'='open' THEN
   INSERT INTO sync_result VALUES('skipped',r->>'coupon','открытая ставка без fixture_id'); CONTINUE;
 END IF;
 INSERT INTO bets(fixture_id,market,line,odds,stake,kickoff_at,created_at,status,goals_home,goals_away,
                  settled_at,profit,manual_home_name,manual_away_name,fonbet_coupon_id,is_freebet)
 VALUES(fid,r->>'market',(r->>'line')::numeric,(r->>'odds')::numeric,1,(r->>'kickoff')::timestamptz,t,
        r->>'status',(r->>'goals_home')::smallint,(r->>'goals_away')::smallint,(r->>'settled')::timestamptz,
        (r->>'profit')::numeric,r->>'home',r->>'away',r->>'coupon',COALESCE((r->>'is_freebet')::boolean,false));
 INSERT INTO sync_result VALUES('added',r->>'coupon',currval(pg_get_serial_sequence('bets','bet_id'))::text);
END LOOP;
END $sync$;
SELECT row_to_json(sync_result) FROM sync_result;
COMMIT;
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--day', default=(datetime.now(MSK).date()-timedelta(days=1)).isoformat())
    ap.add_argument('--dates', action='store_true')
    ap.add_argument('--input')
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()
    day = datetime.strptime(args.day, '%Y-%m-%d').date()
    if args.dates:
        dates = psql("SELECT DISTINCT to_char(created_at AT TIME ZONE 'Europe/Moscow','DD.MM') "
                     "FROM bets WHERE status='open' AND market='TB';")
        print(json.dumps(sorted(set(dates.split()) | {day.strftime('%d.%m')})))
        return
    with open(args.input) as export:
        data = json.load(export)
    display_day = day.strftime('%d.%m')
    if (not isinstance(data, dict) or data.get('complete') is not True
            or not isinstance(data.get('dates'), list) or display_day not in data['dates']
            or not isinstance(data.get('rows'), list)):
        raise ValueError('неполный обход истории')
    parsed, skipped = [], []
    found = 0
    for raw in data['rows']:
        if not is_nightly_total_over(raw):
            continue
        found += int(raw.get('date') == display_day)
        try:
            parsed.append(parse_coupon(raw))
        except (ValueError, KeyError, TypeError, AttributeError, InvalidOperation) as exc:
            skipped.append(dict(coupon=raw.get('id'), detail=str(exc)))
    results = []
    if args.apply and parsed:
        payload = json.dumps(parsed, ensure_ascii=False).replace("'", "''")
        sql = SQL.replace('__DATA__', "'"+payload+"'").replace('__DAY__', "'"+day.isoformat()+"'")
        results = [json.loads(line) for line in psql(sql).splitlines() if line.startswith('{')]
    skipped_count=len(skipped)+sum(r.get('action')=='skipped' for r in results)
    print(json.dumps(dict(status='partial' if skipped_count else 'complete',skipped_count=skipped_count,day=day.isoformat(), found=found,
                         applied=args.apply, freebet_count=sum(row['is_freebet'] for row in parsed), results=results, skipped=skipped), ensure_ascii=False))
    return 2 if skipped_count else 0

if __name__ == '__main__':
    raise SystemExit(main())
