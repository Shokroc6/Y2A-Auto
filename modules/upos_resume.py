"""One private UPOS checkpoint per media path; never stores account cookies.
Lock covers the whole upload including submission. Unknown POST outcomes fail closed.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading

_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


class UploadCheckpoint:
    def __init__(self, root, media, account):
        self.root = Path(root)
        self.media = Path(media).resolve()
        self.account = account
        key = hashlib.sha256(str(self.media).encode()).hexdigest()
        self.path = self.root / (key + '.json')
        with _LOCKS_GUARD:
            self.lock = _LOCKS.setdefault(str(self.path), threading.Lock())
        self.data = {}
        self.file_lock = None

    def __enter__(self):
        if not self.lock.acquire(blocking=False):
            raise ValueError('此成品已有上传正在进行，请勿重复点击')
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.root, 0o700)
            if os.name != 'nt':
                parent = os.open(self.root.parent, os.O_RDONLY)
                try: os.fsync(parent)
                finally: os.close(parent)
            self.file_lock = open(self.path.with_suffix('.lock'), 'a+b')
            os.chmod(self.file_lock.name, 0o600)
            if os.name == 'nt':
                import msvcrt
                self.file_lock.seek(0); self.file_lock.write(b'0'); self.file_lock.flush(); self.file_lock.seek(0)
                msvcrt.locking(self.file_lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            stat = self.media.stat()
            digest = hashlib.sha256()
            with self.media.open('rb') as f:
                for block in iter(lambda: f.read(4 * 1024 * 1024), b''):
                    digest.update(block)
            after = self.media.stat()
            if (stat.st_size, stat.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError('上传成品校验期间发生变化，已停止')
            identity = dict(path=str(self.media), size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                            sha256=digest.hexdigest(), account=self.account)
            if self.path.exists():
                self.data = json.loads(self.path.read_text(encoding='utf8'))
                if self.data.get('schema') != 1 or self.data.get('identity') != identity:
                    raise ValueError('上传续传记录与成品或账号不匹配，已停止；不会自动全量重传')
                if self.phase in ('merging', 'submitting'):
                    raise ValueError('上次合并或投稿结果未知，请核对平台结果；不会自动重复POST')
            else:
                self.data = dict(schema=1, identity=identity, phase='uploading', confirmed=[])
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *exc):
        if self.file_lock is not None:
            # Closing releases both POSIX and Windows kernel locks, including on crash.
            self.file_lock.close()
            self.file_lock = None
        self.lock.release()

    @property
    def session(self):
        return self.data.get('session')

    @property
    def confirmed(self):
        return set(self.data.get('confirmed', []))

    @property
    def phase(self):
        return self.data.get('phase')

    def _save(self):
        fd, name = tempfile.mkstemp(prefix='.upos-', dir=self.root)
        try:
            with os.fdopen(fd, 'w', encoding='utf8') as f:
                json.dump(self.data, f); f.flush(); os.fsync(f.fileno())
            os.replace(name, self.path)
            if os.name != 'nt':
                directory = os.open(self.root, os.O_RDONLY)
                try: os.fsync(directory)
                finally: os.close(directory)
        finally:
            if os.path.exists(name): os.unlink(name)

    def set_session(self, session):
        self.data['session'] = session
        self._save()

    def confirm(self, part):
        self.data['confirmed'] = sorted(self.confirmed | {part})
        self._save()

    def mark(self, phase):
        self.data['phase'] = phase
        self._save()

    def complete(self, result):
        self.data.pop('session', None)  # Drop upload capability as soon as merge is confirmed.
        self.data.update(phase='complete', result=result)
        self._save()

    def clear(self):
        self.path.unlink(missing_ok=True)
