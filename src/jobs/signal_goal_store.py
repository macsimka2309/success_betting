"""Versioned sports observations; source snapshots never overwrite audit history."""
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime,timezone
import hashlib,json
from uuid import uuid4

from psycopg.types.json import Jsonb
from src.jobs.alert_dispatch import rows


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()


def minute_key(goal):
    return goal.get('period'),goal.get('minute'),goal.get('added_minute') or 0


def same_minute(left,right):
    return minute_key(left)==minute_key(right)


def score_after(goal):
    return goal.get('score_after_home'),goal.get('score_after_away')


def conflicting_score(left,right):
    return any(a is not None and b is not None and a!=b for a,b in zip(score_after(left),score_after(right)))


def provider_key(goal):
    provider_id=goal.get('source_goal_id')
    return str(provider_id) if provider_id is not None else None


def scored_minute_key(goal):
    side=goal.get('credited_side')
    score=score_after(goal)
    if side is None or goal.get('minute') is None or None in score:
        return None
    return side,minute_key(goal),score


FONBET_GOAL_TYPES = {'Гол', 'Пенальти забит', 'Автогол'}


def clock_key(goal):
    if goal.get('credited_side') is not None and goal.get('minute') is not None:
        return goal['credited_side'], minute_key(goal)
    return None


def compatible_fonbet_type(goal, old):
    incoming = goal.get('detail')
    previous = old['source_detail']
    return incoming == previous or incoming not in FONBET_GOAL_TYPES or previous not in FONBET_GOAL_TYPES


def clock_order(goals, side):
    # In a validated snapshot the scoring team's tally encodes chronology.
    # Use it inside the snapshot only; cumulative scores can shift between reads.
    scores = [goal.get(f'score_after_{side}') for goal in goals]
    if None in scores or len(set(scores)) != len(scores):
        return None
    return scores


def verified_clock_moves(goals, ordinals, retained, available, incoming_clocks):
    """Keep a clock anchor if the other goals map to vacated clocks in the same prior snapshot."""
    pairs = []
    used = set()
    for ordinal in ordinals:
        goal = goals[ordinal]
        score = score_after(goal)
        if None in score:
            return []
        candidates = [
            row for row in available.values()
            if row['active'] and row['order_verified'] and row['observation_id'] == retained['observation_id']
            and row['credited_side'] == retained['credited_side'] and row['period'] == retained['period']
            and (row is retained or clock_key(row) not in incoming_clocks)
            and score_after(row) == score and compatible_fonbet_type(goal, row)
        ]
        if len(candidates) != 1 or candidates[0]['goal_id'] in used:
            return []
        pairs.append((ordinal, candidates[0]))
        used.add(candidates[0]['goal_id'])
    return pairs if retained['goal_id'] in used else []


