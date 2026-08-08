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
import os
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
    from .notes import NotesStore
    verbs = Verbs(world, ledger, clients, guard, notes=NotesStore(config.NOTES_PATH))
    return verbs, clients, guard, ledger, reactor


def _serve_alive(base, timeout=1.5):
    """True if a controller serve process answers on its chat port."""
    import urllib.request
    try:
        with urllib.request.urlopen(base + "/api/state", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def make_brain(guard, notes=None):
    """The one PM brain: a minimal claude-p-agent (`claude -p` + the fleet MCP
    tools, on your subscription). See controller/agent.py."""
    from .agent import AgentBrain
    return AgentBrain(guard, notes=notes)


def main(argv):
    mode = argv[0] if argv else "serve"

    if mode == "mcp":
        # MCP stdio server. Keep stdout clean for JSON-RPC; logs go to stderr.
        # When spawned by the PM brain (write_mcp_config sets the opt-in env),
        # proxy tools through the serve process's HTTP API instead of building
        # a second fleet connection: a second relay dial would collide on the
        # reserved __ctl__ ident and the two controller processes would
        # supersede each other's links all turn (the world-flap bug). The
        # opt-in gate keeps a hand-run/test `-m controller mcp` (which may
        # point at a different harness entirely) standalone.
        base = f"http://127.0.0.1:{config.CHAT_PORT}"
        if os.environ.get("CONTROLLER_MCP_PROXY") == "1" and _serve_alive(base):
            from .mcp import ProxyMCPServer
            print(f"[mcp] proxying tools to serve at {base}", file=sys.stderr, flush=True)
            ProxyMCPServer(base).serve_stdio()
            return 0
        print(f"[mcp] no serve at {base} — standalone fleet connection",
              file=sys.stderr, flush=True)
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
        import threading
        from . import chat_server
        verbs, clients, guard, ledger, reactor = build()
        brain = make_brain(guard, notes=verbs.notes)
        from .threads import Threads
        threads = Threads(config.THREADS_PATH)
        # One lock serializes EVERY brain turn — operator chats (HTTP +
        # Telegram) and autopilot turns alike — so an event-driven turn can
        # never interleave with a conversation and repoint the memory key
        # mid-flight.
        turn_lock = threading.Lock()

        # a thin façade the chat server drives. One PM brain (a minimal claude-p-agent),
        # but multiple conversation threads (the chat analog of per-project sessions). A
        # thread's memory IS its key: before each (serialized) turn we point the brain at
        # `pm-<tid>`, and claude-p-agent's engine loads/resumes/saves that conversation's
        # session. The threads file keeps only the display transcript; the engine owns
        # the session. Clearing/resetting a thread forgets its engine memory.
        def _key(tid=None):
            return f"pm-{tid or threads.current}"

        # AI thread naming — same feature a session gets: an LLM title + running
        # tldr, refreshed at prompt 1 then every 3 (controller/naming.py). Fired
        # async after a turn's reply is recorded, so it never adds latency to the
        # turn; unconfigured gateway → no-op and first-prompt titles stand.
        # Operator threads only: the autopilot thread is found BY its fixed
        # "🤖 autopilot" title (see _auto_tid), so it must never be renamed.
        from . import naming

        def maybe_name_thread(tid):
            if not naming.configured():
                return
            msgs = threads.messages(tid)
            count = sum(1 for m in msgs if m.get("who") == "me")
            if not naming.name_at_prompt(count):
                return

            def _run():
                title, desc = naming.generate_thread_name(
                    naming.transcript_tail(msgs))
                if title or desc:
                    threads.set_name(tid, title=title, desc=desc)

            threading.Thread(target=_run, daemon=True,
                             name="pm-thread-naming").start()

        class Router:
            label = "router"

            def reset(self):                 # back-compat: clear the current thread
                k = _key()
                threads.clear()
                brain.forget_conversation(k)

            def chat(self, text):
                tid = threads.current
                brain.conversation_key = _key(tid)
                # Record the user turn BEFORE the brain runs: a turn can take minutes,
                # and any transcript fetch in that window (view re-entry, reload,
                # another device) must already show the prompt — not lose it until
                # the reply lands.
                threads.record("me", text, tid=tid)
                threads.persist()
                out = brain.chat(text)
                threads.record("bot", out.get("reply", ""), out.get("trace"), tid=tid)
                threads.persist()
                maybe_name_thread(tid)
                return out

            def chat_stream(self, text, emit):
                """Streaming variant — same bookkeeping as chat(), but the brain fires
                emit(kind, text) per event so a front-end (Telegram) shows live progress."""
                tid = threads.current
                brain.conversation_key = _key(tid)
                threads.record("me", text, tid=tid)
                threads.persist()
                out = brain.chat_stream(text, emit)
                threads.record("bot", out.get("reply", ""), out.get("trace"), tid=tid)
                threads.persist()
                maybe_name_thread(tid)
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
                k = _key(tid)
                threads.clear(tid)
                brain.forget_conversation(k)
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

        # -- autopilot: reactor events → budgeted PM turns -------------------
        # Turns land in a dedicated "🤖 autopilot" thread so every decision it
        # makes unattended is reviewable in the normal chat UI.
        from .autopilot import Autopilot

        def _auto_tid():
            for tid in threads.order:
                if threads.threads[tid]["title"] == "🤖 autopilot":
                    return tid
            return threads.new(title="🤖 autopilot", select=False)

        def run_pm(kind, prompt):
            with turn_lock:
                tid = _auto_tid()
                brain.conversation_key = f"pm-auto-{time.strftime('%Y%m%d')}"
                threads.record("me", prompt, tid=tid)
                threads.persist()
                out = brain.chat(prompt)
                threads.record("bot", out.get("reply", ""), out.get("trace"), tid=tid)
                threads.persist()
                return out.get("reply", "")

        autopilot = Autopilot(
            run_pm, verbs, ledger, guard,
            notify=(tg.notify if tg else None),
            enabled=config.AUTOPILOT, toggle_path=config.AUTOPILOT_PATH,
            cooldown_s=config.AUTOPILOT_COOLDOWN,
            verify_cooldown_s=config.AUTOPILOT_VERIFY_COOLDOWN,
            own_action_s=config.AUTOPILOT_OWN_ACTION_S,
            max_per_hour=config.AUTOPILOT_MAX_PER_HOUR,
            max_per_day=config.AUTOPILOT_MAX_PER_DAY,
            digest_window_s=config.DIGEST_WINDOW).start()
        verbs.escalate_sink = autopilot.escalate
        reactor.on_event(autopilot.feed)
        print(f"[auto] autopilot {'ON' if autopilot.enabled else 'off'} "
              f"(budget {autopilot.max_per_hour}/h {autopilot.max_per_day}/d, "
              f"digest every {int(autopilot.digest_window_s)}s)", flush=True)

        # Optional scheduled sweep (CONTROLLER_SWEEP_EVERY seconds, 0 = off):
        # a deterministic verbs.sweep() — no LLM turn, so it's free — pushed to
        # Telegram as a compact digest, suppressed while nothing changed.
        if config.SWEEP_EVERY > 0:
            import hashlib
            import threading
            sweep_state = {"sig": None}

            def _sweep_loop():
                while True:
                    time.sleep(config.SWEEP_EVERY)
                    try:
                        bundle = verbs.sweep()
                        items = bundle.get("items", [])
                        sig = hashlib.sha1(json.dumps(
                            sorted([i["machine"], i["cid"], i["kind"]] for i in items)
                        ).encode()).hexdigest()
                        if sig == sweep_state["sig"]:
                            continue
                        sweep_state["sig"] = sig
                        if not items:
                            continue
                        lines = [f"🧹 sweep: {len(items)} item(s) need attention"]
                        for i in items[:8]:
                            lines.append(f"• [{i['sev']}] {i['title']} — "
                                         f"{i['summary']} {i.get('url', '')}".strip())
                        msg = "\n".join(lines)
                        print("[sweep] " + msg.replace("\n", " | "), flush=True)
                        if tg:
                            tg.notify(msg)
                    except Exception as e:
                        print(f"[sweep] error: {e}", flush=True)

            threading.Thread(target=_sweep_loop, daemon=True, name="sweep").start()

        # Higher-level reactions: a session crossing into `blocked` (a low-level
        # Claude Code hook) fires a controller event → push it to Telegram. The
        # full event feed is also exposed at /api/notifications for the UI.
        def on_event(e):
            if e["kind"] == "blocked":
                line = f"⏳ needs you — {e['machine']}/{e['cid'][:8]}: {e['summary']}"
                print("[reactor] " + line, flush=True)
                # With the autopilot ON, the raw per-event ping is ITS job to
                # replace (triage it, or batch it into the digest) — pinging
                # here too would double-notify every block.
                if tg and not autopilot.enabled:
                    tg.notify(line)
        reactor.on_event(on_event)

        chat_server.serve_with_router(router, verbs, guard,
                                      lambda: brain.label, config.CHAT_PORT,
                                      reactor=reactor, mcp=debug_mcp,
                                      prompt_brain=prompt_brain,
                                      turn_lock=turn_lock, autopilot=autopilot)
        return 0

    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
