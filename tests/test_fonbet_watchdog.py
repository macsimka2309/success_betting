import fcntl
import json
from pathlib import Path
from types import SimpleNamespace

from src.jobs import fonbet_watchdog as wd

NOW=1_800_000_000
MONO=1000


def service(result='success',active='inactive',ended=999):
    return {'Result':result,'ActiveState':active,'ExecMainExitTimestampMonotonic':str(ended*1_000_000)}


def state():return {'incidents':{},'restarts':[],'retries':{}}


def setup_root(tmp_path):
    from datetime import datetime,timedelta,timezone
    from zoneinfo import ZoneInfo
    day=datetime.fromtimestamp(NOW,timezone.utc).astimezone(ZoneInfo('Europe/Moscow')).date()-timedelta(days=1)
    p=tmp_path/'output';p.mkdir()
    (p/'last-result.json').write_text(json.dumps({'day':day.isoformat(),'applied':True}))


def harness(tmp_path,states=None,probes=(True,),memory=None):
    setup_root(tmp_path)
    commands=[];messages=[];values=list(probes)
    states=states or {wd.LIVE:service(),wd.SYNC:service()}
    def runner(argv,timeout):
        commands.append(argv)
        if argv[:2]==['systemctl','show']:
            return SimpleNamespace(returncode=0,stdout='\n'.join(f'{k}={v}' for k,v in states[argv[2]].items()))
        return SimpleNamespace(returncode=0,stdout='')
    def probe(runner):return values.pop(0) if len(values)>1 else values[0]
    def send(text,markup):messages.append(text);return 7
    def run(s,now=NOW,mono=MONO):
        return wd.monitor(s,now=now,monotonic=mono,root=tmp_path,runner=runner,probe=probe,send=send,sleep=lambda _:None,memory_reader=lambda r:memory if memory is not None else {'generation':'test','oom_kill':0})
    return run,commands,messages,states


def test_healthy_system_neither_restarts_nor_notifies(tmp_path):
    run,cmd,msg,_=harness(tmp_path)
    result=run(state())
    assert result['live_ok'] and result['browser_ok']
    assert len(cmd)==2 and msg==[]


def test_bad_browser_alert_then_restart_and_job_retry(tmp_path):
    run,cmd,msg,_=harness(tmp_path,probes=(False,True))
    s=state();result=run(s)
    assert result['recovery']=='browser_restarted'
    assert len(msg)==1 and msg[0].startswith('⚠️')
    assert any('restart' in c for c in cmd)
    assert any('--no-block' in c for c in cmd)
    assert s['restarts']==[NOW]


def test_active_job_prevents_restart(tmp_path):
    states={wd.LIVE:service(active='activating'),wd.SYNC:service()}
    run,cmd,msg,_=harness(tmp_path,states,probes=(False,))
    assert run(state())['recovery']=='deferred_busy'
    assert not any('restart' in c for c in cmd)


def test_locked_registry_prevents_restart(tmp_path):
    run,cmd,msg,_=harness(tmp_path,probes=(False,))
    p=tmp_path/'output/sync.lock'
    with p.open('a') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        assert run(state())['recovery']=='deferred_lock'
    assert not any('restart' in c for c in cmd)


def test_restart_limits_persist_and_reminders_do_not_spam(tmp_path):
    run,cmd,msg,_=harness(tmp_path,probes=(False,))
    s=state();s['restarts']=[NOW-1800,NOW-900,NOW-1]
    assert run(s)['recovery']=='rate_limited'
    run(s,now=NOW+60,mono=MONO+60)
    assert len(msg)==1
    assert not any('restart' in c for c in cmd)


def test_recovery_notice_waits_for_completed_job(tmp_path):
    run,cmd,msg,states=harness(tmp_path,probes=(False,True))
    s=state();run(s)
    run(s,now=NOW+60,mono=MONO+60)
    assert len(msg)==1 # old successful result is not proof of recovery
    states[wd.LIVE]=service(ended=1080)
    write_export(tmp_path,NOW+80,nonempty=1,selected=1)
    run(s,now=NOW+90,mono=MONO+90)
    assert len(msg)==2 and msg[-1].startswith('✅')
    assert 'live' not in s['incidents']
    run(s,now=NOW+100,mono=MONO+100)
    assert len(msg)==2


