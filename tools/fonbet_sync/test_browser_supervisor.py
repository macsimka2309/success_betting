from pathlib import Path
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from browser_supervisor import clear_stale_display,display_process_alive,display_ready,display_number

class DisplayTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.root=Path(self.directory.name)
        (self.root/'.X11-unix').mkdir()
    def tearDown(self):self.directory.cleanup()
    def test_stale_lock_with_reused_non_x_pid_is_removed(self):
        proc=self.root/'proc';p=proc/str(os.getpid());p.mkdir(parents=True)
        (p/'comm').write_text('python3');(p/'cmdline').write_bytes(b'python3\0')
        lock=self.root/'.X88-lock';lock.write_text(str(os.getpid()))
        clear_stale_display(':88',tmp=self.root,ready=lambda d:False,alive=lambda d:display_process_alive(d,proc))
        self.assertFalse(lock.exists())
    def test_existing_x_server_is_preserved(self):
        lock=self.root/'.X88-lock';lock.write_text('123')
        for ready,alive in [(True,False),(False,True)]:
            with self.assertRaisesRegex(RuntimeError,'already_running'):
                clear_stale_display(':88',tmp=self.root,ready=lambda d:ready,alive=lambda d:alive)
            self.assertTrue(lock.exists())
    def test_symlink_artifact_is_never_deleted(self):
        target=self.root/'private';target.write_text('keep')
        (self.root/'.X88-lock').symlink_to(target)
        with self.assertRaisesRegex(RuntimeError,'unsafe'):
            clear_stale_display(':88',tmp=self.root,ready=lambda d:False,alive=lambda d:False)
        self.assertEqual(target.read_text(),'keep')
    def test_invalid_display_and_dead_socket(self):
        for value in [':99/../../etc',':1;exit','127.0.0.1:99',':9999']:
            with self.assertRaises(ValueError):display_number(value)
        self.assertFalse(display_ready(':88',tmp=self.root))
    @unittest.skipUnless(shutil.which('Xvfb'),'real Xvfb requires Linux runtime')
    def test_two_real_starts_and_sigterm_cleanup(self):
        if display_ready(':88') or display_process_alive(':88'):self.skipTest('display 88 busy')
        lock=Path('/tmp/.X88-lock');lock.write_text(str(os.getpid()))
        code="import sys;from browser_supervisor import supervise;sys.exit(supervise([sys.executable,'-c','import time;time.sleep(30)'],display=':88',viewers=False))"
        for _ in range(5):
            p=subprocess.Popen([sys.executable,'-c',code],cwd=Path(__file__).parent,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            try:
                end=time.monotonic()+12
                while time.monotonic()<end and p.poll() is None and not display_ready(':88'):time.sleep(.1)
                self.assertTrue(display_ready(':88'))
                p.send_signal(signal.SIGTERM);self.assertEqual(p.wait(timeout=10),0)
                self.assertFalse(display_ready(':88'));self.assertFalse(lock.exists())
            finally:
                if p.poll() is None:
                    p.send_signal(signal.SIGTERM)
                    try:p.wait(timeout=10)
                    except subprocess.TimeoutExpired:p.kill();p.wait()

if __name__=='__main__':unittest.main()