def reserve_fonbet_clock_groups(goals, existing, reserved, available, ordered):
    old_groups = defaultdict(list)
    for row in existing:
        if row['active'] and clock_key(row) is not None:
            old_groups[clock_key(row)].append(row)
    incoming = defaultdict(list)
    for ordinal, goal in enumerate(goals):
        if clock_key(goal) in old_groups:
            incoming[clock_key(goal)].append(ordinal)
    incoming_clocks = {clock_key(goal) for goal in goals}

    pending = {}
    for key, ordinals in incoming.items():
        old = old_groups[key]
        side = key[0]
        old_order = clock_order(old, side)
        # Leave equivalent copies from merged aliases to the normal merge path.
        if max(len(old), len(ordinals)) < 2:
            continue
        if (len(ordinals) == 1 and len({row['alert_match_id'] for row in old}) == len(old)
                and len({scored_minute_key(row) for row in old}) == 1
                and scored_minute_key(old[0]) is not None
                and len({row['source_detail'] for row in old}) == 1):
            continue

        new_order = clock_order([goals[i] for i in ordinals], side)
        pairs = []
        type_conflicts = {}
        if (ordered and len(old) == len(ordinals) and old_order is not None
                and new_order is not None and new_order == sorted(new_order)
                and len({row['observation_id'] for row in old}) == 1
                and all(row['order_verified'] for row in old)):
            ranked = sorted(old, key=lambda row: row[f'score_after_{side}'])
            pairs = list(zip(ordinals, ranked))
            if any(i in reserved and reserved[i]['goal_id'] != row['goal_id']
                   or i not in reserved and row['goal_id'] not in available
                   for i, row in pairs):
                pairs = []
            type_conflicts = {
                i: row for i, row in pairs
                if i not in reserved and provider_key(goals[i]) is None
                and not compatible_fonbet_type(goals[i], row)
            }
            if type_conflicts:
                # A conflicting position weakens the ordinal evidence for its
                # neighbors. Keep pairs that also have a unique matching type.
                incoming_types = Counter(goals[i].get('detail') for i in ordinals)
                previous_types = Counter(row['source_detail'] for row in old)
                pairs = [
                    (i, row) for i, row in pairs
                    if goals[i].get('detail') in FONBET_GOAL_TYPES
                    and goals[i]['detail'] == row['source_detail']
                    and incoming_types[goals[i]['detail']] == previous_types[row['source_detail']] == 1
                ]
        else:
            unbound = [i for i in ordinals if i not in reserved and provider_key(goals[i]) is None]
            types = Counter(goals[i].get('detail') for i in unbound)
            for i in unbound:
                detail = goals[i].get('detail')
                candidates = [row for row in old if row['goal_id'] in available
                              and row['source_detail'] == detail]
                if detail in FONBET_GOAL_TYPES and types[detail] == 1 and len(candidates) == 1:
                    pairs.append((i, candidates[0]))
            if (not pairs and len(old) == 1 and len(unbound) > 1 and ordered
                    and old[0]['order_verified'] and old[0]['goal_id'] in available):
                pairs = verified_clock_moves(goals, unbound, old[0], available, incoming_clocks)
        for i, row in pairs:
            if i not in reserved and provider_key(goals[i]) is None:
                reserved[i] = available.pop(row['goal_id'])

        remaining = [row for row in old if row['goal_id'] in available]
        if not remaining:
            continue
        pending_ids = set()
        for i in ordinals:
            if i not in reserved and provider_key(goals[i]) is None:
                candidates = [
                    row for row in remaining
                    if compatible_fonbet_type(goals[i], row) or row is type_conflicts.get(i)
                ]
                if not candidates and ordered and len(old) == len(ordinals):
                    # After a partial reconciliation the previous rows may come
                    # from different observations. A conflicting type within an
                    # unchanged group size still needs identity review.
                    candidates = remaining
                if candidates:
                    pending[i] = candidates
                    pending_ids.update(row['goal_id'] for row in candidates)
        for goal_id in pending_ids:
            available.pop(goal_id)
    return pending


def reserve_unchanged_fonbet_clocks(goals, reserved, pending, available):
    """Reserve unique same-clock, same-type pairs in a verified timeline."""
    previous = defaultdict(list)
    for row in available.values():
        key = clock_key(row)
        if row['active'] and key is not None and row['source_detail'] in FONBET_GOAL_TYPES:
            previous[key, row['source_detail']].append(row)

    incoming = defaultdict(list)
    for ordinal, goal in enumerate(goals):
        key = clock_key(goal)
        detail = goal.get('detail')
        if ordinal not in reserved and provider_key(goal) is None and key is not None and detail in FONBET_GOAL_TYPES:
            incoming[key, detail].append(ordinal)

    for key, ordinals in incoming.items():
        candidates = previous.get(key, ())
        if len(ordinals) == len(candidates) == 1 and ordinals[0] not in pending:
            reserved[ordinals[0]] = available.pop(candidates[0]['goal_id'])


def include_possible_clock_corrections(goals, reserved, pending, available):
    # A goal held for same-clock review is still a candidate for a corrected
    # minute. Keep that possibility visible before any new ID is assigned.
    disputed = {row['goal_id']: row for candidates in pending.values() for row in candidates}
    for ordinal, goal in enumerate(goals):
        if ordinal in reserved or provider_key(goal) is not None:
            continue
        candidates = [
            row for row in disputed.values()
            if row['credited_side'] == goal.get('credited_side') and row['period'] == goal.get('period')
            and not same_minute(row, goal) and compatible_fonbet_type(goal, row)
        ]
        if candidates:
            candidates.extend(
                row for row in available.values()
                if row['active'] and clock_key(row) == clock_key(goal) and compatible_fonbet_type(goal, row)
            )
            pending.setdefault(ordinal, []).extend(candidates)


