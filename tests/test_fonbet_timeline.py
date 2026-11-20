from pathlib import Path
from tools.fonbet_live.timeline import parse_timeline

FIXTURES=Path(__file__).parent/'fixtures/fonbet'


def test_real_anonymized_full_timeline():
    result=parse_timeline((FIXTURES/'timeline-seven-goals.html').read_text())
    assert result['reason'] is None and result['score']==[7,0]
    assert [g['minute'] for g in result['goals']]==[18,55,61,63,67,79,84]
    assert all(g['credited_side']=='home' for g in result['goals'])
    assert result['goals'][2]['detail']=='Пенальти забит'
    assert result['goals'][-1]['score_after_home']==7


def test_unavailable_not_zero():
    assert parse_timeline((FIXTURES/'timeline-unavailable.html').read_text())['reason']=='timeline_unavailable'


def test_last_stoppage_and_incomplete():
    html=(FIXTURES/'timeline-seven-goals.html').read_text().replace("84'","90+12'")
    result=parse_timeline(html)
    assert result['goals'][-1]['minute']==90 and result['goals'][-1]['added_minute']==12
    assert parse_timeline(html.replace('(6:0)','(8:0)'))['reason']=='timeline_incomplete'


def test_adapter_fonbet_only_and_missing_events():
    from types import SimpleNamespace
    import json
    from src.jobs.signal_goals import load_fonbet_timeline
    html=(FIXTURES/'timeline-seven-goals.html').read_text()
    runner=lambda *a,**k:SimpleNamespace(returncode=0,stdout=json.dumps({'html':html,'source_event_id':'2','clock_text':'90','period_text':'Матч окончен'}))
    timeline,_,_=load_fonbet_timeline('https://fon.bet/live/football/1/2',runner=runner,regulation_end_confirmed=True,regulation_score=(7,0))
    assert timeline.complete and len(timeline.goals)==7
    timeline,_,_=load_fonbet_timeline('https://fon.bet/live/football/1/2',runner=lambda *a,**k:SimpleNamespace(returncode=75))
    assert not timeline.complete and timeline.reason=='browser_busy'


def test_primary_source_skips_browser_and_gap_attempts_it(tmp_path):
    from tests.test_collect import make_client
    from tests.test_signal_goals import fixture,goal
    from src.jobs.signal_goals import load_timeline_sources,Timeline
    client=make_client(tmp_path,[{'response':[fixture([goal()])]}])
    assert len(load_timeline_sources(client,[('api_football','10'),('fonbet','url')],fonbet_loader=lambda *a,**k:None))==1
    seen=[]
    result=load_timeline_sources(None,[('fonbet','url')],fonbet_loader=lambda *a,**k:seen.append(a) or (Timeline(reason='timeline_unavailable'),None,{}))
    assert seen and result[0][2].reason=='timeline_unavailable'


def test_stale_live_score_cannot_prove_complete_finished_timeline():
    from types import SimpleNamespace
    import json
    from src.jobs.signal_goals import load_fonbet_timeline
    html=(FIXTURES/'timeline-seven-goals.html').read_text()
    runner=lambda *a,**k:SimpleNamespace(returncode=0,stdout=json.dumps({'html':html,'source_event_id':'2','clock_text':'83','period_text':'2 тайм'}))
    result,_,_=load_fonbet_timeline('https://fon.bet/live/football/1/2',runner=runner,regulation_end_confirmed=True)
    assert not result.complete
    result,_,_=load_fonbet_timeline('https://fon.bet/live/football/1/2',runner=runner,regulation_end_confirmed=True,regulation_score=(7,0))
    assert result.complete


def test_real_completed_result_proves_end_without_live_discovery():
    from types import SimpleNamespace
    import json
    from src.jobs.signal_goals import load_fonbet_timeline
    from src.jobs.signal_results import end_state
    from tools.fonbet_live.timeline import completion_evidence
    html=(FIXTURES/'completed-result.html').read_text()
    assert completion_evidence(html,'69626793')['status']=='REG_END'
    assert completion_evidence(html,'wrong') is None
    assert completion_evidence(html.replace('_selected--','_inactive--'),'69626793') is None
    runner=lambda *a,**k:SimpleNamespace(returncode=0,stdout=json.dumps({'completion_html':html,'clock_text':'90:00','period_text':''}))
    result,_,payload=load_fonbet_timeline('https://fon.bet/live/football/11978/69626793',runner=runner)
    assert end_state(result.status)=='ended' and not result.complete
    assert payload['completion_evidence']['source_event_id']=='69626793'


