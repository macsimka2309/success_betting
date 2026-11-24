"""API-Football timeline adapter. Full source payload is retained by the caller.

Contract: https://www.api-football.com/documentation_v3#tag/Fixtures/operation/get-fixtures-events
No fallback from a missing events list to an empty (zero-goal) timeline.
"""
from dataclasses import dataclass,field
from itertools import product


@dataclass
class Timeline:
    goals: list[dict]=field(default_factory=list)
    complete: bool=False
    reason: str | None=None
    regulation_score: tuple | None=None
    status: str='unknown'
    attempted: bool=True
    elapsed: int | None=None
    added_minute: int | None=None
    period: str='unknown'

    def __post_init__(self):
        if self.regulation_score is not None:self.regulation_score=tuple(self.regulation_score)

    @property
    def outcome(self):
        if not self.attempted:return 'deferred_internal'
        return 'attempted_error' if self.reason in ('source_unavailable','timeline_unavailable','browser_time_budget') else 'attempted_success'



def integer(value):
    return value if type(value) is int and value>=0 else None


def api_timeline(item):
    status=((item or {}).get('fixture') or {}).get('status') or {}
    result=Timeline(status=status.get('short','unknown'))
    if result.status in ('CANC','ABD','AWD','WO'):
        result.reason='void';return result
    if result.status not in ('FT','ET','BT','P','AET','PEN'):
        result.reason='regulation_end_unconfirmed';return result
    score=((item or {}).get('score') or {}).get('fulltime') or {}
    h,a=integer(score.get('home')),integer(score.get('away'))
    if h is not None and a is not None:result.regulation_score=(h,a)
    events=(item or {}).get('events')
    if not isinstance(events,list):result.reason='timeline_unavailable';return result
    teams=(item or {}).get('teams') or {}
    ids={side:(teams.get(side) or {}).get('id') for side in ('home','away')}
    malformed=False
    own_indices=[]
    for index,event in enumerate(events):
        if event.get('type')!='Goal' or event.get('detail')=='Missed Penalty':continue
        time=event.get('time') or {}
        minute,extra=integer(time.get('elapsed')),integer(time.get('extra'))
        # API represents stoppage time as elapsed=90, extra=N.
        if minute is not None and minute>90:continue
        if minute is None:malformed=True
        tid=(event.get('team') or {}).get('id')
        side=next((s for s in ids if ids[s] is not None and ids[s]==tid),None)
        if side is None:malformed=True
        goal=dict(source_goal_id=event.get('id'),source_team_id=tid,credited_side=side,
                  minute=minute,added_minute=extra,
                  period='unknown' if minute is None else 'regulation_1h' if minute<=45 else 'regulation_2h',
                  source_ordinal=index,detail=event.get('detail'),score_after_home=None,score_after_away=None)
        if event.get('detail')=='Own Goal':own_indices.append(len(result.goals))
        result.goals.append(goal)
    # Own-goal team conventions must be resolved by regulation score, never guessed.
    candidates=[]
    if result.regulation_score is not None and not malformed and len(own_indices)<=8:
        for flips in product((False,True),repeat=len(own_indices)):
            sides=[g['credited_side'] for g in result.goals]
            for i,flip in zip(own_indices,flips):
                if flip:sides[i]='away' if sides[i]=='home' else 'home'
            if (sides.count('home'),sides.count('away'))==result.regulation_score:candidates.append(sides)
    if len(candidates)==1:
        home=away=0
        for goal,side in zip(result.goals,candidates[0]):
            goal['credited_side']=side
            home+=side=='home';away+=side=='away'
            goal['score_after_home']=home;goal['score_after_away']=away
        result.complete=True
    else:
        for i in own_indices:result.goals[i]['credited_side']=None
        result.reason='event_details_missing' if malformed else 'own_goal_order_ambiguous' if len(candidates)>1 else 'regulation_score_mismatch'
    if result.regulation_score is None:result.reason='regulation_score_missing';result.complete=False
    return result


def load_api_timeline(client,fixture_id):
    """Client archives the uncached HTTP observation before normalization."""
    body=client.get('/fixtures',{'id':fixture_id},use_cache=False)
    if not body or body.get('errors') or not isinstance(body.get('response'),list):
        return Timeline(reason='source_unavailable'),getattr(client,'last_observation',None),body
    item=next((i for i in body['response'] if (i.get('fixture') or {}).get('id')==fixture_id),None)
    return api_timeline(item),getattr(client,'last_observation',None),item