def test_stale_job_is_not_confused_with_zero_matches(tmp_path):
    run,cmd,msg,_=harness(tmp_path,{wd.LIVE:service(ended=1),wd.SYNC:service()})
    run(state())
    assert len(msg)==1 and not any('restart' in c for c in cmd)
    assert any('--no-block' in c for c in cmd)


def test_corrupt_state_does_not_reset_limits(tmp_path):
    path=tmp_path/'state.json';path.write_text('{broken')
    import pytest
    with pytest.raises(RuntimeError,match='watchdog_state_invalid'):wd.load_state(path)


def test_atomic_private_state_roundtrip(tmp_path):
    path=tmp_path/'state.json';s=state();s['restarts']=[NOW]
    wd.save_state(path,s)
    assert wd.load_state(path)==s
    assert path.stat().st_mode & 0o777==0o600


def test_restart_budget_reserved_before_command(tmp_path):
    run,commands,msg,_=harness(tmp_path,probes=(False,True))
    events=[];s=state()
    # The state persistence callback must observe the restart reservation before docker restart.
    setup=wd.service_state
    def runner(argv,timeout):
        if argv[:2]==['systemctl','show']:
            return SimpleNamespace(returncode=0,stdout='\n'.join(f'{k}={v}' for k,v in service().items()))
        if 'restart' in argv:assert events[-1]==[NOW]
        return SimpleNamespace(returncode=0,stdout='')
    checks=iter([False,True])
    wd.monitor(s,now=NOW,monotonic=MONO,root=tmp_path,send=lambda *a:1,runner=runner,probe=lambda r:next(checks),sleep=lambda _:None,persist=lambda x:events.append(list(x['restarts'])),memory_reader=lambda r:{'generation':'test','oom_kill':0})


def test_telegram_failure_is_rate_limited(tmp_path):
    setup_root(tmp_path);s=state();calls=[]
    def fail(*a):calls.append(1);raise RuntimeError('network')
    wd.reconcile_incident(s,'live',['browser unavailable'],NOW,fail)
    wd.reconcile_incident(s,'live',['browser unavailable'],NOW+60,fail)
    assert len(calls)==1


def test_stale_history_after_deadline_is_reported(tmp_path):
    setup_root(tmp_path)
    (tmp_path/'output/last-result.json').write_text('{"day":"2000-01-01","applied":true}')
    assert wd.sync_reasons(service(),tmp_path,NOW)


def test_history_watchdog_accepts_empty_selection_and_reports_damaged_over(tmp_path):
    setup_root(tmp_path)
    path = tmp_path / 'output/last-result.json'
    report = json.loads(path.read_text())
    report.update(status='complete', found=0, freebet_count=0, skipped_count=0, results=[], skipped=[])
    path.write_text(json.dumps(report))
    assert wd.sync_reasons(service(), tmp_path, NOW) == []
    assert wd.sync_reasons(service(result='exit-code'), tmp_path, NOW)

    report.update(status='partial', found=1, skipped_count=1, skipped=[{'coupon': '123'}])
    path.write_text(json.dumps(report))
    assert any('пропущено купонов 1; найдено 1' in reason
               for reason in wd.sync_reasons(service(), tmp_path, NOW))


def test_partial_run_is_reported_even_if_systemd_returned_zero(tmp_path):
    run,cmd,msg,_=harness(tmp_path)
    path=tmp_path/'output/live';path.mkdir()
    (path/'last-export.json').write_text('{"status":"partial"}')
    result=run(state())
    assert result['live_ok'] is False and len(msg)==1
    assert not any('restart' in c for c in cmd)
    assert result['live_retry']=='requested'

from datetime import datetime,timezone
import json