def test_unlabelled_105th_minute_is_not_regulation_goal():
    html=(FIXTURES/'timeline-seven-goals.html').read_text().replace("84'","105'")
    result=parse_timeline(html)
    assert result['reason']=='timeline_incomplete'
    assert result['goals'][-1]['period']=='unknown'


def test_cancelled_result_is_not_regulation_end_without_play():
    from tools.fonbet_live.timeline import completion_evidence
    html=(FIXTURES/'completed-result.html').read_text().replace('>01:30<','>Отменен<')
    assert completion_evidence(html,'69626793')['status']=='CANC'


def test_complete_timeline_needs_both_regulation_period_markers():
    import re
    html=re.sub('2-й тайм','Перерыв',(FIXTURES/'timeline-seven-goals.html').read_text(),flags=re.I)
    assert parse_timeline(html)['reason']=='timeline_incomplete'


def completed_with_columns(labels):
    captions=''.join(f'<span class="score-caption--fixture">{label}</span>' for label in labels)
    return captions+(FIXTURES/'completed-result.html').read_text()


def read_combined(html,completed,event_id='69626793',period='Матч окончен'):
    from types import SimpleNamespace
    from src.jobs.signal_goals import load_fonbet_timeline
    import json
    body=dict(html=html,completion_html=completed,period_text=period,source_event_id=event_id)
    runner=lambda *a,**k:SimpleNamespace(returncode=0,stdout=json.dumps(body))
    return load_fonbet_timeline('https://fon.bet/live/football/11978/69626793',runner=runner)[0]


def test_independent_regulation_score_conflicts_with_seven_goal_widget():
    html=(FIXTURES/'timeline-seven-goals.html').read_text()
    completed=completed_with_columns(['У','УВ','1','2','Итог'])
    timeline=read_combined(html,completed)
    assert not timeline.complete and timeline.status=='FT'
    assert timeline.regulation_score==(0,1) and timeline.reason=='regulation_score_mismatch'
    assert len(timeline.goals)==7


def test_only_extra_time_totals_cannot_prove_regulation():
    html=(FIXTURES/'timeline-seven-goals.html').read_text()
    completed=completed_with_columns(['У','УВ','ОВ','П','Итог'])
    timeline=read_combined(html,completed,period='Дополнительное время')
    assert timeline.status=='ET'
    assert not timeline.complete and timeline.reason=='regulation_score_missing'


def test_independent_score_needs_same_event_and_side_order():
    html=(FIXTURES/'timeline-seven-goals.html').read_text()
    completed=completed_with_columns(['У','УВ','1','2','Итог'])
    assert read_combined(html,completed,event_id='22').reason=='event_identity_mismatch'
    reversed_names=completed.replace('>Home<','>tmp<').replace('>Away<','>Home<').replace('>tmp<','>Away<')
    assert read_combined(html,reversed_names).reason=='result_sides_mismatch'


def test_explicit_nil_nil_and_empty_regulation_timeline():
    html="""<div class="timeline--test">
      <div class="header_team--x"><span class="team-name--x">Home</span><span class="score--x">0</span></div>
      <div class="header_team--x"><span class="team-name--x">Away</span><span class="score--x">0</span></div>
      <div class="row-period--x">2-й тайм</div><div class="row-period--x">1-й тайм</div>
    </div>"""
    completed=completed_with_columns(['У','УВ','1','2','Итог']).replace('>1<','>0<')
    # Preserve the first-half column caption. Replace only score cells.
    completed=completed.replace('score-caption--fixture">0<','score-caption--fixture">1<')
    timeline=read_combined(html,completed)
    assert timeline.complete and not timeline.goals and timeline.regulation_score==(0,0)
