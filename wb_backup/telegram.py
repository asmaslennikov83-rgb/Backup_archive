import time
import httpx
from .menu import KEYBOARD, COMMANDS


class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, token):
        self.base = f'https://api.telegram.org/bot{token}/'
        self.client = httpx.Client(timeout=120)

    def call(self, method, data, files=None):
        # URLs contain token; never log HTTP exceptions or request objects.
        for attempt in range(5):
            try:
                if files:
                    for _, stream, _ in files.values():
                        stream.seek(0)
                response = self.client.post(self.base + method, data=data, files=files)
                value = response.json()
            except (httpx.HTTPError, ValueError):
                if attempt == 4:
                    raise TelegramError('Telegram: ошибка соединения') from None
                time.sleep(2 ** attempt)
                continue
            if value.get('ok'):
                return value['result']
            if response.status_code == 429:
                time.sleep(max(1, value.get('parameters', {}).get('retry_after', 10)))
                continue
            if response.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            raise TelegramError(f'Telegram: HTTP {response.status_code}; проверьте доступность чата и токен')
        raise TelegramError('Telegram: исчерпаны повторы')

    def message(self, chat, text, keyboard=False):
        import json
        data = {'chat_id': chat, 'text': text[:4000]}
        if keyboard:
            data['reply_markup'] = json.dumps(KEYBOARD, ensure_ascii=False)
        return self.call('sendMessage', data)

    def register_commands(self):
        import json
        # Command names are public; each actual operation still checks the whitelist.
        self.call('setMyCommands', {'commands': json.dumps(COMMANDS, ensure_ascii=False)})

    def document(self, chat, path, caption):
        with path.open('rb') as stream:
            return self.call('sendDocument', {'chat_id': chat, 'caption': caption},
                             {'document': (path.name, stream, 'application/zip')})