def write_export(root,now,*,nonempty=0,status='complete',selected=0):
    p=root/'output/live';p.mkdir(exist_ok=True)
    (p/'last-export.json').write_text(json.dumps({'run_id':'new','status':status,'started_at':datetime.fromtimestamp(now-1,timezone.utc).isoformat(),'finished_at':datetime.fromtimestamp(now,timezone.utc).isoformat(),
        'summary':{'nonempty':nonempty,'selected':selected,'processed':selected,'reasons':{},'memory':{'before':{'generation':'test'}}}}))


def test_oom_baseline_and_container_restart():
    s=state()
    assert wd.new_oom(s,{'generation':'a','oom_kill':3})==0
    assert wd.new_oom(s,{'generation':'a','oom_kill':4})==1
    assert wd.new_oom(s,{'generation':'a','oom_kill':4})==0
    assert wd.new_oom(s,{'generation':'b','oom_kill':0})==0
    assert wd.new_oom(s,{'generation':'b','oom_kill':1})==1


def test_new_oom_restarts_even_when_blank_page_healthy(tmp_path):
    run,commands,messages,_=harness(tmp_path)
    s=state();s['browser_memory']={'generation':'test','oom_kill':-1}
    result=run(s)
    assert result['new_oom_kills']==1 and result['recovery']=='browser_restarted'
    assert s['statistics_required_after']==NOW
    assert any('restart' in c for c in commands)


def test_oom_while_live_busy_is_deferred_and_remains_pending(tmp_path):
    run,commands,_,states=harness(tmp_path,{wd.LIVE:service(active='activating'),wd.SYNC:service()})
    s=state();s['browser_memory']={'generation':'test','oom_kill':-1}
    assert run(s)['recovery']=='deferred_busy';assert s['oom_restart_pending']
    assert not any('restart' in c for c in commands)
    states[wd.LIVE]=service(ended=1030)
    assert run(s,now=NOW+60,mono=MONO+60)['recovery']=='browser_restarted'


def test_empty_cycle_does_not_confirm_recovery_after_oom(tmp_path):
    run,_,messages,states=harness(tmp_path)
    s=state();s['browser_memory']={'generation':'test','oom_kill':-1}
    run(s)
    write_export(tmp_path,NOW+50)
    states[wd.LIVE]=service(ended=1050)
    assert not run(s,now=NOW+60,mono=MONO+60)['live_ok']
    assert not any(m.startswith('✅') for m in messages)
    write_export(tmp_path,NOW+80,nonempty=1,selected=1)
    states[wd.LIVE]=service(ended=1080)
    assert run(s,now=NOW+90,mono=MONO+90)['live_ok']
    assert sum(m.startswith('✅') for m in messages)==1


def test_crash_export_requests_only_one_restart(tmp_path):
    run,commands,_,_=harness(tmp_path)
    write_export(tmp_path,NOW-5,status='partial',selected=2)
    p=tmp_path/'output/live/last-export.json';v=json.loads(p.read_text());v['summary']['errors']=[{'code':'page_crashed'}];p.write_text(json.dumps(v))
    s=state();assert run(s)['recovery']=='browser_restarted'
    run(s,now=NOW+1000,mono=MONO+1000)
    assert sum('restart' in c for c in commands)==1


def test_oom_export_requests_restart_without_renderer_error(tmp_path):
    run,commands,_,_=harness(tmp_path)
    write_export(tmp_path,NOW-5,status='partial',selected=2)
    p=tmp_path/'output/live/last-export.json';v=json.loads(p.read_text());v['summary']['memory']={'oom_kill_delta':1};p.write_text(json.dumps(v))
    assert run(state())['recovery']=='browser_restarted'


def test_delayed_ready_does_not_restart_healthy_browser_after_cooldown(tmp_path):
    run,commands,messages,states=harness(tmp_path,probes=(False,False,False,False,True))
    s=state();assert run(s)['recovery']=='browser_starting'
    assert s['restart_attempt']['command_completed']
    # More than 15 minutes later the original code restarted a healthy browser.
    result=run(s,now=NOW+1000,mono=MONO+1000)
    assert result['recovery']=='browser_ready'
    assert not s['oom_restart_pending'] and 'restart_attempt' not in s
    assert result['phase']=='waiting_statistics'
    assert sum('restart' in c for c in commands)==1
    write_export(tmp_path,NOW+1100)
    run(s,now=NOW+1200,mono=MONO+1200)
    assert sum('restart' in c for c in commands)==1
    write_export(tmp_path,NOW+1300,nonempty=1,selected=1)
    states[wd.LIVE]=service(ended=2300)
    assert run(s,now=NOW+1400,mono=MONO+1400)['live_ok']


