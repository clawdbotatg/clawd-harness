"""Bounded private document storage. All filesystem names are flat and no-follow.

Historical versions count against quotas; reaching a quota refuses writes rather
than silently deleting recovery data. Only an administrator prunes old versions.
"""
import hashlib
import json
import os
import re
import secrets
import stat
import threading
import time
from pathlib import Path

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class DocError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class Store:
    def __init__(self, root, max_bytes=4*1024*1024, max_docs=2000,
                 max_total=256*1024*1024, max_entries=4000,
                 reserve_bytes=256*1024*1024):
        self.root = Path(root)
        self.max_bytes, self.max_docs = max_bytes, max_docs
        self.max_total, self.max_entries = max_total, max_entries
        self.reserve_bytes = reserve_bytes
        self.lock = threading.Lock()

    @staticmethod
    def validate(name):
        if not NAME.fullmatch(name or ""):
            raise DocError("bad doc name (1–128 characters, letters/digits/._-, start alnum)")

    def _open(self):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.root, DIRECTORY)
        os.fchmod(fd, 0o700)
        return fd

    @staticmethod
    def _stat(fd, name):
        try:
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        # Hardlinks also expose data outside the shelf. Version copies are
        # separate inodes so normal store operation never needs hardlinks.
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise DocError("unsafe store entry", 409)
        return st

    def _usage(self, fd):
        total = count = active = 0
        # scandir is incremental: don't allocate an unbounded directory listing.
        with os.scandir(fd) as entries:
            for entry in entries:
                if entry.name == ".trash":
                    trashfd = os.open(".trash", DIRECTORY, dir_fd=fd)
                    try:
                        with os.scandir(trashfd) as versions:
                            for version in versions:
                                st = self._stat(trashfd, version.name)
                                if st is None:
                                    continue
                                total += st.st_size
                                count += 1
                                if count > self.max_entries or total > self.max_total:
                                    raise DocError("store quota exceeded; administrator cleanup required", 507)
                    finally:
                        os.close(trashfd)
                else:
                    st = self._stat(fd, entry.name)
                    if st is None:
                        continue
                    total += st.st_size
                    count += 1
                    active += bool(NAME.fullmatch(entry.name))
                if count > self.max_entries or total > self.max_total:
                    raise DocError("store quota exceeded; administrator cleanup required", 507)
        return total, count, active

    def list(self):
        with self.lock:
            fd = self._open()
            try:
                out = []
                with os.scandir(fd) as entries:
                    for entry in entries:
                        if NAME.fullmatch(entry.name):
                            try:
                                st = self._stat(fd, entry.name)
                            except DocError:
                                continue
                            if st:
                                out.append({"name": entry.name, "size": st.st_size,
                                            "mtime": int(st.st_mtime)})
                            if len(out) > self.max_docs:
                                raise DocError("store document limit exceeded", 507)
                return sorted(out, key=lambda d: d["name"])
            finally:
                os.close(fd)

    def _read(self, fd, name):
        st = self._stat(fd, name)
        if st is None:
            raise DocError("no such doc", 404)
        src = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(src, "rb") as f:
            current = os.fstat(f.fileno())
            if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
                raise DocError("unsafe store entry", 409)
            data = f.read(self.max_bytes + 1)
        if len(data) > self.max_bytes:
            raise DocError("stored document exceeds size limit", 413)
        return data

    def get(self, name):
        self.validate(name)
        with self.lock:
            fd = self._open()
            try:
                return self._read(fd, name)
            finally:
                os.close(fd)

    @staticmethod
    def _write(fd, name, body):
        dst = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                      0o600, dir_fd=fd)
        try:
            with os.fdopen(dst, "wb") as f:
                f.write(body)
                f.flush()
                os.fsync(f.fileno())
        except BaseException:
            os.unlink(name, dir_fd=fd)
            raise

    def _trash(self, fd, name, body=None):
        try:
            os.mkdir(".trash", mode=0o700, dir_fd=fd)
        except FileExistsError:
            pass
        trashfd = os.open(".trash", DIRECTORY, dir_fd=fd)
        try:
            os.fchmod(trashfd, 0o700)
            version = f"{name}-{time.time_ns()}-{secrets.token_hex(8)}"
            if body is None:
                os.rename(name, version, src_dir_fd=fd, dst_dir_fd=trashfd)
            else:
                self._write(trashfd, version, body)
            os.fsync(trashfd)
        finally:
            os.close(trashfd)

    def put(self, name, body):
        self.validate(name)
        if len(body) > self.max_bytes:
            raise DocError("doc too large", 413)
        with self.lock:
            fd = self._open()
            tmp = ".put-" + secrets.token_hex(16)
            try:
                old = self._stat(fd, name)
                total, count, active = self._usage(fd)
                if old is None and active >= self.max_docs:
                    raise DocError("store document limit reached", 507)
                # Account for the transient duplicate of the previous version
                # too. This bounds peak disk usage, not just the final state.
                extra = len(body) + (old.st_size if old else 0)
                if total + extra > self.max_total or count + 1 + bool(old) > self.max_entries:
                    raise DocError("store quota reached; administrator cleanup required", 507)
                free = os.fstatvfs(fd)
                if free.f_bavail * free.f_frsize < self.reserve_bytes + extra:
                    raise DocError("insufficient free disk space", 507)
                previous = self._read(fd, name) if old else None
                self._write(fd, tmp, body)
                if previous is not None:
                    self._trash(fd, name, previous)
                # Never remove the current doc until its replacement is ready.
                os.replace(tmp, name, src_dir_fd=fd, dst_dir_fd=fd)
                os.fsync(fd)
            finally:
                try:
                    os.unlink(tmp, dir_fd=fd)
                except FileNotFoundError:
                    pass
                os.close(fd)

    def delete(self, name):
        self.validate(name)
        with self.lock:
            fd = self._open()
            try:
                if self._stat(fd, name) is None:
                    raise DocError("no such doc", 404)
                self._trash(fd, name)
                os.fsync(fd)
            finally:
                os.close(fd)


