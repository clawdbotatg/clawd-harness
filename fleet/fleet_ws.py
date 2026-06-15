"""Shared stdlib WebSocket helpers for the fleet prototype.

The harness's `server.py` already speaks WebSocket *server*-side (it accepts
browser connections). The fleet adds the mirror case: workers and the mobile
client **dial out** to the relay, so we also need a tiny WebSocket *client*.

Framing is RFC 6455. Two rules that matter:
  - clients MUST mask their frames (`mask=True`); servers MUST NOT.
  - `ws_read_message` unmasks transparently, so the same reader works on both
    ends.

The relay speaks plain `ws://` (terminate `wss://` at nginx/an ALB in front of
it). Clients support both: `client_connect` wraps the socket in TLS for
`wss://`, so workers and the mobile can dial the public `wss://` endpoint.
"""
import base64
import hashlib
import os
import socket
import ssl
import struct
from urllib.parse import urlparse

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def ws_send(wfile, lock, data, opcode=0x1, mask=False):
    """Write one WebSocket frame. `lock` serializes writers on a shared socket."""
    payload = data.encode("utf-8") if isinstance(data, str) else data
    header = bytearray([0x80 | opcode])
    n = len(payload)
    mbit = 0x80 if mask else 0
    if n < 126:
        header.append(mbit | n)
    elif n < 65536:
        header.append(mbit | 126)
        header += struct.pack(">H", n)
    else:
        header.append(mbit | 127)
        header += struct.pack(">Q", n)
    if mask:
        mk = os.urandom(4)
        header += mk
        payload = bytes(payload[i] ^ mk[i % 4] for i in range(len(payload)))
    with lock:
        wfile.write(bytes(header) + payload)
        wfile.flush()


def ws_read_message(rfile):
    """Read one full message (reassembling fragments). Returns (kind, bytes) where
    kind is a text/binary opcode int, or "close"/"ping"/"pong"; None on EOF."""
    payload = b""
    msg_opcode = None
    while True:
        hdr = rfile.read(2)
        if len(hdr) < 2:
            return None
        b0, b1 = hdr[0], hdr[1]
        fin = b0 & 0x80
        opcode = b0 & 0x0F
        masked = b1 & 0x80
        length = b1 & 0x7F
        if length == 126:
            ext = rfile.read(2)
            if len(ext) < 2:
                return None
            length = struct.unpack(">H", ext)[0]
        elif length == 127:
            ext = rfile.read(8)
            if len(ext) < 8:
                return None
            length = struct.unpack(">Q", ext)[0]
        mask = rfile.read(4) if masked else b""
        chunk = rfile.read(length) if length else b""
        if masked and chunk:
            chunk = bytes(chunk[i] ^ mask[i % 4] for i in range(len(chunk)))
        if opcode == 0x8:
            return ("close", chunk)
        if opcode == 0x9:
            return ("ping", chunk)
        if opcode == 0xA:
            return ("pong", chunk)
        if opcode != 0x0:
            msg_opcode = opcode
        payload += chunk
        if fin:
            return (msg_opcode or 0x1, payload)


def server_accept_headers(key):
    """Compute the Sec-WebSocket-Accept value for a server handshake reply."""
    return base64.b64encode(
        hashlib.sha1((key + WS_GUID).encode()).digest()).decode()


def client_connect(url, extra_headers=None, timeout=15):
    """Dial out and perform the client handshake. Returns (sock, rfile, wfile).

    `url` is ws(s)://host:port/path?query. Extra HTTP headers (e.g. an auth
    token, though we pass that in the query for parity with the harness) go in
    `extra_headers`. Raises on a non-101 response.
    """
    u = urlparse(url)
    if u.scheme not in ("ws", "wss", "http", "https"):
        raise ValueError(f"unsupported scheme {u.scheme!r}")
    tls = u.scheme in ("wss", "https")
    host = u.hostname
    port = u.port or (443 if tls else 80)
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    sock = socket.create_connection((host, port), timeout=timeout)
    if tls:
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
    sock.settimeout(None)
    key = base64.b64encode(os.urandom(16)).decode()
    lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {host}:{port}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
    ]
    for k, v in (extra_headers or {}).items():
        lines.append(f"{k}: {v}")
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    rfile = sock.makefile("rb")
    status = rfile.readline()
    if b" 101 " not in status:
        # drain a little for a useful error, then bail
        rest = b""
        for _ in range(20):
            h = rfile.readline()
            if h in (b"\r\n", b"", b"\n"):
                break
            rest += h
        sock.close()
        raise ConnectionError(f"handshake failed: {status.strip()!r} {rest!r}")
    while True:  # skip remaining response headers
        h = rfile.readline()
        if h in (b"\r\n", b"", b"\n"):
            break
    wfile = sock.makefile("wb")
    return sock, rfile, wfile