def test_changed_generation_clears_uncertain_restart_without_second_command(tmp_path):
    run,commands,_,_=harness(tmp_path)
    s=state();s['oom_restart_pending']=True
    s['restart_attempt']={'generation_before':'old','command_completed':False,'requested_at':NOW-1000}
    result=run(s)
    assert result['recovery']=='browser_ready' and not s['oom_restart_pending']
    assert not any('restart' in c for c in commands)


def test_old_crash_export_before_restart_never_requests_another_restart(tmp_path):
    run,commands,_,_=harness(tmp_path)
    write_export(tmp_path,NOW-100,status='partial',selected=1)
    p=tmp_path/'output/live/last-export.json';v=json.loads(p.read_text());v['summary']['errors']=[{'code':'page_crashed'}];p.write_text(json.dumps(v))
    s=state();s['last_restart_requested_at']=NOW-50
    result=run(s)
    assert result['recovery']=='none';assert not any('restart' in c for c in commands)


def test_pre_restart_scan_cannot_confirm_statistics(tmp_path):
    run,_,messages,_=harness(tmp_path)
    s=state();s['statistics_required_after']=NOW-50;s['statistics_required_generation']='new'
    write_export(tmp_path,NOW-5,nonempty=1,selected=1) # carries generation=test
    assert not run(s)['live_ok']
    assert not any(m.startswith('✅') for m in messages)


def test_partial_import_is_reported_despite_successful_service(tmp_path):
    run,_,messages,_=harness(tmp_path)
    p=tmp_path/'output/last-result.json';v=json.loads(p.read_text());v.update(status='partial',found=4,skipped=[{'coupon':'1','detail':'unknown'}]);p.write_text(json.dumps(v))
    assert not run(state())['sync_ok']
    assert any('пропущено купонов 1' in m for m in messages)


def test_database_conflict_is_also_a_partial_import(tmp_path):
    run,_,_,_=harness(tmp_path)
    p=tmp_path/'output/last-result.json';v=json.loads(p.read_text());v['results']=[{'action':'skipped','coupon':'1'}];p.write_text(json.dumps(v))
    assert not run(state())['sync_ok']


def test_common_browser_lock_prevents_restart(tmp_path):
    run,commands,_,_=harness(tmp_path,probes=(False,))
    with (tmp_path/'output/browser.lock').open('a') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        assert run(state())['recovery']=='deferred_lock'
    assert not any('restart' in c for c in commands)


def test_transient_connection_failure_can_recover_without_container_change(tmp_path):
    run,commands,_,_=harness(tmp_path)
    s=state();s['restart_attempt']={'generation_before':'test','command_completed':False,'requested_at':NOW-1000}
    s['statistics_required_after']=NOW-1000
    result=run(s)
    assert result['recovery']=='browser_ready' and 'restart_attempt' not in s
    assert not any('restart' in c for c in commands)


def test_unknown_route_warning_does_not_restart_healthy_browser(tmp_path):
    run,commands,messages,_=harness(tmp_path)
    path=tmp_path/'output/live';path.mkdir()
    (path/'last-export.json').write_text(json.dumps({'status':'partial','summary':{
        'selected':1,'processed':1,'nonempty':1,'reasons':{'unparsed_live_rows':1},
        'link_audit':{'rows_seen':2,'recognized_rows':1,'unrecognized_rows':1}}}))
    result=run(state())
    assert result['live_ok'] is False
    assert len(messages)==1 and 'unparsed_live_rows' in messages[0]
    assert not any('restart' in command for command in commands)


