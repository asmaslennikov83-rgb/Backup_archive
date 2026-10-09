import json
import sqlite3
import threading
from pathlib import Path
from datetime import datetime
from .config import MSK


def stamp():
    return datetime.now(MSK).isoformat(timespec="seconds")


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class Store:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.lock = threading.RLock()
        self.db = sqlite3.connect(root / "state.sqlite", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY, mode TEXT, recipients TEXT,
            status TEXT, created TEXT, progress TEXT DEFAULT '', error TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS sent(job INTEGER, chat INTEGER, file TEXT,
            PRIMARY KEY(job, chat, file));
        """)
        self.db.execute("UPDATE jobs SET status='queued' WHERE status='running'")
        self.db.commit()

    def get(self, key, default=None):
        with self.lock:
            row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, json.dumps(value)))

    def enqueue(self, mode, recipients):
        with self.lock, self.db:
            active = self.db.execute("SELECT id FROM jobs WHERE status IN ('queued','running') ORDER BY id LIMIT 1").fetchone()
            if active:
                return active[0], False
            cursor = self.db.execute("INSERT INTO jobs(mode,recipients,status,created) VALUES (?,?,'queued',?)",
                                     (mode, json.dumps(sorted(recipients)), stamp()))
            return cursor.lastrowid, True

    def next_job(self):
        with self.lock, self.db:
            row = self.db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if row:
                self.db.execute("UPDATE jobs SET status='running' WHERE id=?", (row['id'],))
        return dict(row) if row else None

    def update(self, job, **values):
        if not set(values) <= {"status", "progress", "error"}:
            raise ValueError("Invalid state field")
        with self.lock, self.db:
            self.db.execute("UPDATE jobs SET " + ",".join(f"{k}=?" for k in values) + " WHERE id=?",
                            (*values.values(), job))

    def latest(self):
        with self.lock:
            row = self.db.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def delivered(self, job, chat, file):
        with self.lock:
            return self.db.execute("SELECT 1 FROM sent WHERE job=? AND chat=? AND file=?", (job, chat, file)).fetchone() is not None

    def mark_delivered(self, job, chat, file):
        with self.lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO sent VALUES (?,?,?)", (job, chat, file))