def reserve_goal_matches(goals, existing, *, fonbet=False, ordered=False, crowded_clocks=()):
    """Reserve unique matches across the observation before trying corrections."""
    reserved={}
    available={row['goal_id']:row for row in existing}
    pending = {}
    for key in (provider_key,scored_minute_key):
        if key is scored_minute_key and fonbet:
            pending = reserve_fonbet_clock_groups(goals, existing, reserved, available, ordered)
        incoming={i:key(goal) for i,goal in enumerate(goals)
                  if i not in reserved and i not in pending
                  and (key is provider_key or provider_key(goal) is None)}
        counts=Counter(incoming.values())
        for ordinal,value in incoming.items():
            if value is None or counts[value]!=1:
                continue
            candidates=[row for row in available.values() if key(row)==value]
            if key is scored_minute_key:
                if clock_key(goals[ordinal]) in crowded_clocks:
                    candidates = [row for row in candidates if compatible_fonbet_type(goals[ordinal], row)]
                candidates=[row for row in candidates if row['active']] or candidates
            if len(candidates)==1:
                reserved[ordinal]=available.pop(candidates[0]['goal_id'])
    if pending:
        if ordered:
            reserve_unchanged_fonbet_clocks(goals, reserved, pending, available)
        include_possible_clock_corrections(goals, reserved, pending, available)
    return reserved, pending


def family(conn,mid):
    while True:
        parent=conn.execute('SELECT merged_into FROM live_alert_matches WHERE alert_match_id=%s',(mid,)).fetchone()[0]
        if parent is None:break
        mid=parent
    ids=[r[0] for r in conn.execute('WITH RECURSIVE f AS (SELECT alert_match_id FROM live_alert_matches WHERE alert_match_id=%s UNION ALL SELECT m.alert_match_id FROM live_alert_matches m JOIN f ON m.merged_into=f.alert_match_id) SELECT alert_match_id FROM f',(mid,))]
    return mid,ids


def latest_fonbet_browser_clock(conn, mid, event_id, now):
    _, ids = family(conn, mid)
    return conn.execute("""
        SELECT observed_at, (payload->'timeline'->>'elapsed')::integer
        FROM live_signal_goal_observations
        WHERE alert_match_id=ANY(%s) AND source='fonbet' AND source_event_id=%s AND observed_at<=%s
          AND payload->'timeline'->>'status'='2H' AND payload->'timeline'->>'period'='regulation_2h'
          AND payload->'timeline'->'attempted'='true'::jsonb
          AND incomplete_reason IS DISTINCT FROM 'superseded_observation'
        ORDER BY observed_at DESC, observation_id DESC LIMIT 1
    """, (ids, event_id, now)).fetchone()


