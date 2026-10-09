import json
import tempfile
import unittest
import zipfile
from pathlib import Path
import sys

# Support hosts/developers invoking this file directly instead of unittest discovery.
# This remains a test runner; production must launch main.py or -m wb_backup.
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datetime import date, datetime, time
from unittest.mock import patch, Mock
import httpx
from openpyxl import load_workbook
from wb_backup.config import Account, Config, MSK
from wb_backup.storage import Store
from wb_backup.api import WB, APIError, rows
from wb_backup.export import write_table, package
from wb_backup.join_chunks import join
from wb_backup.collector import Collector, windows
from wb_backup.__main__ import authorized, schedule_due, Bot


def config(root):
    return Config('TEST_SECRET', frozenset({123}), frozenset({123}),
                  (Account('1', 'Первый', 'WB_SECRET'), Account('2', 'Второй', 'WB_SECRET2')),
                  root, time(10), 30, date(2024, 1, 29), date(2019, 1, 1), 2 * 1024 * 1024)


class SecurityTests(unittest.TestCase):
    def test_private_whitelist_and_chat_identity(self):
        msg = {'from': {'id': 123}, 'chat': {'id': 123, 'type': 'private'}}
        self.assertTrue(authorized(msg, {123}))
        self.assertFalse(authorized(msg, {456}))
        self.assertFalse(authorized({**msg, 'chat': {'id': 123, 'type': 'group'}}, {123}))
        self.assertFalse(authorized({**msg, 'chat': {'id': 456, 'type': 'private'}}, {123}))
        self.assertFalse(authorized({}, {123}))

    def test_unauthorized_does_not_enqueue_or_send(self):
        bot = object.__new__(Bot)
        bot.config = Mock(allowed={123})
        bot.store = Mock()
        bot.telegram = Mock()
        bot.handle({'from': {'id': 456}, 'chat': {'id': 456, 'type': 'private'}, 'text': '/all'})
        bot.store.enqueue.assert_not_called()
        bot.telegram.message.assert_not_called()

    def test_10am_moscow_and_catchup(self):
        self.assertFalse(schedule_due(datetime(2026, 10, 9, 9, 59, tzinfo=MSK), time(10), None))
        self.assertTrue(schedule_due(datetime(2026, 10, 9, 10, tzinfo=MSK), time(10), None))
        self.assertTrue(schedule_due(datetime(2026, 10, 9, 15, tzinfo=MSK), time(10), '2026-10-08'))
        self.assertFalse(schedule_due(datetime(2026, 10, 9, 15, tzinfo=MSK), time(10), '2026-10-09'))


class StorageTests(unittest.TestCase):
    def test_duplicate_and_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder))
            job, created = store.enqueue('full', {123})
            self.assertTrue(created)
            self.assertEqual(store.enqueue('daily', {123}), (job, False))
            self.assertEqual(store.next_job()['id'], job)
            store.db.close()
            resumed = Store(Path(folder))
            self.assertEqual(resumed.next_job()['id'], job)
            resumed.mark_delivered(job, 123, 'part.zip')
            self.assertTrue(resumed.delivered(job, 123, 'part.zip'))
            resumed.db.close()


class APITests(unittest.TestCase):
    def test_pacing_retry_and_durable_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            wb = WB(Account('1', 'Test', 'SECRET'), Path(folder))
            calls = []
            def response(request):
                calls.append(request)
                return httpx.Response(429, headers={'Retry-After': '1'}) if len(calls) == 1 else httpx.Response(200, json=[{'rrdId': 1}])
            wb.client = httpx.Client(transport=httpx.MockTransport(response))
            with patch('wb_backup.api.time.sleep') as sleep:
                self.assertEqual(wb.request('finance', 'POST', '/test', body={}), [{'rrdId': 1}])
                sleep.assert_called()
            self.assertEqual(wb.request('finance', 'POST', '/test', body={}), [{'rrdId': 1}])
            self.assertEqual(len(calls), 2)
            self.assertNotIn('SECRET', next(Path(folder).glob('*.json')).read_text())
            wb.client.close()

    def test_http_error_is_not_empty_success(self):
        with tempfile.TemporaryDirectory() as folder:
            wb = WB(Account('1', 'Test', 'SECRET'), Path(folder))
            wb.client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403, text='SECRET')))
            with self.assertRaises(APIError) as caught:
                wb.request('finance', 'GET', '/test')
            self.assertEqual(caught.exception.status, 403)
            self.assertNotIn('SECRET', str(caught.exception))
            self.assertFalse(list(Path(folder).glob('*.json')))
            wb.client.close()

    def test_schema_drift_fails(self):
        with self.assertRaises(APIError):
            rows({'data': {'changed': []}}, 'data', 'items')

    def test_pending_status_is_refreshed(self):
        with tempfile.TemporaryDirectory() as folder:
            wb = WB(Account('1', 'Test', 'SECRET'), Path(folder))
            states = iter(['pending', 'done'])
            wb.client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'status': next(states)})))
            with patch('wb_backup.api.time.sleep'):
                self.assertEqual(wb.request('analytics', 'GET', '/status')['status'], 'pending')
                self.assertEqual(wb.request('analytics', 'GET', '/status', fresh=True)['status'], 'done')
            wb.client.close()


