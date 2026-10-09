import json
import time
import hashlib
from pathlib import Path
import httpx
from .storage import atomic_json, stamp

HOSTS = {
    "finance": "https://finance-api.wildberries.ru",
    "statistics": "https://statistics-api.wildberries.ru",
    "analytics": "https://seller-analytics-api.wildberries.ru",
    "content": "https://content-api.wildberries.ru",
    "marketplace": "https://marketplace-api.wildberries.ru",
    "documents": "https://documents-api.wildberries.ru",
    "prices": "https://discounts-prices-api.wildberries.ru",
}
# Conservative shared host pacing per account, including retries.
INTERVALS = {"finance": 61, "statistics": 61, "analytics": 61,
             "content": 1, "marketplace": 1, "documents": 11, "prices": 1}


class APIError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def rows(value, *path):
    for key in path:
        if not isinstance(value, dict) or key not in value:
            raise APIError("Формат ответа WB изменился: нет поля " + ".".join(path))
        value = value[key]
    if not isinstance(value, list) or any(not isinstance(x, dict) for x in value):
        raise APIError("Формат ответа WB изменился: ожидается список строк")
    return value


class WB:
    def __init__(self, account, cache: Path):
        self.cache = cache
        self.headers = {"Authorization": account.token, "User-Agent": "WBIndependentBackup/1.0"}
        if account.secret:
            self.headers["X-Client-Secret"] = account.secret
        self.client = httpx.Client(timeout=120, follow_redirects=False)
        self.last = {}

    def request(self, host, method, path, *, body=None, params=None, fresh=False):
        if method not in ("GET", "POST") or host not in HOSTS:
            raise ValueError("Read adapters only")
        identity = json.dumps([host, method, path, body, params], sort_keys=True, ensure_ascii=False)
        key = hashlib.sha256(identity.encode()).hexdigest()
        file = self.cache / (key + ".json")
        if file.exists() and not fresh:
            return json.loads(file.read_text(encoding="utf-8"))["response"]
        for attempt in range(6):
            wait = INTERVALS[host] - (time.monotonic() - self.last.get(host, -1e10))
            if wait > 0:
                time.sleep(wait)
            self.last[host] = time.monotonic()
            try:
                response = self.client.request(method, HOSTS[host] + path, headers=self.headers, json=body, params=params)
            except httpx.TransportError:
                if attempt == 5:
                    raise APIError("WB: ошибка соединения после повторных попыток") from None
                time.sleep(min(60, 2 ** (attempt + 1)))
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 5:
                    raise APIError(f"WB: HTTP {response.status_code}, исчерпаны повторы")
                delay = response.headers.get("X-Ratelimit-Retry", response.headers.get("Retry-After", "60"))
                try:
                    delay = max(1, float(delay))
                except ValueError:
                    delay = 60
                time.sleep(min(delay, 3600))
                continue
            if response.status_code == 204:
                value = []
            elif not response.is_success:
                # Do not log response bodies or request headers: they may contain secrets.
                raise APIError(f"WB: HTTP {response.status_code}, {host}{path}; проверьте права токена и доступность метода", response.status_code)
            else:
                try:
                    value = response.json()
                except ValueError:
                    raise APIError("WB: ответ не является JSON") from None
                if isinstance(value, dict) and value.get("error") is True:
                    raise APIError("WB: ошибка в успешном HTTP-ответе")
            atomic_json(file, {"source": HOSTS[host] + path, "body": body, "params": params,
                               "received_at": stamp(), "response": value})
            return value
        raise APIError("WB: запрос не завершён")