class Credentials:
    """Reload a small admin-owned registry on every request for instant revocation.

    Only SHA256 hashes are stored here. Missing/invalid registry fails closed.
    Each entry: {id, sha256, permissions: [read, write, delete], prefixes: [""]}.
    """
    def __init__(self, path):
        self.path = Path(path)

    def authenticate(self, token):
        if not token or len(token) > 256 or not token.isascii():
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        try:
            with self.path.open("rb") as f:
                data = f.read(65537)
            if len(data) > 65536:
                return None
            entries = json.loads(data)
            if not isinstance(entries, list) or len(entries) > 128:
                return None
            for entry in entries:
                if (isinstance(entry, dict) and isinstance(entry.get("sha256"), str)
                        and secrets.compare_digest(entry["sha256"], digest)
                        and NAME.fullmatch(entry.get("id", ""))
                        and isinstance(entry.get("permissions"), list)
                        and all(p in ("read", "write", "delete") for p in entry["permissions"])
                        and isinstance(entry.get("prefixes"), list)
                        and all(isinstance(p, str) for p in entry["prefixes"])):
                    return entry
        except (OSError, ValueError, TypeError):
            pass
        return None

    @staticmethod
    def allowed(identity, operation, name=None):
        return (operation in identity["permissions"] and
                (name is None or any(name.startswith(p) for p in identity["prefixes"])))


class RateLimit:
    """Bounded token buckets for authenticated identities, with a global bucket."""
    def __init__(self, rate=2, burst=30):
        self.rate, self.burst = rate, burst
        self.buckets = {}
        self.lock = threading.Lock()

    def allow(self, key):
        now = time.monotonic()
        with self.lock:
            # Fixed registry size bounds normal keys; revoked identities age out.
            if key not in self.buckets and len(self.buckets) >= 256:
                self.buckets = {k: v for k, v in self.buckets.items() if now-v[1] < 60}
                if len(self.buckets) >= 256:
                    return False
            tokens, last = self.buckets.get(key, (self.burst, now))
            tokens = min(self.burst, tokens + (now-last)*self.rate)
            ok = tokens >= 1
            self.buckets[key] = (tokens-1 if ok else tokens, now)
            return ok