def save_timeline(conn,mid,source,event_id,timeline,observed_at,payload,receipt=None,*,
                  superseded=False,status_only=False):
    """Caller owns a short transaction after I/O. Serialize only identity/data writes."""
    conn.execute('SELECT pg_advisory_xact_lock(27001)')
    mid,ids=family(conn,mid)
    snapshot={'timeline':asdict(timeline),'payload':payload}
    # A merge leaves original archive rows under their recorded match IDs.
    oid, applied_complete, applied_reason, replayed = conn.execute('''
      WITH archived AS (
        SELECT observation_id,complete,incomplete_reason
        FROM live_signal_goal_observations
        WHERE alert_match_id=ANY(%(family)s) AND source=%(source)s AND source_event_id=%(event)s
          AND observed_at=%(observed)s AND payload_hash=%(fingerprint)s
        ORDER BY observation_id LIMIT 1
      ), inserted AS (
        INSERT INTO live_signal_goal_observations
          (alert_match_id,source,source_event_id,observed_at,receipt_reference,payload,payload_hash,complete,incomplete_reason)
        SELECT %(match)s,%(source)s,%(event)s,%(observed)s,%(receipt)s,
          %(snapshot)s,%(fingerprint)s,%(complete)s,%(reason)s
        WHERE NOT EXISTS (SELECT 1 FROM archived)
        RETURNING observation_id,complete,incomplete_reason
      )
      SELECT observation_id,complete,incomplete_reason,true FROM archived
      UNION ALL
      SELECT observation_id,complete,incomplete_reason,false FROM inserted
      ''', dict(
        family=ids,match=mid,source=source,event=event_id,observed=observed_at,
        receipt=str(receipt) if receipt is not None else None,snapshot=Jsonb(snapshot),
        fingerprint=digest(snapshot),complete=timeline.complete and not status_only,reason=timeline.reason,
    )).fetchone()
    # A later observation can advance the projection even when some of its
    # events remain pending. Older snapshots are retained for audit only.
    scope = (ids,source,observed_at,oid)
    newer = conn.execute('''SELECT EXISTS (
        SELECT 1 FROM live_signal_goal_observations o
        WHERE o.alert_match_id=ANY(%s) AND o.source=%s AND o.complete
          AND (o.observed_at,o.observation_id)>(%s,%s)
      ) OR EXISTS (
        SELECT 1 FROM live_signal_goals g JOIN live_signal_goal_observations o USING (observation_id)
        WHERE g.alert_match_id=ANY(%s) AND g.source=%s AND (o.observed_at,o.observation_id)>(%s,%s)
      )''', scope * 2).fetchone()[0]
    if superseded or newer:
        timeline.complete=False
        timeline.reason='superseded_observation'
        if not replayed:
            conn.execute("""UPDATE live_signal_goal_observations
              SET complete=false,incomplete_reason='superseded_observation' WHERE observation_id=%s""", (oid,))
        return oid
    if replayed:
        timeline.complete = applied_complete
        timeline.reason = applied_reason
        return oid
    if status_only:
        return oid
    existing = rows(conn, """SELECT g.*, (
        o.payload->'timeline'->'complete' = 'true'::jsonb
        OR (o.payload->'timeline'->>'status' = '2H'
            AND o.payload->'payload'->'structure_valid' = 'true'::jsonb)
      ) AS order_verified
      FROM live_signal_goals g JOIN live_signal_goal_observations o USING (observation_id)
      WHERE g.alert_match_id=ANY(%s) AND g.source=%s ORDER BY g.active DESC,g.goal_id""", (ids,source))
    consumed=set();matched=[];ambiguous=False
    was_complete=timeline.complete
    coherent_live=source=='fonbet' and timeline.status=='2H' and (payload or {}).get('structure_valid') is True
    crowded_clocks = set()
    if source == 'fonbet':
        for snapshot in ([row for row in existing if row['active']], timeline.goals):
            counts = Counter(map(clock_key, snapshot))
            crowded_clocks.update(key for key, count in counts.items() if key is not None and count > 1)
    reserved, pending = reserve_goal_matches(timeline.goals, existing, fonbet=source=='fonbet',
                                             ordered=was_complete or coherent_live, crowded_clocks=crowded_clocks)
    reserved_ids={g['goal_id'] for g in reserved.values()}
    reserved_ids.update(g['goal_id'] for candidates in pending.values() for g in candidates)
    for ordinal,goal in enumerate(timeline.goals):
        provider=provider_key(goal)
        claimed_ids=consumed|reserved_ids
        available=[g for g in existing if g['goal_id'] not in claimed_ids]
        if ordinal in reserved:
            candidates=[reserved[ordinal]]
        elif ordinal in pending:
            candidates=pending[ordinal]
        elif provider is not None:
            candidates=[g for g in existing if provider_key(g)==provider]
        elif goal.get('credited_side') is None:
            team=goal.get('source_team_id')
            candidates=[g for g in available if team is not None and g['source_team_id']==str(team)
                        and goal.get('detail') and g['source_detail']==goal['detail']
                        and same_minute(g,goal)]
        else:
            compatible=[g for g in available if g['period']==goal.get('period') and g['credited_side']==goal.get('credited_side')]
            if crowded_clocks:
                clock_is_crowded = clock_key(goal) in crowded_clocks
                compatible = [
                    g for g in compatible
                    if (not clock_is_crowded and clock_key(g) not in crowded_clocks)
                    or compatible_fonbet_type(goal, g)
                ]
            geometry=[g for g in compatible if same_minute(g,goal)]
            def same_score(g):
                return None not in score_after(goal) and score_after(g)==score_after(goal)
            # Minute/team/period continuity precedes mutable cumulative scoring.
            # In particular, a late inserted goal must not steal the identity of
            # a later unchanged goal whose cumulative score shifted.
            scored_geometry=[g for g in geometry if same_score(g)]
            clock_claims={g['goal_id'] for g in compatible if any(
                i!=ordinal and i not in reserved and other.get('credited_side')==g['credited_side']
                and same_minute(g,other) for i,other in enumerate(timeline.goals))}
            candidates=scored_geometry or geometry or [
                g for g in compatible if same_score(g) and g['goal_id'] not in clock_claims]
            active=[g for g in candidates if g['active']]
            if active:candidates=active
            if not candidates and not was_complete:
                same_clock=[g for g in existing if g['credited_side']==goal.get('credited_side')
                            and same_minute(g,goal)]
                clock_matches = same_clock
                if clock_key(goal) in crowded_clocks:
                    clock_matches = [g for g in same_clock if compatible_fonbet_type(goal, g)]
                if coherent_live and all(goal.get(k) is not None for k in ('score_after_home','score_after_away')):
                    # The validated live sequence proves different scores are
                    # separate goals, even before the final result is verified.
                    candidates=[g for g in clock_matches if same_score(g)]
                else:
                    candidates=compatible or clock_matches
                if not candidates and source == 'fonbet':
                    # In a crowded minute the type filter can hide precisely
                    # the unresolved event. Keep it as a review candidate.
                    conflicts = [g for g in same_clock if g['goal_id'] not in claimed_ids
                                 and not compatible_fonbet_type(goal, g)]
                    candidates = [g for g in conflicts if same_score(g)] or ([] if coherent_live else conflicts)
        # A complete observation can consolidate equivalent copies from
        # different merged identities, without choosing between distinct goals.
        if ordinal not in pending and was_complete and len(candidates)>1 and len({g['alert_match_id'] for g in candidates})==len(candidates):
            key=scored_minute_key(candidates[0])
            if provider is not None or key is not None and all(scored_minute_key(g)==key for g in candidates):
                candidates=[min(candidates,key=lambda g:(g['alert_match_id']!=mid,g['goal_id']))]
        old=candidates[0] if len(candidates)==1 else None
        unknown_side=not candidates and goal.get('credited_side') is None
        already_claimed=old is not None and ordinal not in reserved and old['goal_id'] in claimed_ids
        conflicting_partial=old is not None and provider is None and not was_complete and (
            not same_minute(old,goal)
            or source=='fonbet' and not compatible_fonbet_type(goal,old)
            or not coherent_live and conflicting_score(old,goal))
        if ordinal in pending or unknown_side or len(candidates)>1 or already_claimed or conflicting_partial:
            ambiguous=True
            conn.execute("""INSERT INTO live_signal_goal_pending VALUES(%s,%s,%s,%s,'identity_ambiguous')
             ON CONFLICT DO NOTHING""",(oid,ordinal,Jsonb([g['goal_id'] for g in candidates]),Jsonb(goal)))
            continue
        values={k:goal.get(k) if goal.get(k) is not None else old.get(k) if old else None
                for k in ('credited_side','source_team_id','minute','added_minute','period','score_after_home','score_after_away')}
        if old and goal.get('minute') is not None and (was_complete or goal['minute']!=old['minute']):
            values['added_minute']=goal.get('added_minute')
        if values['source_team_id'] is not None:values['source_team_id']=str(values['source_team_id'])
        detail=goal.get('detail') or (old['source_detail'] if old else None)
        if old:
            gid=old['goal_id'];consumed.add(gid)
            conn.execute("""UPDATE live_signal_goals SET observation_id=%s,credited_side=%s,source_team_id=%s,
             minute=%s,added_minute=%s,period=%s,score_after_home=%s,score_after_away=%s,source_detail=%s,
             revision=revision+1,match_quality=%s WHERE goal_id=%s""",
             (oid,*values.values(),detail,'complete_snapshot' if was_complete else 'partial_snapshot',gid))
        else:
            gid=conn.execute("""INSERT INTO live_signal_goals(alert_match_id,event_key,source,source_goal_id,observation_id,
              credited_side,source_team_id,minute,added_minute,period,score_after_home,score_after_away,source_detail,match_quality)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING goal_id""",
              (mid,'provider:'+provider if provider is not None else 'internal:'+str(uuid4()),source,
               provider,oid,*values.values(),detail,
               'complete_snapshot' if was_complete else 'partial_snapshot')).fetchone()[0]
        if old is None:existing.append(dict(values,goal_id=gid,alert_match_id=mid,source_detail=detail,
                                            source_goal_id=provider,active=True))
        consumed.add(gid)
        matched.append(gid)
    if ambiguous:
        timeline.complete=False;timeline.reason='identity_ambiguous'
        conn.execute("UPDATE live_signal_goal_observations SET complete=false,incomplete_reason='identity_ambiguous' WHERE observation_id=%s",(oid,))
    elif was_complete:
        conn.execute('UPDATE live_signal_goals SET active=true WHERE goal_id=ANY(%s)',(matched,))
        conn.execute("""UPDATE live_signal_goals SET active=false,observation_id=%s,revision=revision+1,
            match_quality='complete_snapshot'
          WHERE alert_match_id=ANY(%s) AND source=%s AND active AND NOT(goal_id=ANY(%s))""",
          (oid,ids,source,matched))
    conn.execute("""INSERT INTO live_signal_goal_versions(goal_id,observation_id,payload)
        SELECT goal_id,observation_id,jsonb_build_object(
          'credited_side',credited_side,'source_team_id',source_team_id,'minute',minute,
          'added_minute',added_minute,'period',period,'score_after_home',score_after_home,
          'score_after_away',score_after_away,'source_detail',source_detail,'active',active
        )
        FROM live_signal_goals WHERE alert_match_id=ANY(%s) AND source=%s AND observation_id=%s
        ON CONFLICT DO NOTHING""", (ids,source,oid))
    return oid


