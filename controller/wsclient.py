"""Re-export the fleet's stdlib WebSocket helpers.

The controller is a harness *client* (it dials the harness/relay over WS, exactly
like the fleet worker), so it reuses `fleet/fleet_ws.py` — a generic RFC 6455
helper with **no** harness imports. We add the fleet dir to sys.path here so the
rest of the package can `from .wsclient import ...` without path games.

Boundary note: importing `fleet_ws` (a transport util) is fine; the controller
still never imports `server.py` or reaches into harness internals.
"""
import os
import sys

_FLEET = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fleet")
if _FLEET not in sys.path:
    sys.path.insert(0, _FLEET)

from fleet_ws import (  # noqa: E402
    client_connect,
    ws_send,
    ws_read_message,
    server_accept_headers,
)

__all__ = ["client_connect", "ws_send", "ws_read_message", "server_accept_headers"]
