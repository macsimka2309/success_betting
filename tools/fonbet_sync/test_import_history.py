import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from import_history import parse_coupon

class ParseTests(unittest.TestCase):
    def setUp(self):
        self.raw = dict(id='12345678901',type='Одинар',legs=[['09.11.2026 22:00','LIVE 1:1 А – Б','> 2.5','1.73','2:1','Выигрышный прогноз']],bar=['Одинар','500','1.73','09.11.2026 23:37:01','Рассчитана 10.11.2026 00:00:42'])
    def test_placement_date_and_unit_profit(self):
        r=parse_coupon(self.raw)
        self.assertTrue(r['created'].startswith('2026-11-09'))
        self.assertTrue(r['settled'].startswith('2026-11-10'))
        self.assertEqual(r['profit'],'0.73')
    def test_individual_total(self):
        self.raw['legs'][0][2]='Б > 1.5'
        self.assertEqual(parse_coupon(self.raw)['market'],'ITB2')
    def test_new_fonbet_status_labels(self):
        for label,status,profit in [('Выигрыш','won','0.73'),('Проигрыш','lost','-1')]:
            self.raw['legs'][0][-1]=label
            result=parse_coupon(self.raw)
            self.assertEqual(result['status'],status);self.assertEqual(result['profit'],profit)
            self.assertIsNotNone(result['settled'])

    def test_unknown_status_rejected(self):
        self.raw['legs'][0][-1]='Продан'
        with self.assertRaises(ValueError):parse_coupon(self.raw)
    def test_quarter_line_rejected(self):
        self.raw['legs'][0][2]='> 2.25'
        with self.assertRaises(ValueError):parse_coupon(self.raw)
    def test_freebet_single_and_whitespace(self):
        for label in ('Одинар\nФрибет', ' Одинар  Фрибет ', 'Одинар\r\nФрибет'):
            self.raw['type']=label
            result=parse_coupon(self.raw)
            self.assertTrue(result['is_freebet'])
            self.assertEqual(result['profit'],'0.73')
        self.raw['type']='Одинар'
        self.assertFalse(parse_coupon(self.raw)['is_freebet'])

    def test_freebet_results_use_existing_unit_convention(self):
        self.raw['type']='Одинар\nФрибет'
        for label,status,profit in [('Проигрыш','lost','-1'),('Возврат','push','0'),('Не рассчитан','open',None)]:
            self.raw['legs'][0][-1]=label
            result=parse_coupon(self.raw)
            self.assertEqual((result['status'],result['profit']),(status,profit))
            self.assertTrue(result['is_freebet'])

    def test_freebet_marker_does_not_allow_unknown_type_or_multiple_legs(self):
        for label in ('Экспресс\nФрибет','Фрибет','Одинар\nНеизвестный бонус'):
            self.raw['type']=label
            with self.assertRaises(ValueError):parse_coupon(self.raw)
        self.raw['type']='Одинар\nФрибет'
        self.raw['legs'].append(copy.deepcopy(self.raw['legs'][0]))
        with self.assertRaises(ValueError):parse_coupon(self.raw)

    def test_multiple_legs_rejected(self):
        self.raw['legs'].append(copy.deepcopy(self.raw['legs'][0]))
        with self.assertRaises(ValueError):parse_coupon(self.raw)

class ReportTests(unittest.TestCase):
    def setUp(self):
        self.fixture = dict(id='123', date='09.11', type='Одинар',
                            legs=[['09.11.2026 22:00', 'LIVE 1:1 А – Б', '> 2.5', '1.73', '2:1', 'Выигрыш']],
                            bar=['Одинар', '500', '1.73', '09.11.2026 23:37:01', 'Рассчитана 10.11.2026 00:00:42'])

    def run_import(self, rows, *, complete=True, apply=False):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'export.json'
            path.write_text(json.dumps({'complete': complete, 'dates': ['09.11'], 'rows': rows}))
            args = [sys.executable, str(Path(__file__).with_name('import_history.py')),
                    '--day', '2026-11-09', '--input', str(path)]
            if apply:
                args.append('--apply')
            return subprocess.run(args, capture_output=True, text=True)

    def test_recognized_over_with_damaged_data_is_still_reported(self):
        for index, value in ((2, '> ?'), (3, 'not odds'), (3, None), (3, 'Infinity'),
                             (4, 'неизвестно'), (5, 'Продан')):
            with self.subTest(field=index, value=value):
                coupon = copy.deepcopy(self.fixture)
                coupon['legs'][0][index] = value
                process = self.run_import([coupon])
                self.assertEqual(process.returncode, 2, process.stderr)
                report = json.loads(process.stdout)
                self.assertEqual((report['status'], report['found'], report['skipped_count']), ('partial', 1, 1))
                self.assertEqual(report['skipped'][0]['coupon'], coupon['id'])
                self.assertFalse(report['applied'])

    def test_recognized_over_with_missing_id_is_still_reported(self):
        del self.fixture['id']
        process = self.run_import([self.fixture])
        self.assertEqual(process.returncode, 2, process.stderr)
        report = json.loads(process.stdout)
        self.assertEqual((report['status'], report['found'], report['skipped_count']), ('partial', 1, 1))
        self.assertIn('номер купона', report['skipped'][0]['detail'])

    def test_ignored_markets_are_absent_even_from_applied_empty_report(self):
        rows = []
        for coupon_type, prediction in [('Одинар', '< 2.5'), ('Одинар\nФрибет', 'Б > 1.5'),
                                        ('Экспресс', '> 2.5'), ('Одинар', 'П1')]:
            coupon = copy.deepcopy(self.fixture)
            coupon['type'] = coupon_type
            coupon['legs'][0][2] = prediction
            coupon['legs'][0][-1] = 'Продан'
            rows.append(coupon)
        process = self.run_import(rows, apply=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout), dict(status='complete', skipped_count=0, day='2026-11-09',
                          found=0, applied=True, freebet_count=0, results=[], skipped=[]))

    def test_incomplete_export_still_fails_with_no_eligible_coupons(self):
        process = self.run_import([], complete=False, apply=True)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn('неполный обход истории', process.stderr)
        self.assertEqual(process.stdout, '')

if __name__=='__main__':unittest.main()