def event_relation(goal,signal):
    context=signal['first_context'];minute=goal.get('minute');anchor=context.get('current_minute',context.get('elapsed'))
    if type(minute) is not int or type(anchor) is not int:return 'order_unknown'
    if goal.get('period') not in ('regulation_1h','regulation_2h'):return 'before'
    event=(minute,goal.get('added_minute') or 0);boundary=(anchor,context.get('added_minute') or 0)
    if event<boundary:return 'before'
    if event==boundary:return 'order_unknown'
    # Bound how far the match clock could have progressed during snapshot/send lag.
    # Minute labels have a full minute of precision uncertainty; never treat
    # observation time as exact goal time or as exact message time.
    at=context.get('context_at') or context.get('observed_at')
    if not at:return 'order_unknown'
    if isinstance(at,str):at=datetime.fromisoformat(at)
    lag=(signal['confirmed_at']-at).total_seconds()
    if lag<0:return 'order_unknown'
    if minute-1>anchor+lag/60+1:return 'after'
    return 'order_unknown'


def relate_goals(conn,signal,source):
    _,ids=family(conn,signal['alert_match_id'])
    conn.execute('DELETE FROM live_signal_goal_relations WHERE signal_id=%s',(signal['signal_id'],))
    unknown=0;seen_scores=set()
    for goal in rows(conn,'SELECT * FROM live_signal_goals WHERE alert_match_id=ANY(%s) AND source=%s AND active ORDER BY goal_id',(ids,source)):
        if goal['score_after_home'] is not None and goal['score_after_away'] is not None:
            key=(goal['credited_side'],goal['period'],goal['score_after_home'],goal['score_after_away'])
            if key in seen_scores:continue
            seen_scores.add(key)
        relation=event_relation(goal,signal);unknown+=relation=='order_unknown'
        conn.execute('INSERT INTO live_signal_goal_relations(signal_id,goal_id,relation,reason) VALUES(%s,%s,%s,%s)',
          (signal['signal_id'],goal['goal_id'],relation,'порядок относительно сигнала не установлен' if relation=='order_unknown' else None))
    return unknown


def choose_timeline(observations):
    complete=[o for o in observations if o[2].complete]
    selected=next((o for o in complete if o[0]=='api_football'),None) or next(iter(complete),None) or next(iter(observations),None)
    scores=[o[2].regulation_score for o in observations if o[2].regulation_score is not None]
    conflict=bool(scores and any(s!=scores[0] for s in scores[1:]))
    if len(complete)>1:
        def comparable(o):return [(g.get('credited_side'),g.get('minute'),g.get('added_minute') or 0,g.get('score_after_home'),g.get('score_after_away')) for g in o[2].goals]
        conflict=conflict or any(comparable(o)!=comparable(complete[0]) for o in complete[1:])
    return selected,conflict
