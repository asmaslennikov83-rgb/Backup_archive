"""Disk accounting and bounded retention; never follow links when deleting."""
from collections import defaultdict
from datetime import datetime, timedelta
import json
import shutil
from .config import MSK

RETENTION_DAYS = 14


def linked(path):
    return path.is_symlink() or getattr(path, 'is_junction', lambda: False)()


def remove_tree(path, boundary):
    boundary = boundary.resolve()
    if linked(path) or not path.resolve().is_relative_to(boundary) or path.resolve() == boundary:
        raise ValueError('Небезопасный путь очистки')
    # Refuse trees containing reparse points, including Windows junctions.
    for entry in path.rglob('*'):
        if linked(entry) or not entry.resolve().is_relative_to(boundary):
            raise ValueError('Ссылка в папке очистки')
    shutil.rmtree(path)


def cleanup(store, now=None):
    now = now or datetime.now(MSK)
    cutoff = now - timedelta(days=RETENTION_DAYS)
    base = store.root / 'jobs'
    result = {'deleted_jobs': 0, 'internal_removed': 0, 'errors': 0}
    if not base.exists() or linked(base):
        return result
    with store.lock:
        for folder in base.iterdir():
            if not folder.name.isdigit() or not folder.is_dir() or linked(folder):
                continue
            row = store.db.execute('SELECT * FROM jobs WHERE id=?', (int(folder.name),)).fetchone()
            if row and row['status'] in ('queued', 'running'):
                continue
            try:
                created = datetime.fromisoformat(row['created']) if row else datetime.fromtimestamp(folder.stat().st_mtime, MSK)
                if created.tzinfo is None:
                    created = created.replace(tzinfo=MSK)
                if created < cutoff:
                    remove_tree(folder, base)
                    if row:
                        with store.db:
                            store.db.execute("UPDATE jobs SET status='expired', progress='Удалено по сроку хранения: 14 дней', error='' WHERE id=?", (row['id'],))
                            store.db.execute('DELETE FROM sent WHERE job=?', (row['id'],))
                    result['deleted_jobs'] += 1
                elif (folder / 'internal').exists():
                    # Temporary card responses are dispensable even for failed
                    # jobs: retry can discover size IDs again if necessary.
                    remove_tree(folder / 'internal', base)
                    result['internal_removed'] += 1
            except (OSError, ValueError):
                result['errors'] += 1
    store.set('last_cleanup', {'at': now.isoformat(), **result})
    return result


def disk_report(root):
    totals = defaultdict(int)
    jobs = defaultdict(int)
    if linked(root):
        return 'Не удалось проверить хранилище: путь является ссылкой.'
    # os.walk with followlinks=False plus explicit junction filtering.
    import os
    for directory, dirs, files in os.walk(root, followlinks=False):
        from pathlib import Path
        parent = Path(directory)
        dirs[:] = [d for d in dirs if not linked(parent / d)]
        for name in files:
            file = parent / name
            if linked(file):
                continue
            try:
                size = file.stat().st_size
            except OSError:
                continue
            parts = file.relative_to(root).parts
            if len(parts) >= 2 and parts[0] == 'jobs':
                jobs[parts[1]] += size
            category = ('cards' if 'internal' in parts else 'zip' if 'delivery' in parts else
                        'excel' if 'excel' in parts or file.suffix == '.xlsx' else
                        'documents' if 'documents' in parts else 'raw' if 'raw' in parts else 'other')
            totals[category] += size
    labels = {'cards': 'Служебные карточки', 'zip': 'ZIP для отправки', 'excel': 'Excel',
              'documents': 'Оригиналы документов', 'raw': 'Исходные ответы WB', 'other': 'База и прочие файлы'}
    mb = lambda n: f'{n / 1024 / 1024:.1f} МиБ'
    lines = [f'Место на диске: {mb(sum(totals.values()))}', 'Хранение: 14 дней. Активные загрузки защищены.', '']
    lines += [f'{labels[key]}: {mb(value)}' for key, value in sorted(totals.items(), key=lambda x: -x[1])]
    lines += ['', 'Самые большие загрузки:'] + [f'№{key}: {mb(size)}' for key, size in sorted(jobs.items(), key=lambda x: -x[1])[:5]]
    return '\n'.join(lines)
