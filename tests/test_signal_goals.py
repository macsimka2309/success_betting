from src.jobs.signal_goals import api_timeline,load_api_timeline
from tests.test_collect import make_client


def fixture(events,score=(1,0),status='FT'):
    return dict(fixture={'id':10,'status':{'short':status}},teams={'home':{'id':1},'away':{'id':2}},
                score={'fulltime':{'home':score[0],'away':score[1]}},events=events)

def goal(minute=80,extra=None,side=1,detail='Normal Goal'):
    return dict(type='Goal',detail=detail,team={'id':side},time={'elapsed':minute,'extra':extra})


def test_last_added_goal_and_extra_time():
    result=api_timeline(fixture([goal(90,12),goal(105,1)],status='AET'))
    assert result.complete and len(result.goals)==1
    assert result.goals[0]['added_minute']==12 and result.goals[0]['score_after_home']==1

def test_empty_is_not_missing():
    assert api_timeline(fixture([],score=(0,0))).complete
    item=fixture([],score=(0,0));del item['events']
    assert not api_timeline(item).complete
    assert not api_timeline(fixture([],score=(1,0))).complete

def test_own_goal_credited_by_score_not_player_team():
    result=api_timeline(fixture([goal(side=2,detail='Own Goal')]))
    assert result.complete and result.goals[0]['credited_side']=='home'
    result=api_timeline(fixture([goal(side=1,detail='Own Goal')]))
    assert result.complete and result.goals[0]['credited_side']=='home'

def test_penalty_missed_and_unknown_fields():
    assert api_timeline(fixture([goal(detail='Penalty'),goal(detail='Missed Penalty')])).complete
    assert not api_timeline(fixture([goal(None)])).complete
    assert not api_timeline(fixture([goal(side=999)])).complete
    assert api_timeline(fixture([],status='ABD')).reason=='void'

def test_uncached_archived_client_adapter(tmp_path):
    client=make_client(tmp_path,[{'response':[fixture([goal()])]}])
    timeline,receipt,payload=load_api_timeline(client,10)
    assert timeline.complete and receipt is not None and payload['fixture']['id']==10
