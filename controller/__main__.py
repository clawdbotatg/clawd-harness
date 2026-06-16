"""Controller entry point.

  python3 -m controller mcp        # MCP stdio server (for `claude -p` / any MCP client)
  python3 -m controller serve      # chat UI + PM brain on CHAT_PORT (default 8799)
  python3 -m controller world      # one-shot: print the world snapshot and exit
  python3 -m controller attention  # one-shot: print the attention queue and exit
  python3 -m controller tasks      # one-shot: print the task ledger and exit

All modes connect to the harness at CONTROLLER_HARNESS_WS (default the local
harness) as a WS client and share the ledger at CONTROLLER_LEDGER. Brain backend
for `serve` is CONTROLLER_BRAIN=bankr|claude-code (switchable live in the UI).
"""
import json
import sys
import time

from . import config
from .events import Reactor
from .harness_client import HarnessClient
from .ledger import TaskLedger
from .mcp import MCPServer, TOOLS
from .verbs import Guard, Verbs
from .world import World


def build(connect_wait=4.0):
    """Wire ledger + reactor + harness client(s) + world + verbs. Returns
    (verbs, clients, guard, ledger, reactor). Single machine for now; the
    relay/multi-machine adapter adds more clients (each with on_hook=reactor.feed)
    to the same World + Reactor."""
    ledger = TaskLedger(config.LEDGER_PATH)
    reactor = Reactor(ledger)
    clients = {}
    # The box deploy has no local harness (CONTROLLER_HARNESS_WS empty) → empty
    # world for now; it still chats + serves the debug page. The laptop deploy
    # points at its local harness. (Driving remote machines via the relay's
    # trusted-control path is the next layer.)
    if config.HARNESS_WS:
        client = HarnessClient(config.MACHINE_ID, config.HARNESS_WS,
                               config.harness_token(), on_hook=reactor.feed).start()
        clients = {config.MACHINE_ID: client}
        if connect_wait:
            end = time.time() + connect_wait
            while time.time() < end and not (client.connected and client.projects):
                time.sleep(0.05)
    guard = Guard(autonomy=config.AUTONOMY, rate_per_min=config.RATE_PER_MIN)
    world = World(clients, ledger)
    verbs = Verbs(world, ledger, clients, guard)
    return verbs, clients, guard, ledger, reactor


def make_brain(backend, verbs, clients, guard):
    mcp = MCPServer(verbs)
    if backend == "claude-code":
        from .claude_brain import ClaudeCodeBrain
        return ClaudeCodeBrain(guard)
    from .brain import Brain
    return Brain(call_tool=mcp.call_tool, tools=TOOLS,
                 machine_ids=list(clients.keys()), guard=guard)


def main(argv):
    mode = argv[0] if argv else "serve"

    if mode == "mcp":
        # MCP stdio server. Keep stdout clean for JSON-RPC; logs go to stderr.
        verbs, clients, guard, ledger, reactor = build(connect_wait=3.0)
        MCPServer(verbs).serve_stdio()
        return 0

    if mode in ("world", "attention", "tasks"):
        verbs, clients, guard, ledger, reactor = build()
        out = {"world": verbs.get_world, "attention": verbs.get_attention,
               "tasks": verbs.list_tasks}[mode]()
        print(json.dumps(out, indent=2))
        return 0

    if mode == "serve":
        from . import chat_server
        verbs, clients, guard, ledger, reactor = build()
        backend = config.cfg("CONTROLLER_BRAIN", "bankr").lower()
        brains = {}

        def get_brain(name):
            if name not in brains:
                brains[name] = make_brain(name, verbs, clients, guard)
            return brains[name]

        active = {"backend": backend, "brain": get_brain(backend)}

        # a thin façade the chat server drives; supports live backend switching
        class Router:
            label = "router"

            @property
            def history(self):
                return active["brain"].history

            def reset(self):
                active["brain"].reset()

            def switch(self, name):
                active["backend"] = name
                active["brain"] = get_brain(name)

            def chat(self, text):
                return active["brain"].chat(text)

        router = Router()
        debug_mcp = MCPServer(verbs)                 # tool runner for the debug page
        # the editable system prompt belongs to the bankr brain (claude-code uses
        # its own); expose that instance to the debug page regardless of backend.
        prompt_brain = get_brain("bankr")

        # Telegram front-end (optional) — same brain, on your phone.
        tg = None
        if config.TELEGRAM_TOKEN:
            from .telegram import TelegramBridge
            tg = TelegramBridge(config.TELEGRAM_TOKEN, config.TELEGRAM_ALLOW, router).start()

        # Higher-level reactions: a session crossing into `blocked` (a low-level
        # Claude Code hook) fires a controller event → push it to Telegram. The
        # full event feed is also exposed at /api/notifications for the UI.
        def on_event(e):
            if e["kind"] == "blocked":
                line = f"⏳ needs you — {e['machine']}/{e['cid'][:8]}: {e['summary']}"
                print("[reactor] " + line, flush=True)
                if tg:
                    tg.notify(line)
        reactor.on_event(on_event)

        chat_server.serve_with_router(router, verbs, guard,
                                      lambda: active["backend"], config.CHAT_PORT,
                                      reactor=reactor, mcp=debug_mcp, prompt_brain=prompt_brain)
        return 0

    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
