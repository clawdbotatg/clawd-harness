#!/usr/bin/env python3
"""Security regression tests use isolated storage and a localhost relay."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from docs_store import Credentials, DocError, RateLimit, Store


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "store", max_bytes=64, max_total=256,
                           max_entries=8, reserve_bytes=0)

    def test_versions_bounded_preserve_current(self):
        for _ in range(3):
            self.store.put("plan.md", b"x" * 64)
        with self.assertRaises(DocError) as error:
            self.store.put("plan.md", b"y" * 64)
        self.assertEqual(error.exception.status, 507)
        self.assertEqual(self.store.get("plan.md"), b"x" * 64)
        self.assertLessEqual(sum(f.stat().st_size for f in self.store.root.rglob("*")
                                 if f.is_file()), 256)

    def test_empty_versions_are_counted(self):
        for _ in range(7):
            self.store.put("empty", b"")
        with self.assertRaises(DocError):
            self.store.put("empty", b"")

    def test_failed_replace_preserves_current_and_cleans_temp(self):
        self.store.put("plan", b"old")
        with patch("docs_store.os.replace", side_effect=OSError("disk failed")):
            with self.assertRaises(OSError):
                self.store.put("plan", b"new")
        self.assertEqual(self.store.get("plan"), b"old")
        self.assertFalse(list(self.store.root.glob(".put-*")))

    def test_symlinks_and_hardlinks_never_read_written_or_deleted(self):
        self.store.put("normal", b"fine")
        outside = self.root / "secret"
        outside.write_bytes(b"secret")
        (self.store.root / "link").symlink_to(outside)
        os.link(outside, self.store.root / "hard")
        for name in ("link", "hard"):
            for operation in (lambda: self.store.get(name),
                              lambda: self.store.put(name, b"overwrite"),
                              lambda: self.store.delete(name)):
                with self.assertRaises(DocError):
                    operation()
        self.assertEqual(self.store.list()[0]["name"], "normal")
        self.assertEqual(outside.read_bytes(), b"secret")

    def test_trash_directory_symlink_refused(self):
        self.store.put("normal", b"fine")
        (self.store.root / ".trash").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError):
            self.store.delete("normal")
        self.assertEqual(self.store.get("normal"), b"fine")

    def test_name_fullmatch_and_private_files(self):
        for name in ("../a", ".hidden", "a\n", "a/b", "a"*129, ""):
            with self.assertRaises(DocError):
                self.store.put(name, b"a")
        self.store.put("ok", b"ok")
        self.assertEqual(self.store.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.store.root / "ok").stat().st_mode & 0o777, 0o600)

    def test_disk_reserve_and_oversized_local_files(self):
        self.store.reserve_bytes = 10**30
        with self.assertRaises(DocError) as error:
            self.store.put("ok", b"ok")
        self.assertEqual(error.exception.status, 507)
        (self.store.root / "big").write_bytes(b"x" * 65)
        with self.assertRaises(DocError) as error:
            self.store.get("big")
        self.assertEqual(error.exception.status, 413)


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.credentials = root / "credentials.json"
        cls.entries = [{"id": "reader", "sha256": hashlib.sha256(b"reader-token").hexdigest(),
                        "permissions": ["read"], "prefixes": ["shared-"]}]
        for i in range(10):
            cls.entries.append({"id": f"writer{i}",
                                "sha256": hashlib.sha256(f"writer{i}-token".encode()).hexdigest(),
                                "permissions": ["read", "write", "delete"], "prefixes": [""]})
        cls.credentials.write_text(json.dumps(cls.entries))
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        cls.port = sock.getsockname()[1]
        sock.close()
        cls.proc = subprocess.Popen([sys.executable, "relay.py"], cwd=Path(__file__).parent,
            env={**os.environ, "FLEET_PORT": str(cls.port), "FLEET_BIND": "127.0.0.1",
                 "FLEET_DOCS_DIR": str(root / "docs"), "FLEET_REQUIRE_PASSKEY": "0",
                 "FLEET_DOCS_CREDENTIALS_FILE": str(cls.credentials),
                 "FLEET_MAX_DOC_BYTES": "1024", "FLEET_WORKER_TOKEN": "worker-token",
                 "FLEET_MOBILE_TOKEN": "mobile-token", "FLEET_PREFS_FILE": str(root / "prefs"),
                 "FLEET_SESSIONS_FILE": str(root / "sessions")},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", cls.port), timeout=.1):
                    break
            except OSError:
                time.sleep(.1)
        else:
            cls.proc.terminate()
            cls.tmp.cleanup()
            raise RuntimeError("test relay failed to start")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=5)
        cls.tmp.cleanup()

    def call(self, path, body=None, token="writer0-token", headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        h = {"Authorization": "Bearer " + token}
        h.update(headers or {})
        try:
            c.request("POST" if body is not None else "GET", path, body=body, headers=h)
            r = c.getresponse()
            return r.status, r.read(), dict(r.getheaders())
        finally:
            c.close()

    def test_auth_scope_and_revocation(self):
        for token in ("", "wrong", "worker-token", "mobile-token"):
            self.assertEqual(self.call("/docs/list", token=token)[0], 403)
        self.assertEqual(self.call("/docs/list?t=writer0-token")[0], 403)
        self.assertEqual(self.call("/docs/put?name=shared-a", b"public")[0], 200)
        self.assertEqual(self.call("/docs/put?name=private-a", b"private")[0], 200)
        status, body, _ = self.call("/docs/list", token="reader-token")
        self.assertEqual(status, 200)
        self.assertEqual([d["name"] for d in json.loads(body)["docs"]], ["shared-a"])
        self.assertEqual(self.call("/docs/get?name=private-a", token="reader-token")[0], 403)
        self.assertEqual(self.call("/docs/put?name=shared-a", b"bad", token="reader-token")[0], 403)
        self.assertEqual(self.call("/docs/rm?name=shared-a", b"", token="reader-token")[0], 403)
        self.credentials.write_text(json.dumps(self.entries[1:]))
        self.assertEqual(self.call("/docs/list", token="reader-token")[0], 403)
        self.credentials.write_text(json.dumps(self.entries))

    def test_html_forced_download_sandboxed(self):
        html = b'<script>fetch("/pm/")</script>'
        self.assertEqual(self.call("/docs/put?name=evil.html", html)[0], 200)
        status, body, headers = self.call("/docs/get?name=evil.html")
        self.assertEqual((status, body), (200, html))
        self.assertEqual(headers["Content-Type"], "application/octet-stream")
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="evil.html"')
        self.assertIn("sandbox", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_invalid_framing_preserves_document(self):
        self.assertEqual(self.call("/docs/put?name=framing", b"original")[0], 200)
        for length in ("invalid", "-1", "1,1"):
            self.assertEqual(self.call("/docs/put?name=framing", b"",
                                      headers={"Content-Length": length})[0], 400)
        self.assertEqual(self.call("/docs/put?name=framing", b"",
                                  headers={"Transfer-Encoding": "chunked"})[0], 400)
        raw = (b"POST /docs/put?name=framing HTTP/1.1\r\nHost: localhost\r\n"
               b"Authorization: Bearer writer0-token\r\nContent-Length: 8\r\n\r\nshort")
        with socket.create_connection(("127.0.0.1", self.port), timeout=3) as sock:
            sock.sendall(raw)
            sock.shutdown(socket.SHUT_WR)
            self.assertIn(b"400", sock.recv(4096).split(b"\r\n")[0])
        self.assertEqual(self.call("/docs/get?name=framing")[1], b"original")

    def test_z_agent_rate_limit(self):
        statuses = [self.call("/docs/list", token="writer9-token")[0] for _ in range(40)]
        self.assertIn(429, statuses)


class CredentialTests(unittest.TestCase):
    def test_invalid_registry_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "credentials"
            registry = Credentials(path)
            for data in ("{}", "not json", "[]", "x"*65537):
                path.write_text(data)
                self.assertIsNone(registry.authenticate("test"))

    def test_rate_recovers_without_sleep(self):
        limiter = RateLimit(rate=2, burst=2)
        with patch("docs_store.time.monotonic", return_value=100):
            self.assertTrue(limiter.allow("a"))
            self.assertTrue(limiter.allow("a"))
            self.assertFalse(limiter.allow("a"))
            self.assertTrue(limiter.allow("b"))
        with patch("docs_store.time.monotonic", return_value=101):
            self.assertTrue(limiter.allow("a"))


if __name__ == "__main__":
    unittest.main()
