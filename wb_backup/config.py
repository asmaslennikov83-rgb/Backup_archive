from dataclasses import dataclass
from datetime import time
import os
from pathlib import Path
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")


@dataclass(frozen=True)
class Account:
    key: str
    name: str
    token: str
    secret: str = ""


@dataclass(frozen=True)
class Config:
    telegram_token: str
    allowed: frozenset[int]
    recipients: frozenset[int]
    accounts: tuple[Account, ...]
    data: Path
    schedule: time
    part_bytes: int

    @classmethod
    def load(cls):
        from dotenv import load_dotenv
        load_dotenv()
        def ids(name):
            return frozenset(int(x.strip()) for x in os.getenv(name, "").split(",") if x.strip())
        allowed = ids("ALLOWED_USER_IDS")
        recipients = ids("RECIPIENT_IDS") or allowed
        accounts = tuple(Account(str(i), os.getenv(f"WB_{i}_NAME", f"Кабинет {i}"),
                                 os.getenv(f"WB_{i}_TOKEN", ""), os.getenv(f"WB_{i}_CLIENT_SECRET", ""))
                         for i in (1, 2))
        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        if not token or not allowed or any(x <= 0 for x in allowed) or not recipients <= allowed:
            raise ValueError("Настройте токен Telegram и положительные ID пользователей; получатели должны входить в белый список")
        if any(not a.token for a in accounts):
            raise ValueError("Нужны токены обоих кабинетов WB")
        size = int(os.getenv("ARCHIVE_PART_MB", "45"))
        if not 1 <= size <= 45:
            raise ValueError("ARCHIVE_PART_MB: 1–45")
        return cls(token, allowed, recipients, accounts, Path(os.getenv("DATA_DIR", "data")).resolve(),
                   time.fromisoformat(os.getenv("SCHEDULE_TIME", "10:00")), size * 1024 * 1024)
