import base64
import hashlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from .api import WB, APIError, rows
from .export import write_table, safe_name, package
from .storage import atomic_json, stamp
from .config import MSK

SOURCES = {
    "balance": "Финансовый баланс",
    "finance_daily": "Ежедневные отчёты реализации",
    "orders": "Заказы (оперативные данные)",
    "sales": "Продажи и возвраты (оперативные данные)",
    "wb_stocks": "Остатки на складах WB",
    "cards": "Карточки товаров",
    "fbs_stocks": "Остатки на складах продавца",
    "prices": "Цены и скидки",
    "documents": "Документы продавца",
    "acquiring": "Издержки на приём платежей",
    "paid_storage": "Платное хранение",
    "acceptance": "Операции при приёмке",
}


def windows(start, end, days=31):
    while start <= end:
        finish = min(end, start + timedelta(days=days - 1))
        yield start.isoformat(), finish.isoformat()
        start = finish + timedelta(days=1)


class Collector:
    def __init__(self, config, store, job):
        self.config, self.store, self.job = config, store, job
        self.root = config.data / 'jobs' / str(job['id']) / 'archive'
        self.root.mkdir(parents=True, exist_ok=True)
        if job['mode'] != 'yesterday':
            raise ValueError('Старая многодневная загрузка отключена; создайте новую загрузку за вчера')
        created = datetime.fromisoformat(job['created'])
        if created.tzinfo is None:
            created = created.replace(tzinfo=MSK)
        self.start = self.end = created.astimezone(MSK).date() - timedelta(days=1)
        self.registry = []

    def table(self, wb, name, records, source, period='Текущее состояние'):
        path = wb.cache.parent / 'excel' / (safe_name(name) + '.xlsx')
        if not path.exists():
            write_table(path, records, {'Кабинет': self.account.name, 'Источник': source,
                                      'Период': period, 'Создано': stamp(),
                                      'Исходные данные': 'raw/*.json, с временем получения каждого ответа'})
        return records

    def request_table(self, wb, name, host, method, path, *, body=None, params=None, unwrap=(), period='Текущее состояние'):
        response = wb.request(host, method, path, body=body, params=params)
        records = [] if response == [] else rows(response, *unwrap)
        return self.table(wb, name, records, host + path, period)

    def finance(self, wb, period):
        start = self.start
        errors = []
        for a, b in reversed(list(windows(start, self.end))):
            try:
                self.finance_window(wb, period, a, b)
            except APIError as error:
                errors.append(f'{a} — {b}: {error}')
                if error.status in (401, 402, 403, 404):
                    break
        if errors:
            raise APIError('\n'.join(errors))

    def finance_window(self, wb, period, a, b):
            if period != 'daily':
                raise ValueError('Еженедельная выгрузка отключена в режиме одного дня')
            # Explicit bounds include the whole calendar day, not today's changes.
            bounds = {'dateFrom': a + 'T00:00:00', 'dateTo': b + 'T23:59:59.999'}
            offset = 0
            while True:
                batch = self.request_table(wb, f'{period}_реестр_{a}_{offset}', 'finance', 'POST',
                    '/api/finance/v1/sales-reports/list', body={**bounds,
                    'period': period, 'limit': 1000, 'offset': offset}, period=f'{a} — {b}')
                if len(batch) < 1000:
                    break
                offset += len(batch)
            cursor = 0
            while True:
                batch = self.request_table(wb, f'{period}_операции_{a}_{cursor}', 'finance', 'POST',
                    '/api/finance/v1/sales-reports/detailed', body={**bounds, 'period': period,
                    'limit': 10000, 'rrdId': cursor}, period=f'{a} — {b}')
                if not batch:
                    break
                next_cursor = batch[-1].get('rrdId')
                if next_cursor is None or int(next_cursor) <= cursor:
                    raise APIError('Курсор детализации не продвигается; полнота не подтверждена')
                cursor = int(next_cursor)

    def statistics(self, wb, kind):
        # flag=1 returns all operations for precisely this date. flag=0 would
        # also fetch today's updates and modifications to older operations.
        day = self.start.isoformat()
        response = wb.request('statistics', 'GET', f'/api/v1/supplier/{kind}',
                              params={'dateFrom': day, 'flag': 1})
        self.table(wb, f'{kind}_{day}', rows(response), f'statistics/{kind}',
                   f'{day}; оперативные данные, не итоговый финансовый отчёт')

    def stocks(self, wb):
        offset = 0
        while True:
            batch = self.request_table(wb, f'остатки_WB_{offset}', 'analytics', 'POST',
                '/api/analytics/v1/stocks-report/wb-warehouses', body={'nmIds': [], 'chrtIds': [], 'limit': 10000, 'offset': offset},
                unwrap=('data', 'items'))
            if len(batch) < 10000:
                break
            offset += len(batch)

    def cards(self, wb):
        cards = []
        # Include cards in trash: FBS stocks may still exist for them.
        for trash in (False, True):
            cursor = {'limit': 100}
            page = 0
            path = '/content/v2/get/cards/trash' if trash else '/content/v2/get/cards/list'
            while True:
                settings = {'sort': {'ascending': True}, 'cursor': cursor}
                if not trash:
                    settings['filter'] = {'withPhoto': -1}
                value = wb.request('content', 'POST', path, body={'settings': settings})
                batch = rows(value, 'cards')
                self.table(wb, f'карточки_{"корзина" if trash else "активные"}_{page}', batch, path)
                cards.extend(batch)
                if len(batch) < 100:
                    break
                nxt = value.get('cursor', {})
                field = 'trashedAt' if trash else 'updatedAt'
                if field not in nxt or 'nmID' not in nxt:
                    raise APIError('Неполный курсор карточек')
                new_cursor = {'limit': 100, field: nxt[field], 'nmID': nxt['nmID']}
                if new_cursor == cursor:
                    raise APIError('Курсор карточек не продвигается')
                cursor, page = new_cursor, page + 1
        return cards

    def fbs(self, wb):
        cards = self.cards(wb)
        ids = sorted({int(size['chrtID']) for card in cards for size in card.get('sizes', []) if 'chrtID' in size})
        warehouses = self.request_table(wb, 'склады_продавца', 'marketplace', 'GET', '/api/v3/warehouses')
        if cards and not ids:
            raise APIError('В карточках нет ID размеров; остатки FBS не получены')
        for warehouse in warehouses:
            if 'id' not in warehouse:
                raise APIError('В складе нет ID')
            for offset in range(0, len(ids), 1000):
                response = wb.request('marketplace', 'POST', f'/api/v3/stocks/{warehouse["id"]}', body={'chrtIds': ids[offset:offset + 1000]})
                batch = rows(response, 'stocks')
                for record in batch:
                    record.update(warehouseId=warehouse['id'], warehouseName=warehouse.get('name', ''))
                self.table(wb, f'остатки_FBS_{warehouse["id"]}_{offset}', batch, '/api/v3/stocks')

    def prices(self, wb):
        offset = 0
        while True:
            batch = self.request_table(wb, f'цены_{offset}', 'prices', 'GET', '/api/v2/list/goods/filter',
                params={'limit': 1000, 'offset': offset}, unwrap=('data', 'listGoods'))
            if len(batch) < 1000:
                break
            offset += len(batch)

    def documents(self, wb):
        offset = 0
        while True:
            params = {'locale': 'ru', 'limit': 50, 'offset': offset}
            params.update(beginTime=self.start.isoformat(), endTime=self.end.isoformat())
            batch = self.request_table(wb, f'документы_реестр_{offset}', 'documents', 'GET',
                '/api/v1/documents/list', params=params, unwrap=('data', 'documents'),
                period=f'{self.start} — {self.end}')
            for record in batch:
                if not record.get('extensions') or not record.get('serviceName'):
                    raise APIError('В документе нет форматов или ID')
                for extension in record['extensions']:
                    value = wb.request('documents', 'GET', '/api/v1/documents/download',
                                       params={'serviceName': record['serviceName'], 'extension': extension})
                    data = value.get('data', {})
                    if not isinstance(data.get('document'), str):
                        raise APIError('Нет содержимого документа')
                    try:
                        content = base64.b64decode(data['document'], validate=True)
                    except ValueError:
                        raise APIError('Некорректный документ base64') from None
                    unique = hashlib.sha256((record['serviceName'] + extension).encode()).hexdigest()[:16]
                    path = wb.cache.parent / 'documents' / (unique + '_' + safe_name(data.get('fileName', record['serviceName'] + '.' + extension)))
                    path.parent.mkdir(exist_ok=True)
                    path.write_bytes(content)
            if len(batch) < 50:
                break
            offset += len(batch)

    def acquiring(self, wb):
        for a, b in windows(self.start, self.end):
            cursor = 0
            while True:
                batch = self.request_table(wb, f'эквайринг_{a}_{cursor}', 'finance', 'POST',
                    '/api/finance/v1/acquiring/detailed', body={'dateFrom': a + 'T00:00:00', 'dateTo': b + 'T23:59:59.999', 'limit': 10000, 'rrdId': cursor},
                    period=f'{a} — {b}')
                if not batch:
                    break
                nxt = batch[-1].get('rrdId')
                if nxt is None or int(nxt) <= cursor:
                    raise APIError('Курсор эквайринга не продвигается')
                cursor = int(nxt)

    def generated(self, wb, kind, days):
        import time
        start = self.start
        errors = []
        for a, b in reversed(list(windows(start, self.end, days))):
            try:
                self.generated_window(wb, kind, a, b)
            except APIError as error:
                errors.append(f'{a} — {b}: {error}')
                if error.status in (401, 402, 403, 404):
                    break
        if errors:
            raise APIError('\n'.join(errors))

    def generated_window(self, wb, kind, a, b):
            import time
            if (wb.cache.parent / 'excel' / (safe_name(f'{kind}_{a}') + '.xlsx')).exists():
                return
            path = '/api/v1/' + kind
            value = wb.request('analytics', 'GET', path, params={'dateFrom': a, 'dateTo': b})
            if value == []:
                return
            task = value.get('data', {}).get('taskId')
            if not task:
                raise APIError('WB не вернул ID задания')
            # Status responses must not be permanently cached while a task is pending.
            status_path = path + f'/tasks/{task}/status'
            deadline = time.monotonic() + 1800
            recreations = 0
            while True:
                if time.monotonic() > deadline:
                    raise APIError('Истекло время ожидания генерации отчёта')
                try:
                    status = wb.request('analytics', 'GET', status_path, fresh=True)
                except APIError as error:
                    if error.status == 404:
                        if recreations >= 2:
                            raise APIError('WB не находит пересозданное задание')
                        recreations += 1
                        # Old generation tasks expire. Force a new task for this period.
                        value = wb.request('analytics', 'GET', path, params={'dateFrom': a, 'dateTo': b}, fresh=True)
                        task = value.get('data', {}).get('taskId') if isinstance(value, dict) else None
                        if not task:
                            raise APIError('Не удалось пересоздать истёкшее задание')
                        status_path = path + f'/tasks/{task}/status'
                        continue
                    raise
                state = status.get('data', {}).get('status') if isinstance(status, dict) else None
                if state == 'done':
                    break
                if state in ('failed', 'error', 'expired') and recreations < 2:
                    recreations += 1
                    value = wb.request('analytics', 'GET', path, params={'dateFrom': a, 'dateTo': b}, fresh=True)
                    task = value.get('data', {}).get('taskId') if isinstance(value, dict) else None
                    if not task:
                        raise APIError('Не удалось пересоздать задание')
                    status_path = path + f'/tasks/{task}/status'
                    continue
                if state in ('failed', 'error', 'expired'):
                    raise APIError('Отчёт не сформирован; повторите выгрузку')
                time.sleep(5)
            self.request_table(wb, f'{kind}_{a}', 'analytics', 'GET', path + f'/tasks/{task}/download', period=f'{a} — {b}')

    def balance(self, wb):
        value = wb.request('finance', 'GET', '/api/v1/account/balance')
        if not isinstance(value, dict) or not {'currency', 'current', 'for_withdraw'} <= value.keys():
            raise APIError('Неполный ответ баланса')
        self.table(wb, 'баланс', [value], '/api/v1/account/balance')

    def run(self):
        failures = 0
        for account in self.config.accounts:
            self.account = account
            folder = self.root / f'кабинет_{account.key}'
            wb = WB(account, folder / 'raw')
            operations = {
                'balance': lambda: self.balance(wb),
                'wb_stocks': lambda: self.stocks(wb),
                'cards': lambda: self.cards(wb), 'fbs_stocks': lambda: self.fbs(wb),
                'orders': lambda: self.statistics(wb, 'orders'), 'sales': lambda: self.statistics(wb, 'sales'),
                'finance_daily': lambda: self.finance(wb, 'daily'),
                'prices': lambda: self.prices(wb), 'documents': lambda: self.documents(wb),
                'acquiring': lambda: self.acquiring(wb),
                'paid_storage': lambda: self.generated(wb, 'paid_storage', 8),
                'acceptance': lambda: self.generated(wb, 'acceptance_report', 31),
            }
            try:
                for source, operation in operations.items():
                    self.store.update(self.job['id'], progress=f'{account.name}: {SOURCES[source]}')
                    marker = folder / f'{source}_result.json'
                    prior = json.loads(marker.read_text(encoding='utf-8')) if marker.exists() else None
                    if prior and prior['status'] == 'complete':
                        self.registry.append(prior)
                        continue
                    result = {'account': account.name, 'account_key': account.key, 'source': SOURCES[source],
                              'started': stamp(), 'status': 'complete', 'error': ''}
                    try:
                        operation()
                    except Exception as error:
                        result.update(status='incomplete', error=str(error) if isinstance(error, APIError) else type(error).__name__)
                        failures += 1
                    result['finished'] = stamp()
                    atomic_json(marker, result)
                    self.registry.append(result)
            finally:
                wb.client.close()
        metadata = {'job': self.job['id'], 'mode': self.job['mode'], 'created': self.job['created'],
                    'finished': stamp(), 'status': 'incomplete' if failures else 'complete',
                    'requested_start': self.start.isoformat(), 'requested_end': self.end.isoformat(),
                    'limits': 'Отчёты и документы запрашиваются только за прошедший календарный день по Москве. '
                              'Баланс, цены, карточки и текущие остатки — снимки. '
                              'Заказы/продажи — предварительные данные. WB может публиковать данные с задержкой; '
                              'более ранние дни автоматически не перепроверяются. '
                              'Еженедельные отчёты, история остатков CSV, реклама и сборочные задания отключены.',
                    'sources': self.registry}
        atomic_json(self.root / 'РЕЕСТР.json', metadata)
        write_table(self.root / 'РЕЕСТР.xlsx', self.registry, {'Загрузка': self.job['id'], 'Режим': self.job['mode'],
                                                           'Статус': metadata['status'], 'Примечания': metadata['limits']})
        files = package(self.root, self.root.parent / 'delivery', self.config.part_bytes)
        return files, failures