def load_fonbet_timeline(url,*,runner=None,regulation_end_confirmed=False,regulation_score=None,result_day=None):
    """Best effort sports read through existing locks. No score-only synthetic events."""
    import json,subprocess
    from pathlib import Path
    from tools.fonbet_live.timeline import parse_timeline,completion_evidence
    from tools.fonbet_live.match_clock import fonbet_clock
    if runner is None:runner=subprocess.run
    try:
        response=runner(['bash',str(Path(__file__).resolve().parents[2]/'deploy/run_fonbet_goals.sh'),url,result_day or '', 'result' if regulation_end_confirmed else 'live'],
                        capture_output=True,text=True,timeout=65)
        if response.returncode==75:return Timeline(reason='browser_busy',attempted=False),None,{'reason':'browser_busy'}
        data=json.loads(response.stdout)
        if not isinstance(data,dict):raise ValueError()
        parsed=parse_timeline(data.get('html'))
        status,elapsed,added,period=fonbet_clock(data.get('clock_text'),data.get('period_text'))
        from urllib.parse import urlsplit
        event_id=urlsplit(url).path.rstrip('/').split('/')[-1]
        if data.get('html') and str(data.get('source_event_id'))!=event_id:
            return Timeline(reason='event_identity_mismatch'),None,{'reason':'event_identity_mismatch'}
        evidence=completion_evidence(data.get('completion_html'),event_id)
        if evidence and status not in ('ET','P','AET','PEN','FT'):
            status=evidence['status']
        if evidence and evidence['status'] in ('CANC','ABD'):
            status=evidence['status']
        ended=status in ('FT','REG_END','ET','BT','P','AET','PEN') or regulation_end_confirmed
        result=Timeline(goals=[g for g in parsed['goals'] if g['period'] in ('regulation_1h','regulation_2h')],
                        reason=data.get('reason') or parsed['reason'],status=status,
                        attempted=data.get('contact_attempted',True),elapsed=elapsed,added_minute=added,period=period)
        independent=evidence.get('regulation_score') if evidence else None
        same_sides=not independent or (
            [name.casefold() if name else None for name in parsed.get('teams',[])]
            == [name.casefold() for name in evidence['teams']]
        )
        expected=independent if independent is not None else regulation_score
        if expected is not None:
            result.regulation_score=tuple(expected)
        if ended and parsed.get('regulation_score') is not None:
            if not same_sides:
                result.reason='result_sides_mismatch'
            elif expected is None:
                result.reason='regulation_score_missing'
            elif tuple(parsed['regulation_score'])!=tuple(expected):
                result.reason='regulation_score_mismatch'
            elif regulation_score is not None and independent is not None and tuple(regulation_score)!=tuple(independent):
                result.reason='regulation_score_conflict'
            elif (not data.get('reason') and not parsed['regulation_reason']
                  and (parsed['reason'] is None or status in ('ET','BT','P','AET','PEN'))):
                # Once the second half is verified, an ET/shootout tally in
                # the widget's header does not affect regulation completeness.
                result.reason=None
                result.complete=True
        elif not result.reason:
            result.reason='regulation_end_unconfirmed'
        payload={'goals':parsed['goals'],'score':parsed.get('score'),'clock_text':data.get('clock_text'),
                 'period_text':data.get('period_text'),'reason':result.reason,'completion_evidence':evidence,
                 'teams':parsed.get('teams'),'parsed_regulation_score':parsed.get('regulation_score'),
                 'structure_valid':parsed.get('reason') is None}
        return result,data.get('observed_at'),payload
    except (OSError,ValueError,TypeError,subprocess.TimeoutExpired):
        return Timeline(reason='timeline_unavailable',attempted=False),None,{'reason':'timeline_unavailable'}


def load_timeline_sources(client,aliases,*,fonbet_loader=load_fonbet_timeline):
    """One request per source, API first; observations returned separately for audit."""
    observations=[];api=None
    for source,event_id in aliases:
        if source=='api_football':
            api,receipt,payload=load_api_timeline(client,int(event_id))
            observations.append((source,str(event_id),api,receipt,payload))
            if api.complete:return observations
            break
    for source,event_id in aliases:
        if source=='fonbet':
            # URL comes from a validated saved snapshot (set by the caller).
            result,receipt,payload=fonbet_loader(event_id,regulation_end_confirmed=bool(api and api.status in ('FT','ET','BT','P','AET','PEN')),regulation_score=api.regulation_score if api else None)
            observations.append((source,str(event_id),result,receipt,payload));break
    return observations
