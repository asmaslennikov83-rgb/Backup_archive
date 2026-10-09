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
                  root, time(10), 2 * 1024 * 1024)


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

    def test_old_full_button_disabled_and_new_button_is_one_day(self):
        bot = object.__new__(Bot)
        bot.config = Mock(allowed={123})
        bot.store = Mock()
        bot.telegram = Mock()
        message = {'from': {'id': 123}, 'chat': {'id': 123, 'type': 'private'}}
        bot.handle({**message, 'text': '/all'})
        bot.store.enqueue.assert_not_called()
        bot.store.enqueue.return_value = (1, True)
        bot.handle({**message, 'text': 'Скачать за вчера'})
        bot.store.enqueue.assert_called_once_with('yesterday', {123})

    def test_10am_moscow_and_catchup(self):
        self.assertFalse(schedule_due(datetime(2026, 10, 9, 9, 59, tzinfo=MSK), time(10), None))
        self.assertTrue(schedule_due(datetime(2026, 10, 9, 10, tzinfo=MSK), time(10), None))
        self.assertTrue(schedule_due(datetime(2026, 10, 9, 15, tzinfo=MSK), time(10), '2026-10-08'))
        self.assertFalse(schedule_due(datetime(2026, 10, 9, 15, tzinfo=MSK), time(10), '2026-10-09'))


class StorageTests(unittest.TestCase):
    def test_duplicate_and_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder))
            job, created = store.enqueue('yesterday', {123})
            self.assertTrue(created)
            self.assertEqual(store.enqueue('yesterday', {123}), (job, False))
            self.assertEqual(store.next_job()['id'], job)
            store.db.close()
            resumed = Store(Path(folder))
            self.assertEqual(resumed.next_job()['id'], job)
            resumed.mark_delivered(job, 123, 'part.zip')
            self.assertTrue(resumed.delivered(job, 123, 'part.zip'))
            resumed.db.close()

    def test_legacy_jobs_cancelled_without_losing_archives(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Store(root)
            store.db.execute("INSERT INTO jobs(mode,recipients,status,created) VALUES ('full','[123]','running','2026-10-09T10:00:00+03:00')")
            store.db.commit()
            store.set('last_scheduled_date', '2026-10-09')
            archive = root / 'old.zip'
            archive.write_bytes(b'keep')
            store.db.close()
            upgraded = Store(root)
            self.assertIsNone(upgraded.next_job())
            self.assertEqual(upgraded.latest()['status'], 'cancelled')
            self.assertIsNone(upgraded.get('last_scheduled_date'))
            self.assertEqual(archive.read_bytes(), b'keep')
            with self.assertRaises(ValueError):
                upgraded.enqueue('full', {123})
            upgraded.db.close()


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
            c = Collector(config(Path(folder)), Mock(), {'id': 1, 'mode': 'yesterday', 'created': '2026-10-09T10:00:00+03:00'})
            wb = Mock()
            wb.request.return_value = []
            with self.assertRaises(APIError):
                c.balance(wb)

    def test_yesterday_uses_moscow_date_and_single_finance_window(self):
        with tempfile.TemporaryDirectory() as folder:
            # 22:30 UTC on the 8th is already October 9 in Moscow.
            c = Collector(config(Path(folder)), Mock(), {'id': 1, 'mode': 'yesterday', 'created': '2026-10-08T22:30:00+00:00'})
            self.assertEqual(c.start, date(2026, 10, 8))
            self.assertEqual(c.end, c.start)
            c.finance_window = Mock()
            wb = Mock()
            c.finance(wb, 'daily')
            c.finance_window.assert_called_once_with(wb, 'daily', '2026-10-08', '2026-10-08')

    def test_month_boundary_and_no_legacy_history(self):
        with tempfile.TemporaryDirectory() as folder:
            job = {'id': 1, 'mode': 'yesterday', 'created': '2026-01-01T10:00:00+03:00'}
            c = Collector(config(Path(folder)), Mock(), job)
            self.assertEqual(c.start, date(2025, 12, 31))
            with self.assertRaises(ValueError):
                Collector(config(Path(folder)), Mock(), {**job, 'mode': 'full'})

    def test_statistics_fetches_exact_day_once(self):
        with tempfile.TemporaryDirectory() as folder:
            c = Collector(config(Path(folder)), Mock(), {'id': 1, 'mode': 'yesterday', 'created': '2026-10-09T10:00:00+03:00'})
            wb = Mock()
            wb.request.return_value = [{'date': '2026-10-08T19:00:00', 'srid': 'id'}]
            c.table = Mock()
            c.statistics(wb, 'orders')
            wb.request.assert_called_once_with('statistics', 'GET', '/api/v1/supplier/orders',
                                               params={'dateFrom': '2026-10-08', 'flag': 1})

    def test_generated_and_documents_request_single_day(self):
        with tempfile.TemporaryDirectory() as folder:
            c = Collector(config(Path(folder)), Mock(), {'id': 1, 'mode': 'yesterday', 'created': '2026-10-09T10:00:00+03:00'})
            wb = Mock()
            c.generated_window = Mock()
            c.generated(wb, 'paid_storage', 8)
            c.generated_window.assert_called_once_with(wb, 'paid_storage', '2026-10-08', '2026-10-08')
            c.request_table = Mock(return_value=[])
            c.documents(wb)
            params = c.request_table.call_args.kwargs['params']
            self.assertEqual(params['beginTime'], '2026-10-08')
            self.assertEqual(params['endTime'], '2026-10-08')

    def test_windows_contiguous_boundaries(self):
        self.assertEqual(list(windows(date(2026, 1, 1), date(2026, 2, 2))),
                         [('2026-01-01', '2026-01-31'), ('2026-02-01', '2026-02-02')])

    def test_financial_cursor_must_advance(self):
        with tempfile.TemporaryDirectory() as folder:
            c = Collector(config(Path(folder)), Mock(), {'id': 1, 'mode': 'yesterday', 'created': '2026-10-09T10:00:00+03:00'})
            c.request_table = Mock(side_effect=[[], [{'rrdId': 0}]])
            with self.assertRaises(APIError):
                c.finance_window(Mock(), 'daily', '2026-10-01', '2026-10-09')

    def test_financial_all_pages_to_204(self):
        with tempfile.TemporaryDirectory() as folder:
            c = Collector(config(Path(folder)), Mock(), {'id': 1, 'mode': 'yesterday', 'created': '2026-10-09T10:00:00+03:00'})
            c.request_table = Mock(side_effect=[[], [{'rrdId': 7}], [{'rrdId': 11}], []])
            c.finance_window(Mock(), 'daily', '2026-10-08', '2026-10-08')
            cursors = [call.kwargs['body']['rrdId'] for call in c.request_table.call_args_list if 'rrdId' in call.kwargs.get('body', {})]
            self.assertEqual(cursors, [0, 7, 11])
            for call in c.request_table.call_args_list:
                body = call.kwargs['body']
                self.assertEqual(body['dateFrom'], '2026-10-08T00:00:00')
                self.assertEqual(body['dateTo'], '2026-10-08T23:59:59.999')

    def test_source_failure_does_not_block_second_account(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            c = Collector(config(root), Mock(), {'id': 1, 'mode': 'yesterday', 'created': '2026-10-09T10:00:00+03:00'})
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
            self.assertNotIn('Еженедельные отчёты реализации', {r['source'] for r in manifest['sources']})
            self.assertEqual(manifest['requested_start'], '2026-10-08')
            self.assertEqual(manifest['requested_end'], '2026-10-08')


if __name__ == '__main__':
    unittest.main()
