import json
import logging
import signal
import threading
from datetime import datetime
from .config import Config, MSK
from .storage import Store
from .collector import Collector, SOURCES
from .telegram import Telegram, TelegramError
from .lock import InstanceLock

log = logging.getLogger('wb_backup')


def authorized(message, allowed):
    return (message.get('chat', {}).get('type') == 'private'
            and message.get('from', {}).get('id') in allowed
            and message.get('chat', {}).get('id') == message.get('from', {}).get('id'))


def schedule_due(now, configured_time, last):
    return now.timetz().replace(tzinfo=None) >= configured_time and last != now.date().isoformat()


class Bot:
    def __init__(self, config):
        self.config = config
        self.instance_lock = InstanceLock(config.data)
        self.store = Store(config.data)
        self.stop = threading.Event()
        self.telegram = Telegram(config.telegram_token)

    def notify(self, recipients, text):
        for user in recipients:
            if user not in self.config.allowed:
                continue
            try:
                self.telegram.message(user, text)
            except TelegramError:
                log.warning('Не удалось отправить сообщение получателю %s', user)

    def worker(self):
        while not self.stop.is_set():
            job = self.store.next_job()
            if not job:
                self.stop.wait(2)
                continue
            recipients = set(json.loads(job['recipients'])) & self.config.allowed
            self.notify(recipients, f'Загрузка №{job["id"]} началась. Полная история может занимать часы из-за лимитов WB. '
                                   'Прогресс доступен по кнопке «Статус».')
            try:
                files, failures = Collector(self.config, self.store, job).run()
                delivery_errors = 0
                for user in recipients:
                    for index, path in enumerate(files, 1):
                        if self.store.delivered(job['id'], user, path.name):
                            continue
                        try:
                            caption = f'Архив №{job["id"]}, часть {index}/{len(files)}. '
                            caption += f'Есть ошибки в {failures} источниках — см. РЕЕСТР.xlsx.' if failures else 'Перечисленные источники загружены; периоды и ограничения — в РЕЕСТР.xlsx.'
                            self.telegram.document(user, path, caption)
                            self.store.mark_delivered(job['id'], user, path.name)
                            self.stop.wait(1)
                        except TelegramError:
                            delivery_errors += 1
                state = 'incomplete' if failures or delivery_errors else 'complete'
                self.store.update(job['id'], status=state, progress='Архив сохранён',
                                  error=f'Ошибок источников: {failures}; ошибок доставки: {delivery_errors}')
                self.notify(recipients, f'Загрузка №{job["id"]}: ' + ('готово.' if state == 'complete' else
                            'есть ошибки. Архив сохранён, подробности в реестре. /retry повторит незавершённые этапы и доставку.'))
            except Exception as error:
                # Exception text may include HTTP URLs with tokens. Log only its class.
                log.error('Ошибка задания %s: %s', job['id'], type(error).__name__)
                self.store.update(job['id'], status='failed', error=type(error).__name__)
                self.notify(recipients, 'Загрузка остановлена. Уже полученные данные сохранены. /retry продолжит загрузку.')

    def handle(self, message):
        if not authorized(message, self.config.allowed):
            return
        user = message['from']['id']
        command = message.get('text', '').strip()
        if command in ('/start', '/help'):
            self.telegram.message(user, 'Архив Wildberries: оба кабинета, ежедневно в ' + self.config.schedule.strftime('%H:%M') +
                ' по Москве.\n«Скачать всё» — все подключённые источники за доступную историю API. '
                'Баланс и текущие остатки — снимки.\nИсточники:\n' + '\n'.join(SOURCES.values()) +
                '\n/retry — продолжить последнюю неполную загрузку.\nНажатие кнопки во время работы не создаёт повторное задание.', True)
        elif command in ('/all', 'Скачать всё', '/daily', 'Скачать свежие данные'):
            mode = 'full' if command in ('/all', 'Скачать всё') else 'daily'
            job, created = self.store.enqueue(mode, {user})
            self.telegram.message(user, f'Загрузка №{job} ' + ('добавлена в очередь.' if created else
                'уже выполняется. Дождитесь завершения; затем повторите команду для собственного архива.'))
        elif command in ('/status', 'Статус'):
            job = self.store.latest()
            states = {'queued': 'В очереди', 'running': 'Загрузка', 'complete': 'Готово',
                      'incomplete': 'Есть ошибки', 'failed': 'Остановлено из-за ошибки'}
            self.telegram.message(user, 'Загрузок ещё нет.' if not job else
                f'Загрузка №{job["id"]}\nРежим: {"Вся история" if job["mode"] == "full" else "Свежие данные"}\n'
                f'Статус: {states.get(job["status"], job["status"])}\nНачало: {job["created"]}\n'
                f'{job["progress"]}\n{job["error"]}')
        elif command == '/retry':
            job = self.store.latest()
            if not job or job['status'] not in ('incomplete', 'failed'):
                self.telegram.message(user, 'Нет остановленной или неполной загрузки для продолжения.')
            elif user not in json.loads(job['recipients']):
                self.telegram.message(user, 'Повторить доставку может получатель этой загрузки. Для нового архива: /all.')
            else:
                self.store.update(job['id'], status='queued')
                self.telegram.message(user, f'Загрузка №{job["id"]} будет продолжена.')
        else:
            self.telegram.message(user, 'Используйте кнопки меню или /help.', True)

    def run(self):
        # Long polling: one replica and no webhook. Do not silently delete an existing webhook.
        info = self.telegram.call('getWebhookInfo', {})
        if info.get('url'):
            raise ValueError('У бота настроен webhook. Используйте отдельного бота или отключите webhook перед запуском.')
        threading.Thread(target=self.worker, daemon=True).start()
        offset = self.store.get('telegram_offset', 0)
        while not self.stop.is_set():
            now = datetime.now(MSK)
            if schedule_due(now, self.config.schedule, self.store.get('last_scheduled_date')):
                _, created = self.store.enqueue('daily', self.config.recipients)
                if created:
                    self.store.set('last_scheduled_date', now.date().isoformat())
            try:
                updates = self.telegram.call('getUpdates', {'offset': offset, 'timeout': 20,
                    'allowed_updates': json.dumps(['message'])})
                for update in updates:
                    if 'message' in update:
                        self.handle(update['message'])
                    offset = update['update_id'] + 1
                    self.store.set('telegram_offset', offset)
            except TelegramError:
                log.warning('Telegram недоступен; повтор через 10 секунд')
                self.stop.wait(10)


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    bot = Bot(Config.load())
    signal.signal(signal.SIGINT, lambda *_: bot.stop.set())
    signal.signal(signal.SIGTERM, lambda *_: bot.stop.set())
    bot.run()


if __name__ == '__main__':
    main()
