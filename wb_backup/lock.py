"""Prevent two processes from collecting/delivering the same persistent queue."""
import os


class InstanceLock:
    def __init__(self, root):
        root.mkdir(parents=True, exist_ok=True)
        self.file = (root / 'instance.lock').open('a+b')
        self.file.seek(0)
        self.file.write(b'0')
        self.file.flush()
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError('Другой экземпляр бота уже использует DATA_DIR') from None
