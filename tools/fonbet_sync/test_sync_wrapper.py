import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

@unittest.skipUnless(shutil.which('flock'),'wrapper requires Linux flock')
class WrapperTests(unittest.TestCase):
    def test_partial_report_survives_nonzero_exit_and_does_not_mark_success(self):
        self.check_wrapper('> 2.5', 2, 'partial', 1, 1)

    def test_only_ignored_markets_marks_success_without_partial_report(self):
        self.check_wrapper('< 2.5', 0, 'complete', 0, 0)

    def test_incomplete_export_marks_error_without_import_report(self):
        self.check_wrapper('< 2.5', 1, None, 0, 0, complete=False)

    def check_wrapper(self, prediction, expected_exit, status, found, skipped, *, complete=True):
        source=Path(__file__).parent
        wrapper=source/'run_fonbet_sync.sh'
        if not wrapper.exists():wrapper=source.parents[1]/'deploy/run_fonbet_sync.sh'
        fixture=dict(id='123',date='09.11',type='Одинар',legs=[['09.11.2026 22:00','LIVE 1:1 А – Б',prediction,'1.73','2:1','Продан']],bar=['Одинар','500','1.73','09.11.2026 23:37:01','Рассчитана 10.11.2026 00:00:42'])
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'bin').mkdir();(root/'output').mkdir()
            shutil.copy(source/'import_history.py',root/'import_history.py')
            (root/'browser_lock.py').write_text('raise SystemExit(0)\n')
            (root/'fixture.json').write_text(json.dumps({'complete':complete,'dates':['09.11'],'rows':[fixture]}))
            fake=root/'bin/sudo'
            fake.write_text('#!/usr/bin/env python3\nimport sys,os\nfrom pathlib import Path\nif "cat" in sys.argv:print((Path(os.environ["FONBET_SYNC_ROOT"])/"fixture.json").read_text())\nelif "-i" in sys.argv:sys.stdin.read()\n')
            fake.chmod(0o700)
            env=dict(os.environ,PATH=str(root/'bin')+os.pathsep+os.environ['PATH'],FONBET_SYNC_ROOT=str(root))
            p=subprocess.run(['bash',str(wrapper),'2026-11-09'],env=env,capture_output=True,text=True,timeout=20)
            self.assertEqual(p.returncode,expected_exit,p.stderr)
            report_file = root/'output/last-result.json'
            if status is None:
                self.assertFalse(report_file.exists())
            else:
                report=json.loads(report_file.read_text())
                self.assertEqual((report['status'], report['found'], report['skipped_count']), (status, found, skipped))
            self.assertEqual((root/'output/last-success.txt').exists(), expected_exit == 0)
            self.assertEqual((root/'output/last-error.txt').exists(), expected_exit != 0)

if __name__=='__main__':unittest.main()