class ExportTests(unittest.TestCase):
    def test_identifiers_literal_text_money_and_sheet_split(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'data.xlsx'
            records = [{'reportId': 1234567890123456789, 'barcode': '0012345', 'title': '=HYPERLINK("x")',
                        'retailAmount': '123.45', 'quantity': 2}, {'reportId': 2, 'extra': 'late field'}, {'reportId': 3}]
            write_table(path, records, {'Кабинет': 'Test'}, max_rows=2)
            wb = load_workbook(path)
            self.assertEqual(wb['Данные 1']['A2'].value, '1234567890123456789')
            self.assertEqual(wb['Данные 1']['B2'].value, '0012345')
            self.assertEqual(wb['Данные 1']['C2'].data_type, 's')
            self.assertEqual(wb['Данные 1']['D2'].value, 123.45)
            self.assertEqual(wb['Данные 2']['A2'].value, '3')
            wb.close()

    def test_large_files_restore_and_size_bound(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'archive'
            root.mkdir()
            original = bytes(range(256)) * 10000
            (root / 'original.bin').write_bytes(original)
            archives = package(root, Path(folder) / 'parts', 2 * 1024 * 1024)
            self.assertGreater(len(archives), 1)
            extracted = Path(folder) / 'restored'
            for archive in archives:
                self.assertLessEqual(archive.stat().st_size, 2 * 1024 * 1024)
                with zipfile.ZipFile(archive) as z:
                    z.extractall(extracted)
            join(extracted)
            self.assertEqual((extracted / 'original.bin').read_bytes(), original)
            self.assertTrue((extracted / 'checksums.json').exists())


class CollectorTests(unittest.TestCase):
    def test_missing_balance_is_an_error(self):
        with tempfile.TemporaryDirectory() as folder:
            c = Collector(config(Path(folder)), Mock(), {'id': 1, 'mode': 'daily', 'created': '2026-10-09T10:00:00+03:00'})
            wb = Mock()
            wb.request.return_value = []
            with self.assertRaises(APIError):
                c.balance(wb)

    def test_recent_finance_windows_survive_old_history_error(self):
        with tempfile.TemporaryDirectory() as folder:
            c = Collector(config(Path(folder)), Mock(), {'id': 1, 'mode': 'full', 'created': '2026-10-09T10:00:00+03:00'})
            def window(wb, period, a, b):
                if a < '2025-01-01':
                    raise APIError('Недоступный период', 400)
            c.finance_window = Mock(side_effect=window)
            with self.assertRaises(APIError):
                c.finance(Mock(), 'daily')
            calls = c.finance_window.call_args_list
            self.assertGreater(calls[0].args[2], calls[-1].args[2])
            self.assertGreater(len(calls), 20)

    def test_windows_contiguous_boundaries(self):
        self.assertEqual(list(windows(date(2026, 1, 1), date(2026, 2, 2))),
                         [('2026-01-01', '2026-01-31'), ('2026-02-01', '2026-02-02')])

    def test_financial_cursor_must_advance(self):
        with tempfile.TemporaryDirectory() as folder:
            c = Collector(config(Path(folder)), Mock(), {'id': 1, 'mode': 'full', 'created': '2026-10-09T10:00:00+03:00'})
            c.request_table = Mock(side_effect=[[], [{'rrdId': 0}]])
            with self.assertRaises(APIError):
                c.finance_window(Mock(), 'daily', '2026-10-01', '2026-10-09')

    def test_financial_all_pages_to_204(self):
        with tempfile.TemporaryDirectory() as folder:
            c = Collector(config(Path(folder)), Mock(), {'id': 1, 'mode': 'full', 'created': '2026-10-09T10:00:00+03:00'})
            c.request_table = Mock(side_effect=[[], [{'rrdId': 7}], [{'rrdId': 11}], []])
            c.finance_window(Mock(), 'weekly', '2026-10-01', '2026-10-09')
            cursors = [call.kwargs['body']['rrdId'] for call in c.request_table.call_args_list if 'rrdId' in call.kwargs.get('body', {})]
            self.assertEqual(cursors, [0, 7, 11])

    def test_source_failure_does_not_block_second_account(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            c = Collector(config(root), Mock(), {'id': 1, 'mode': 'daily', 'created': '2026-10-09T10:00:00+03:00'})
            fake = Mock()
            fake.request.return_value = {'currency': 'RUB', 'current': 1, 'for_withdraw': 0}
            with patch('wb_backup.collector.WB', return_value=fake), patch.object(c, 'table'), \
                 patch.object(c, 'stocks', side_effect=APIError('failed')), patch.object(c, 'cards'), \
                 patch.object(c, 'fbs'), patch.object(c, 'statistics'), patch.object(c, 'finance'), \
                 patch.object(c, 'prices'), patch.object(c, 'documents'), patch.object(c, 'acquiring'), \
                 patch.object(c, 'generated'), patch('wb_backup.collector.package', return_value=[]):
                files, failures = c.run()
            self.assertEqual(failures, 2)
            manifest = json.loads((c.root / 'РЕЕСТР.json').read_text(encoding='utf-8'))
            self.assertEqual(manifest['status'], 'incomplete')
            self.assertEqual({r['account_key'] for r in manifest['sources']}, {'1', '2'})


if __name__ == '__main__':
    unittest.main()
