import fcntl
from pathlib import Path
import tempfile
import unittest
from browser_lock import acquire

class LockTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.root=Path(self.directory.name)
        (self.root/'output/live').mkdir(parents=True)
    def tearDown(self):self.directory.cleanup()
    def test_live_never_waits_on_busy_history(self):
        first=acquire(self.root,'history',0,wall=lambda:100)
        self.assertIsNotNone(first)
        try:self.assertIsNone(acquire(self.root,'live',0))
        finally:first.close()
        acquire(self.root,'live',0).close()
    def test_history_yields_to_active_live_and_times_out(self):
        with (self.root/'output/live/collect.lock').open('a') as live:
            fcntl.flock(live,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.assertIsNone(acquire(self.root,'history',0,wall=lambda:100))
    def test_history_defers_slot_boundary_then_runs(self):
        clock=[0]
        def sleep(delay):clock[0]+=delay
        handle=acquire(self.root,'history',90,monotonic=lambda:clock[0],wall=lambda:clock[0],sleep=sleep)
        try:self.assertGreaterEqual(clock[0],60);self.assertLess(clock[0],61)
        finally:handle.close()
    def test_history_waits_until_live_releases(self):
        live=(self.root/'output/live/collect.lock').open('a');fcntl.flock(live,fcntl.LOCK_EX)
        clock=[0]
        def sleep(delay):
            clock[0]+=delay
            if clock[0]>.5 and not live.closed:live.close()
        handle=acquire(self.root,'history',2,monotonic=lambda:clock[0],wall=lambda:100,sleep=sleep)
        self.assertIsNotNone(handle);handle.close()
    def test_common_lock_stays_exclusive(self):
        one=acquire(self.root,'live',0)
        try:self.assertIsNone(acquire(self.root,'history',0,wall=lambda:100))
        finally:one.close()

if __name__=='__main__':unittest.main()
