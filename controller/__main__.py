"""Controller entry point.

  python3 -m controller mcp        # MCP stdio server (for `claude -p` / any MCP client)
  python3 -m controller serve      # chat UI + PM brain on CHAT_PORT (default 8799)
  python3 -m controller world      # one-shot: print the world snapshot and exit
  python3 -m controller attention  # one-shot: print the attention queue and exit
  python3 -m controller tasks      # one-shot: print the task ledger and exit

All modes connect to the harness at CONTROLLER_HARNESS_WS (default the local
harness) as a WS client and share the ledger at CONTROLLER_LEDGER. The `serve`
brain is a minimal claude-p-agent (`claude -p` + the fleet MCP tools); see
controller/agent.py.
"""
import json
import sys
import time

from . import config
from .events import Reactor
from .harness_client import HarnessClient
from .ledger import TaskLedger
from .mcp import MCPServer
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
    if config.RELAY_URL:
        # Box mode: drive the whole fleet through the relay's trusted-control path.
        # `clients` IS the fleet's live machine map — World/Verbs see machines come
        # and go as the roster changes.
        from .relay_client import RelayFleet
        fleet = RelayFleet(config.RELAY_URL, config.RELAY_TOKEN, on_hook=reactor.feed).start()
        clients = fleet.machines
        if connect_wait:
            end = time.time() + connect_wait
            while time.time() < end and not fleet.connected:
                time.sleep(0.05)
            # brief grace for the roster + first per-machine state to arrive
            time.sleep(min(1.5, max(0.0, end - time.time()) + 1.5))
    elif config.HARNESS_WS:
        # Laptop/direct mode: one local harness.
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


def make_brain(guard):
    """The one PM brain: a minimal claude-p-agent (`claude -p` + the fleet MCP
    tools, on your subscription). See controller/agent.py."""
    from .agent import AgentBrain
    return AgentBrain(guard)


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
        brain = make_brain(guard)
        from .threads import Threads
        threads = Threads(config.THREADS_PATH)

        # a thin façade the chat server drives. One PM brain (a minimal
        # claude-p-agent), but multiple conversation threads (the chat analog of
        # per-project sessions): each thread keeps its own brain state, swapped
        # in/out of the shared brain around every (serialized) turn.
        STATE_KEY = brain.label

        class Router:
            label = "router"

            @property
            def history(self):
                return brain.history

            def reset(self):                 # back-compat: clear the current thread
                threads.clear()
                brain.reset()

            def chat(self, text):
                brain.import_state(threads.state_for(STATE_KEY))
                out = brain.chat(text)
                threads.save_state(STATE_KEY, brain.export_state())
                threads.record("me", text)
                threads.record("bot", out.get("reply", ""), out.get("trace"))
                threads.persist()
                return out

            # -- thread management (driven by the chat server endpoints) --------
            def list_threads(self):
                return threads.summary()

            def thread_messages(self, tid=None):
                return {"messages": threads.messages(tid)}

            def new_thread(self, title=None):
                threads.new(title=title)
                return threads.summary()

            def select_thread(self, tid):
                ok = threads.select(tid)
                return {"ok": ok, **threads.summary()}

            def clear_thread(self, tid=None):
                threads.clear(tid)
                brain.reset()
                return threads.summary()

            def archive_thread(self, tid=None):
                threads.archive(tid)
                return threads.summary()

        router = Router()
        debug_mcp = MCPServer(verbs)                 # tool runner for the debug page
        # the editable persona (debug page) is the PM brain's own private.md prompt
        prompt_brain = brain

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
                                      lambda: brain.label, config.CHAT_PORT,
                                      reactor=reactor, mcp=debug_mcp, prompt_brain=prompt_brain)
        return 0

    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
