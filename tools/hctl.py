#!/usr/bin/env python3
"""hctl — send one control frame to the LOCAL harness over its WebSocket.

The harness binds loopback with no auth, so any process on the box (a claude
session, the fleet PM's remote hands, you in a shell) can drive it:

  python3 tools/hctl.py accountRemove name=default
  python3 tools/hctl.py accountUse name=work
  python3 tools/hctl.py accountsRefresh
  python3 tools/hctl.py '{"type":"restart","reason":"manual"}'

Prints every JSON frame the server sends for a couple of seconds (the connect
snapshot includes `projects`/`sessions`/`accounts`, so e.g. an accountRemove
is verifiable right in the output). Stdlib only, like everything here.
"""
import base64, json, os, socket, sys, time

PORT = int(os.environ.get("PORT", "8787"))
LISTEN_SECS = float(os.environ.get("HCTL_LISTEN", "2.5"))


def parse_frame(args):
    if not args:
        print(__doc__.strip()); sys.exit(2)
    if args[0].lstrip().startswith("{"):
        return json.loads(args[0])
    frame = {"type": args[0]}
    for kv in args[1:]:
        k, _, v = kv.partition("=")
        frame[k] = v
    return frame


def ws_connect(host="127.0.0.1", port=PORT):
    s = socket.create_connection((host, port), timeout=5)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall((f"GET /ws HTTP/1.1\r\nHost: {host}:{port}\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
               ).encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = s.recv(4096)
        if not chunk:
            raise ConnectionError("closed during WS handshake")
        resp += chunk
    status = resp.split(b"\r\n", 1)[0]
    if b"101" not in status:
        raise ConnectionError(f"WS upgrade refused: {status.decode(errors='replace')}")
    return s


def send_text(s, text):
    payload = text.encode()
    mask = os.urandom(4)
    hdr = bytearray([0x81])                      # FIN + text
    n = len(payload)
    if n < 126:
        hdr.append(0x80 | n)
    elif n < 65536:
        hdr.append(0x80 | 126); hdr += n.to_bytes(2, "big")
    else:
        hdr.append(0x80 | 127); hdr += n.to_bytes(8, "big")
    hdr += mask
    s.sendall(bytes(hdr) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))


def recv_exact(s, n):
    data = b""
    while len(data) < n:
        chunk = s.recv(n - len(data))
        if not chunk:
            return data
        data += chunk
    return data


def read_frames(s, secs):
    """Yield decoded text frames (server→client is unmasked) for `secs`."""
    s.settimeout(0.5)
    end = time.time() + secs
    while time.time() < end:
        try:
            hd = recv_exact(s, 2)
        except socket.timeout:
            continue
        except OSError:
            return
        if len(hd) < 2:
            return
        opcode, ln = hd[0] & 0x0F, hd[1] & 0x7F
        if ln == 126:
            ln = int.from_bytes(recv_exact(s, 2), "big")
        elif ln == 127:
            ln = int.from_bytes(recv_exact(s, 8), "big")
        try:
            data = recv_exact(s, ln)
        except socket.timeout:
            return
        if opcode == 0x1:
            yield data.decode("utf-8", "replace")


def main():
    frame = parse_frame(sys.argv[1:])
    s = ws_connect()
    send_text(s, json.dumps(frame))
    print(f"sent: {json.dumps(frame)}")
    for text in read_frames(s, LISTEN_SECS):
        try:
            obj = json.loads(text)
        except ValueError:
            continue
        t = obj.get("type")
        if t == "accounts":                      # the frame you usually care about
            print(json.dumps(obj, indent=1))
        else:
            print(f"← {t}")
    s.close()


if __name__ == "__main__":
    main()
