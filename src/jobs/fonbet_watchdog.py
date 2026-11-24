"""Health monitoring and bounded recovery; no .env, profile, credentials or cookies are read."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import time
from zoneinfo import ZoneInfo

from src.jobs.alert_dispatch import telegram_sender

ROOT=Path('/opt/apps/fonbet-sync-pilot')
CONTAINER='fonbet-browser-pilot'
LIVE='football-fonbet-live.service'
SYNC='football-fonbet-sync.service'
REMINDER=1800
COOLDOWN=900
MAX_RESTARTS=3


def execute(argv,timeout=15):
    """Never include stdout/stderr/arguments in an exception (may contain sensitive diagnostics)."""
    try:return subprocess.run(argv,text=True,capture_output=True,timeout=timeout)
    except (subprocess.TimeoutExpired,OSError):return None


def service_state(unit,runner=execute):
    proc=runner(['systemctl','show',unit,'-p','ActiveState','-p','Result','-p','ExecMainExitTimestampMonotonic','-p','ExecMainStartTimestampMonotonic'],10)
    if proc is None or proc.returncode:return {'ActiveState':'unknown','Result':'unknown'}
    return dict(line.split('=',1) for line in proc.stdout.splitlines() if '=' in line)


def running(state):return state.get('ActiveState') in ('active','activating','reloading')


def healthy_live(state,monotonic):
    if state.get('Result')!='success':return False
    if running(state):return True
    ended=int(state.get('ExecMainExitTimestampMonotonic') or 0)/1_000_000
    return ended>0 and 0<=monotonic-ended<=900


def browser_healthy(runner=execute):
    proc=runner(['sudo','-n','docker','exec',CONTAINER,'node','/work/live/browser_probe.cjs'],18)
    return proc is not None and proc.returncode==0


def browser_memory(runner=execute):
    proc=runner(['sudo','-n','docker','exec',CONTAINER,'node','/work/live/memory.cjs'],12)
    if proc is None or proc.returncode:return None
    try:
        value=json.loads(proc.stdout)
        if not isinstance(value,dict) or not isinstance(value.get('oom_kill'),int) or not value.get('generation'):return None
        return {k:value.get(k) for k in ('generation','oom_kill','current_bytes','limit_bytes','cgroup_peak_bytes','working_set_bytes','anon_bytes')}
    except (ValueError,TypeError):return None


def pressure_due(state,memory,now,idle):
    working=(memory or {}).get('working_set_bytes');limit=(memory or {}).get('limit_bytes')
    high=(idle and type(working) is int and type(limit) is int and limit>0
          and working>=512*1024*1024 and working>=limit*0.60)
    if not high:
        state.pop('memory_pressure',None);return False
    generation=memory.get('generation')
    previous=state.get('memory_pressure')
    if not previous or previous.get('generation')!=generation or now<previous['since']:
        state['memory_pressure']={'generation':generation,'since':now};return False
    return now-previous['since']>=60


def new_oom(state,current):
    if current is None:return 0
    previous=state.get('browser_memory')
    state['browser_memory']=current
    if previous is None:return 0 # Baseline: a historic OOM flag is not a new incident.
    old=previous.get('oom_kill',0) if previous.get('generation')==current['generation'] else 0
    return max(0,current['oom_kill']-old)


@contextmanager
def recovery_locks(root=ROOT):
    handles=[]
    try:
        for path in (root/'output/live/collect.lock',root/'output/sync.lock',root/'output/browser.lock'):
            path.parent.mkdir(parents=True,exist_ok=True)
            handle=path.open('a');handles.append(handle)
            try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                yield False;return
        yield True
    finally:
        for handle in reversed(handles):handle.close()


def save_state(path,state):
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp')
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as f:
        json.dump(state,f);f.flush();os.fsync(f.fileno())
    os.replace(temp,path)
    directory=os.open(path.parent,os.O_RDONLY)
    try:os.fsync(directory)
    finally:os.close(directory)


def load_state(path):
    try:
        with path.open() as f:state=json.load(f)
        if not isinstance(state,dict):raise ValueError()
        return state
    except FileNotFoundError:return {'incidents':{},'restarts':[],'retries':{}}
    except (ValueError,TypeError):
        # Corrupt state must not reset rate limits and trigger unbounded restarts.
        raise RuntimeError('watchdog_state_invalid') from None


def restart_allowed(state,now):
    recent=[t for t in state.get('restarts',[]) if now-t<3600]
    return len(recent)<MAX_RESTARTS and (not recent or now-max(recent)>=COOLDOWN)


def notify(send,text):
    try:return bool(send(text,None))
    except Exception:return False


def reconcile_incident(state,key,reasons,now,send):
    incidents=state.setdefault('incidents',{})
    incident=incidents.get(key)
    titles={'live':'Live-сбор Fonbet','sync':'Сверка ставок Fonbet'}
    if reasons:
        if incident is None:
            incident={'opened_at':now,'last_notice':0,'notified':False};incidents[key]=incident
        incident['reasons']=reasons
        if now-incident['last_notice']>=REMINDER:
            # Save an attempt timestamp even for an unknown outcome: do not spam Telegram.
            repeated=bool(incident['last_notice'])
            incident['last_notice']=now
            stamp=datetime.fromtimestamp(now,timezone.utc).astimezone(ZoneInfo('Europe/Moscow')).strftime('%d.%m %H:%M')
            if repeated:
                opened=datetime.fromtimestamp(incident['opened_at'],timezone.utc).astimezone(ZoneInfo('Europe/Moscow')).strftime('%d.%m %H:%M')
                text=f"⏳ {titles[key]}: напоминание об открытом инциденте от {opened} МСК.\nПроверка: {stamp} МСК.\n"
            else:
                text=f"⚠️ {titles[key]}: обнаружен сбой ({stamp} МСК).\n"
            text+='\n'.join(reasons)
            text+='\nАвтовосстановление ограничено: не чаще раза в 15 минут, максимум 3 перезапуска браузера в час.'
            incident['notified']=notify(send,text) or incident['notified']
        return
    if incident:
        if not incident.get('notified'):
            # A notice might have reached Telegram before its response was lost.
            if now-incident['last_notice']<REMINDER:return
        if now-incident.get('last_recovery_attempt',0)<60:return
        incident['last_recovery_attempt']=now
        if notify(send,f"✅ {titles[key]} восстановлен. Проверка браузера и штатного задания успешна."):
            del incidents[key]


def completed_since(state,opened,now,monotonic):
    ended=int(state.get('ExecMainExitTimestampMonotonic') or 0)/1_000_000
    return not running(state) and state.get('Result')=='success' and ended>0 and now-(monotonic-ended)>=opened


def sync_reasons(state,root,now):
    reasons=[]
    if state.get('Result') not in ('success',None):reasons.append('Ночной запуск завершился ошибкой или неполной сверкой.')
    local=datetime.fromtimestamp(now,timezone.utc).astimezone(ZoneInfo('Europe/Moscow'))
    if not running(state):
        try:result=json.loads((root/'output/last-result.json').read_text())
        except (OSError,ValueError):result={}
        expected=(local.date()-timedelta(days=1)).isoformat()
        if result.get('day')==expected:
            skipped=len(result.get('skipped',[]))+sum(r.get('action')=='skipped' for r in result.get('results',[]))
            if skipped or result.get('status')=='partial':
                reasons.append(f'Сверка ставок неполная: пропущено купонов {skipped}; найдено {result.get("found","?")}. Требуется разбор отчёта.')
        if local.hour>=2 and (result.get('day')!=expected or not result.get('applied')):
            reasons.append('Нет подтверждённой сверки ставок за предыдущий день после 02:00 МСК.')
    return reasons


def monitor(state,*,now,monotonic,root=ROOT,send,runner=execute,probe=browser_healthy,locks=recovery_locks,sleep=time.sleep,persist=lambda s:None,memory_reader=browser_memory):
    live=service_state(LIVE,runner);sync=service_state(SYNC,runner)
    browser_ok=probe(runner)
    memory=memory_reader(runner)
    oom_delta=new_oom(state,memory)
    if oom_delta:
        state['oom_restart_pending']=True
        state['statistics_required_after']=now
        state.pop('restart_attempt',None)
        state.pop('statistics_required_generation',None)
    # A successful restart command and readiness are separate events. A delayed
    # healthy probe must clear the restart request, not restart a working browser.
    attempt=state.get('restart_attempt')
    ready_later=False
    if attempt and browser_ok:
        changed=memory is not None and memory.get('generation')!=attempt.get('generation_before')
        if attempt.get('command_completed') or changed or not state.get('oom_restart_pending'):
            state['oom_restart_pending']=False
            if not attempt.get('preventive'):
                state['statistics_required_after']=now
                state['statistics_required_generation']=memory.get('generation') if memory else None
            state.pop('restart_attempt',None)
            ready_later=True
    live_reasons=[]
    if state.get('oom_restart_pending'):
        live_reasons.append('Новый OOM Chromium: требуется контролируемый перезапуск браузера.')
    if memory is None:
        live_reasons.append('Не удалось проверить счётчик OOM и память браузера.')
    last_export={}
    if not browser_ok:state.setdefault('statistics_required_after',now)
    if not browser_ok:live_reasons.append('Нет рабочего подключения к серверному Chromium (возможны OOM или зависание).')
    if not healthy_live(live,monotonic):live_reasons.append('Штатный live-сбор завершился ошибкой либо более 15 минут нет завершённого успешного цикла.')
    if not running(live):
        try:
            last_export=json.loads((root/'output/live/last-export.json').read_text())
            if last_export.get('status') in ('partial','failed'):
                summary=last_export.get('summary',{})
                live_reasons.append(f"Последний live-цикл неполный: обработано {summary.get('processed','?')} из {summary.get('selected','?')}; ошибки {summary.get('reasons',{})}.")
                if (summary.get('memory',{}).get('oom_kill_delta') or 0)>0 or any(d.get('code') in ('page_crashed','browser_closed') for d in summary.get('errors',[])):
                    state.setdefault('statistics_required_after',now)
                    # One controlled restart for this failed export, never on every tick.
                    run_id=last_export.get('run_id')
                    if run_id and run_id!=state.get('last_crashed_run'):
                        state['last_crashed_run']=run_id
                        try:exported=datetime.fromisoformat(last_export['finished_at']).timestamp()
                        except (KeyError,ValueError,TypeError):exported=now
                        # An old export may predate a restart already performed.
                        if exported>state.get('last_restart_requested_at',0):state['oom_restart_pending']=True
        except FileNotFoundError:pass
        except (OSError,ValueError):live_reasons.append('Не удалось прочитать итоговый отчёт live-сбора.')
    required=state.get('statistics_required_after')
    if required:
        try:finished=datetime.fromisoformat(last_export['started_at']).timestamp()
        except (KeyError,ValueError,TypeError):finished=0
        summary=last_export.get('summary',{})
        required_generation=state.get('statistics_required_generation')
        generation=summary.get('memory',{}).get('before',{}).get('generation')
        if (browser_ok and not state.get('oom_restart_pending') and not state.get('restart_attempt') and not running(live)
            and finished>=required and (not required_generation or generation==required_generation)
            and last_export.get('status')=='complete' and summary.get('nonempty',0)>0):
            state.pop('statistics_required_after',None)
            state.pop('statistics_required_generation',None)
        else:
            live_reasons.append('После сбоя ожидается успешное чтение статистики реального матча; пустой цикл не подтверждает восстановление.')
    history_reasons=sync_reasons(sync,root,now)
    for key,current,reasons in [('live',live,live_reasons),('sync',sync,history_reasons)]:
        incident=state.get('incidents',{}).get(key)
        if incident and not reasons and not completed_since(current,incident['opened_at'],now,monotonic):
            reasons.append('Ожидается успешное завершение проверочного задания после сбоя.')
    # Rate-limit state is persisted before a side effect or Telegram request.
    def safe_send(text,markup):
        persist(state)
        return send(text,markup)
    reconcile_incident(state,'live',live_reasons,now,safe_send)
    reconcile_incident(state,'sync',history_reasons,now,safe_send)
    report={'browser_ok':browser_ok,'live_ok':not live_reasons,'sync_ok':not history_reasons,'recovery':'browser_ready' if ready_later else 'none','memory':memory,'new_oom_kills':oom_delta}
    maintenance=pressure_due(state,memory,now,
        browser_ok and not running(live) and not running(sync) and not live_reasons
        and not state.get('statistics_required_after') and not state.get('restart_attempt')
        and not state.get('incidents',{}).get('live'))
    if not browser_ok or state.get('oom_restart_pending') or maintenance:
        if not restart_allowed(state,now):report['recovery']='rate_limited'
        elif running(live) or running(sync):report['recovery']='deferred_busy'
        else:
            with locks(root) as acquired:
                if not acquired:report['recovery']='deferred_lock'
                else:
                    # Reserve even failed attempts. Preserve the browser profile and memory limits.
                    state.setdefault('restarts',[]).append(now)
                    state['last_restart_requested_at']=now
                    if not maintenance:state['statistics_required_after']=now
                    state['restart_attempt']={'requested_at':now,'generation_before':memory.get('generation') if memory else None,'command_completed':False,'preventive':maintenance}
                    persist(state)
                    proc=runner(['sudo','-n','docker','restart','-t','15',CONTAINER],35)
                    report['recovery']='restart_failed'
                    if proc is not None and proc.returncode==0:
                        state['restart_attempt']['command_completed']=True;persist(state)
                        report['recovery']='browser_starting'
                        for attempt in range(3):
                            if probe(runner):
                                report['recovery']='browser_restarted';state['oom_restart_pending']=False
                                latest=memory_reader(runner)
                                if latest:state['browser_memory']=latest
                                if not maintenance:
                                    state['statistics_required_generation']=latest.get('generation') if latest else None
                                    # Never confirm crash recovery using a pre-restart scan.
                                    state['statistics_required_after']=now
                                else:
                                    report['recovery']='browser_recycled'
                                    state.pop('memory_pressure',None)
                                state.pop('restart_attempt',None)
                                break
                            sleep(2)
    # Start jobs only after releasing host locks, or their wrappers would skip collection.
    if report['recovery'] in ('browser_restarted','browser_recycled'):browser_ok=True
    if state.get('restart_attempt'):browser_ok=False
    report['browser_ok']=browser_ok
    for key,unit,reasons,current in [('live',LIVE,live_reasons,live),('sync',SYNC,history_reasons,sync)]:
        last=state.setdefault('retries',{}).get(key,0)
        if browser_ok and reasons and not running(current) and now-last>=1800:
            state['retries'][key]=now;persist(state)
            proc=runner(['sudo','-n','systemctl','start','--no-block',unit],10)
            report[key+'_retry']='requested' if proc is not None and proc.returncode==0 else 'failed'
    report['phase']=('restart_required' if state.get('oom_restart_pending') and not state.get('restart_attempt')
        else 'waiting_browser' if state.get('restart_attempt')
        else 'waiting_statistics' if state.get('statistics_required_after') else 'healthy')
    state['restarts']=[t for t in state.get('restarts',[]) if now-t<3600]
    persist(state)
    return report


def main(argv=None):
    parser=argparse.ArgumentParser(description='Fonbet failure notifications and bounded browser recovery')
    parser.add_argument('--check-only',action='store_true',help='read health only; do not notify/restart/start jobs')
    args=parser.parse_args(argv)
    os.umask(0o077)
    try:
        directory=ROOT/'output/watchdog';directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        with (directory/'monitor.lock').open('a') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                print(json.dumps({'skipped_previous_running':True}));return 0
            if args.check_only:
                print(json.dumps({'browser_ok':browser_healthy(),'live':service_state(LIVE),'sync':service_state(SYNC),'memory':browser_memory()}));return 0
            token,chat=os.environ.get('TELEGRAM_BOT_TOKEN'),os.environ.get('TELEGRAM_CHAT_ID')
            if not token or not chat:raise RuntimeError('telegram_environment_missing')
            path=directory/'state.json';state=load_state(path)
            report=monitor(state,now=time.time(),monotonic=time.monotonic(),send=telegram_sender(token,chat,os.environ.get('TELEGRAM_PROXY') or None),persist=lambda s:save_state(path,s))
            print(json.dumps(report));return 0
    except Exception as exc:
        print(json.dumps({'watchdog_error':type(exc).__name__}));return 1

if __name__=='__main__':raise SystemExit(main())
