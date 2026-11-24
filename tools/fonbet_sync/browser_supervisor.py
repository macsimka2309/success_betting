"""Own Xvfb/browser processes; only known display artifacts may be removed."""
import os
from pathlib import Path
import re
import signal
import socket
import stat
import struct
import subprocess
import sys
import time


def display_number(display):
    if not re.fullmatch(r':[0-9]{1,3}',display):raise ValueError('invalid_display')
    return display[1:]


def display_ready(display,*,tmp=Path('/tmp')):
    number=display_number(display)
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(.3)
            client.connect(str(tmp/'.X11-unix'/('X'+number)))
            client.sendall(b'l\0'+struct.pack('<HHHHH',11,0,0,0,0))
            return client.recv(8)[:1]==b'\x01'
    except OSError:return False


def display_process_alive(display,proc=Path('/proc')):
    for path in proc.iterdir():
        if not path.name.isdigit():continue
        try:
            if (path/'comm').read_text().strip() not in ('Xvfb','Xorg','X'):continue
            if display.encode() in (path/'cmdline').read_bytes().split(b'\0'):return True
        except FileNotFoundError:continue
        except PermissionError:raise RuntimeError('cannot_check_display_process') from None
    return False


def clear_stale_display(display,*,tmp=Path('/tmp'),ready=display_ready,alive=display_process_alive):
    number=display_number(display)
    if ready(display) or alive(display):raise RuntimeError('display_already_running')
    for path in (tmp/('.X'+number+'-lock'),tmp/'.X11-unix'/('X'+number)):
        try:info=path.lstat()
        except FileNotFoundError:continue
        if info.st_uid!=os.getuid() or not (stat.S_ISREG(info.st_mode) or stat.S_ISSOCK(info.st_mode)):
            raise RuntimeError('unsafe_display_artifact')
        path.unlink()


def stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid,signal.SIGKILL);process.wait(timeout=2)
    try:os.killpg(process.pid,signal.SIGTERM)
    except ProcessLookupError:pass


def supervise(command=None,*,display=':99',viewers=True):
    display_number(display);clear_stale_display(display)
    ready_path=Path('/tmp/fonbet-browser-ready') if command is None else None
    if ready_path:ready_path.unlink(missing_ok=True)
    stop=False;children=[];logs=[];old={}
    def request_stop(signum,frame):
        nonlocal stop
        stop=True
    for signum in (signal.SIGTERM,signal.SIGINT):
        old[signum]=signal.signal(signum,request_stop)
    def launch(argv,**kwargs):
        p=subprocess.Popen(argv,start_new_session=True,**kwargs);children.append(p);return p
    try:
        xserver=launch(['Xvfb',display,'-screen','0','1280x900x24','-nolisten','tcp','-noreset'])
        end=time.monotonic()+10;ready=False
        while not stop and time.monotonic()<end and xserver.poll() is None:
            if display_ready(display):ready=True;break
            time.sleep(.1)
        if stop:return 0
        if not ready:raise RuntimeError('display_start_failed')
        if viewers:
            for path,argv in [('/tmp/x11vnc.log',['x11vnc','-display',display,'-localhost','-rfbport','5900','-nopw','-forever','-shared','-quiet']),
                ('/tmp/novnc.log',['websockify','--web','/usr/share/novnc','6080','localhost:5900'])]:
                log=open(path,'a');logs.append(log);launch(argv,stdout=log,stderr=log)
        browser=launch(command or ['node','/work/browser.cjs'])
        while not stop and all(p.poll() is None for p in children):time.sleep(.1)
        if stop:return 0
        return browser.returncode if browser.returncode is not None else 1
    finally:
        # Browser gets TERM first so it can close the persistent context before Xvfb.
        for process in reversed(children):stop_process(process)
        if ready_path:ready_path.unlink(missing_ok=True)
        for log in logs:log.close()
        for signum,handler in old.items():signal.signal(signum,handler)


if __name__=='__main__':
    try:raise SystemExit(supervise())
    except (OSError,RuntimeError,ValueError,subprocess.SubprocessError) as exc:
        # Whitelisted local errors, never subprocess arguments or browser/account data.
        reason=str(exc) if str(exc) in ('invalid_display','display_already_running','cannot_check_display_process','unsafe_display_artifact','display_start_failed') else type(exc).__name__
        print('browser_supervisor_failed:'+reason,file=sys.stderr)
        raise SystemExit(1)
