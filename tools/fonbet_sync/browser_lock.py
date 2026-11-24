"""Serialize expensive reads; live never queues. No credentials/profile contents read."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import time


def try_lock(path):
    handle=Path(path).open('a')
    try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:handle.close();return None
    return handle


def acquire(root,mode,wait_seconds=90,*,monotonic=time.monotonic,wall=time.time,sleep=time.sleep):
    root=Path(root);deadline=monotonic()+wait_seconds
    while True:
        # At simultaneous timer boundaries the live job gets a 60-second head start.
        priority=None
        if mode=='history' and wall()%300>=60:
            priority=try_lock(root/'output/live/collect.lock')
        allowed=mode=='live' or priority is not None
        try:
            handle=try_lock(root/'output/browser.lock') if allowed else None
        finally:
            if priority:priority.close()
        if handle is not None:return handle
        if mode=='live' or monotonic()>=deadline:return None
        sleep(min(.2,max(0,deadline-monotonic())))


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',required=True)
    parser.add_argument('--mode',choices=['live','history'],required=True)
    parser.add_argument('--wait-seconds',type=float,default=90)
    parser.add_argument('command',nargs=argparse.REMAINDER)
    args=parser.parse_args(argv)
    command=args.command[1:] if args.command[:1]==['--'] else args.command
    if not command or not 0<=args.wait_seconds<=90:parser.error('command and wait budget required')
    os.umask(0o077)
    (Path(args.root)/'output/live').mkdir(parents=True,exist_ok=True)
    requested=time.monotonic()
    handle=acquire(args.root,args.mode,args.wait_seconds)
    if handle is None:
        print(json.dumps({'status':'skipped_browser_busy' if args.mode=='live' else 'browser_wait_timeout'}),flush=True)
        return 75
    acquired=time.monotonic()
    print(json.dumps({'browser_gate':'acquired','mode':args.mode,'waited_seconds':round(acquired-requested,3)}),flush=True)
    try:
        # Preserve the lock for commands that retain descriptors; the container lock
        # additionally protects an orphaned docker exec after host clients disappear.
        return subprocess.run(command,pass_fds=(handle.fileno(),)).returncode
    finally:
        print(json.dumps({'browser_gate':'released','mode':args.mode,'held_seconds':round(time.monotonic()-acquired,3)}),flush=True)
        handle.close()


if __name__=='__main__':raise SystemExit(main())