def test_reminder_has_original_incident_time_and_does_not_claim_new_failure():
    from datetime import datetime,timezone
    from zoneinfo import ZoneInfo
    s=state();messages=[]
    send=lambda text,markup:messages.append(text) or 7
    reasons=['Ожидается статистика реального матча.']
    wd.reconcile_incident(s,'live',reasons,NOW,send)
    wd.reconcile_incident(s,'live',reasons,NOW+wd.REMINDER-1,send)
    assert len(messages)==1 and messages[0].startswith('⚠️')
    wd.reconcile_incident(s,'live',reasons,NOW+wd.REMINDER,send)
    opened=datetime.fromtimestamp(NOW,timezone.utc).astimezone(ZoneInfo('Europe/Moscow')).strftime('%d.%m %H:%M')
    checked=datetime.fromtimestamp(NOW+wd.REMINDER,timezone.utc).astimezone(ZoneInfo('Europe/Moscow')).strftime('%d.%m %H:%M')
    assert len(messages)==2 and messages[-1].startswith('⏳')
    assert 'напоминание об открытом инциденте от '+opened in messages[-1]
    assert 'Проверка: '+checked in messages[-1]
    assert 'обнаружен сбой' not in messages[-1]
    assert s['incidents']['live']['opened_at']==NOW
    assert s['incidents']['live']['reasons']==reasons


def test_pressure_requires_stable_working_set_not_file_cache():
    s=state();m={'generation':'a','working_set_bytes':1400*1024**2,'limit_bytes':2*1024**3}
    assert not wd.pressure_due(s,m,NOW,True)
    assert not wd.pressure_due(s,m,NOW+59,True)
    assert wd.pressure_due(s,m,NOW+60,True)
    assert not wd.pressure_due(s,m,NOW+61,False)
    assert not wd.pressure_due(s,m,NOW+62,True)
    assert not wd.pressure_due(s,dict(m,generation='b'),NOW+122,True)
    assert not wd.pressure_due(s,dict(m,working_set_bytes=500*1024**2,current_bytes=2*1024**3),NOW+200,True)
    assert 'memory_pressure' not in s
    assert not wd.pressure_due(s,{},NOW+300,True)


def test_preventive_restart_is_idle_bounded_and_not_a_failure(tmp_path):
    memory={'generation':'test','oom_kill':0,'working_set_bytes':1400*1024**2,'limit_bytes':2*1024**3}
    run,commands,messages,states=harness(tmp_path,memory=memory)
    s=state()
    assert run(s)['recovery']=='none'
    result=run(s,now=NOW+60,mono=MONO+60)
    assert result['recovery']=='browser_recycled'
    assert not messages and not s.get('statistics_required_after')
    assert result['phase']=='healthy'
    assert sum('restart' in c for c in commands)==1
    run(s,now=NOW+120,mono=MONO+120)
    run(s,now=NOW+180,mono=MONO+180)
    assert sum('restart' in c for c in commands)==1


def test_pressure_never_restarts_active_job_or_bypasses_locks(tmp_path):
    memory={'generation':'test','oom_kill':0,'working_set_bytes':1400*1024**2,'limit_bytes':2*1024**3}
    run,commands,messages,states=harness(tmp_path,memory=memory)
    s=state();run(s)
    states[wd.LIVE]=service(active='activating')
    run(s,now=NOW+60,mono=MONO+60)
    assert not any('restart' in c for c in commands)
    states[wd.LIVE]=service(ended=1060)
    run(s,now=NOW+65,mono=MONO+65)
    with (tmp_path/'output/sync.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        assert run(s,now=NOW+125,mono=MONO+125)['recovery']=='deferred_lock'
    assert not any('restart' in c for c in commands)


def test_delayed_preventive_readiness_does_not_open_statistics_failure(tmp_path):
    run,commands,messages,_=harness(tmp_path)
    s=state();s['restart_attempt']={'generation_before':'old','command_completed':True,'preventive':True,'requested_at':NOW-10}
    result=run(s)
    assert result['browser_ok'] and not s.get('statistics_required_after') and not messages
