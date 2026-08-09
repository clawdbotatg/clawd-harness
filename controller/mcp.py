"""A minimal MCP server (stdio, JSON-RPC 2.0) over the intent verbs.

This is the "MCP to read and MCP to write" surface: any MCP client — Claude Code,
a cron agent, the bundled brain — drives the whole fleet through it. Pure stdlib,
newline-delimited JSON-RPC on stdin/stdout (the MCP stdio transport).

Resources = the read shape (world / attention / tasks). Tools = the verbs. The
dispatch (`handle`) is split from the stdio loop so it's unit-testable without a
subprocess. Tool schemas are kept terse + self-describing so a model needs no
out-of-band docs to drive them.
"""
import json
import sys
import urllib.request

PROTOCOL_VERSION = "2025-06-18"

# (name, description, inputSchema). Kept flat + explicit — a model reads these
# verbatim and should be able to act with no further docs.
_S = lambda props, req=(): {"type": "object", "properties": props, "required": list(req)}  # noqa: E731
_STR = {"type": "string"}
_BOOL = {"type": "boolean"}
_INT = {"type": "integer"}

TOOLS = [
    ("get_world", "Compact fleet map: machines→projects→sessions, one line per "
        "session (cid/title/status/task/idle_m/digest), per-machine counts, "
        "empty projects as a name list. Bounded — never overflows. Drill down "
        "with machine=/pid= (verbose=true only when scoped). For 'where is X' "
        "questions use `find`, NOT this. Two flags appear only when true: "
        "`engine` (absent ⇒ claude, so `engine:\"codex\"` marks the codex ones) "
        "and `pinned` (parked on the 📌 board = done-but-unverified, NOT idle "
        "work). `kind:\"local\"` on a project = a private folder, no GitHub.",
        _S({"machine": _STR, "pid": _STR, "verbose": _BOOL})),
    ("find", "Deterministic fleet-wide search in ONE call: session titles/"
        "digests/blocked_on/lastAnswer, project names, the task ledger, AND "
        "each machine's transcript store (searched server-side on the machine). "
        "THE way to answer 'which session/task is about X' — never use Bash, "
        "GitHub, or get_world for that. Returns matches with deep links (and "
        "`engine` on the non-claude ones); scope=meta skips transcripts.",
        _S({"query": _STR, "machine": _STR, "scope": _STR, "limit": _INT},
           ["query"])),
    ("transcript_tail", "Last n structured transcript events for one session — "
        "what it actually said and did, including tool calls (a pending "
        "AskUserQuestion's options ride in its tool_use event). n≤50, text "
        "capped. Read this BEFORE answer_prompt, and to retrieve a delegated "
        "session's results.", _S({"machine": _STR, "cid": _STR, "n": _INT},
                                 ["machine", "cid"])),
    ("peek_screen", "De-ANSI'd render of a session's current terminal screen — "
        "the only way to read TUI dialogs that never reach the transcript "
        "(trust prompts, login menus). Use before answering raw keys blind.",
        _S({"machine": _STR, "cid": _STR}, ["machine", "cid"])),
    ("sweep", "One-call check-in bundle: the ranked attention queue enriched "
        "with transcript-tail evidence, deep links, and a suggested clearing "
        "verb per item, plus rollups (idle sessions with no task, stale "
        "in-progress tasks). Use when the operator says 'check in on "
        "everything' / 'what needs me'. Items on a non-claude session carry "
        "`engine` — worth knowing, because a codex session's badge can go silent "
        "while its transcript is fine, so believe the transcript. Pair with "
        "get_pins for the done-but-unverified queue, which a sweep omits.",
        _S({"max_items": _INT})),
    ("get_attention", "Ranked queue of sessions needing a human, each with a "
        "suggested_action verb. Lighter than sweep (no tails/links).", _S({})),
    ("get_pins", "The 📌 pin board: sessions parked as \"coded, but a human "
        "still has to go and check it\". Each carries `test_hint` — one "
        "imperative instruction for the operator ('open /eq during the next "
        "show and verify 30fps'). The honest answer to 'what's outstanding?' "
        "that the attention queue can't give (a pin isn't blocked) and "
        "idle_no_task shouldn't (a pin is deliberately parked). Read-only.",
        _S({"machine": _STR})),
    ("get_accounts", "Subscription health per machine: which Claude login new "
        "sessions spawn under, how much of each plan window is spent, when it "
        "resets — plus codex's plan (read-only, single-login, outside the "
        "router). Use before promising a big multi-session job, and to answer "
        "'are we about to run out of plan?'. There is intentionally no verb to "
        "switch accounts: the harness routes itself, continuously, better than "
        "a turn-based PM can.", _S({"machine": _STR})),
    ("escalate", "Queue a question/decision for the operator. urgency='digest' "
        "(default) batches into ONE periodic push — use it for almost "
        "everything; 'now' pushes immediately — only for actively-breaking "
        "things. Include machine+cid when it's about a session (adds a deep "
        "link).", _S({"question": _STR, "machine": _STR, "cid": _STR,
                      "urgency": _STR}, ["question"])),
    ("remember_note", "Write to YOUR durable memory — it is injected into every "
        "future turn. Use for lasting facts: a machine's quirks, a project's "
        "state, operator guidance. Scope: 'machine:<id>' | 'project:<name>' | "
        "'task:<id>' | 'general'. Not for transient status.",
        _S({"text": _STR, "scope": _STR}, ["text"])),
    ("forget_note", "Drop one note by scope + index (see get_notes).",
        _S({"scope": _STR, "index": _INT}, ["scope", "index"])),
    ("get_notes", "Your full durable memory: priorities + all notes with "
        "indexes (the prompt only carries the newest).", _S({})),
    ("set_priorities", "Replace the operator's standing priorities (ordered "
        "list, highest first). Call ONLY when the operator states or changes "
        "what matters — priorities steer every future turn and sweep.",
        _S({"priorities": {"type": "array", "items": _STR}}, ["priorities"])),
    ("session_digest", "Full current detail for one session.",
        _S({"machine": _STR, "cid": _STR}, ["machine", "cid"])),
    ("open_session", "Build a deep link that opens ONE session in the harness UI — "
        "the user's browser jumps straight to its transcript (or its terminal with "
        "view='tty'). Use whenever the user says 'open' / 'take me to' / 'send me "
        "to' a session. Read-only. The chat renders it as an Open button; include "
        "the returned url in your reply too.",
        _S({"machine": _STR, "cid": _STR, "view": _STR}, ["machine", "cid"])),
    ("open_project", "Build a deep link that opens a project's session list in the "
        "harness UI. Use to send the user to a project. Read-only.",
        _S({"machine": _STR, "pid": _STR}, ["machine", "pid"])),
    ("list_tasks", "The task ledger (PM intent). Optional status filter "
        "(open|in_progress|blocked|review|done).", _S({"status": _STR})),
    ("get_task", "One task by id.", _S({"task_id": _STR}, ["task_id"])),
    ("create_task", "Record an intended unit of work. Bookkeeping only — does not "
        "touch the fleet. acceptance = how you'll know it's done.",
        _S({"goal": _STR, "project": _STR, "acceptance": _STR, "machine": _STR}, ["goal"])),
    ("set_task_status", "Update a task's status.",
        _S({"task_id": _STR, "status": _STR}, ["task_id", "status"])),
    ("note_task", "Append a note to a task's history.",
        _S({"task_id": _STR, "text": _STR}, ["task_id", "text"])),
    ("assign", "Put a task to work: spawn a new session in project `spawn_in` (a "
        "pid) OR reuse `existing` (a cid), link it to the task, and send the goal "
        "as the first message. `engine` picks the CLI for a newly spawned session "
        "— \"claude\" (default) or \"codex\" — and is recorded on the task. WRITE "
        "— needs confirm=true under autonomy=confirm.",
        _S({"task_id": _STR, "machine": _STR, "spawn_in": _STR, "existing": _STR,
            "engine": _STR, "confirm": _BOOL}, ["task_id", "machine"])),
    ("create_pipeline", "Plan a MULTI-STEP task: one goal, an ordered list of "
        "steps, a session per step, and each step may run a different engine. "
        "This is the tool for \"research it with claude, have codex "
        "double-check that, then let claude write the final report\" — a shape "
        "nothing else here can express. Each step is "
        "{role, engine, prompt, pid?, reuse?}: `role` is a short label; "
        "`engine` is claude|codex; `prompt` is what to ask (every EARLIER "
        "step's final answer is folded in automatically, so write 'critique the "
        "research above', not a placeholder); `pid` is the project to spawn in; "
        "`reuse`=<earlier step number> sends this step to THAT step's session "
        "instead of a fresh one (how a step keeps its own earlier work in "
        "context). Max 6 steps. Bookkeeping only — nothing runs until "
        "start_pipeline.",
        _S({"goal": _STR,
            "steps": {"type": "array", "items": {"type": "object"}},
            "machine": _STR, "project": _STR, "acceptance": _STR},
           ["goal", "steps"])),
    ("start_pipeline", "Run step 1 of a pipeline. `machine`/`pid` are the "
        "defaults for steps that don't name their own. WRITE — and this is the "
        "ONE approval for the WHOLE chain: confirming it approves every step, "
        "which is why later steps then advance on their own without asking "
        "again. Say so when you relay the proposal.",
        _S({"task_id": _STR, "machine": _STR, "pid": _STR, "confirm": _BOOL},
           ["task_id"])),
    ("advance_pipeline", "Close the pipeline's running step (recording its "
        "session's final answer) and start the next; on the last step, mark the "
        "task `review` and return the report. Normally you do NOT call this — it "
        "fires by itself the moment a step's session finishes. Call it by hand "
        "to unstick a chain, with force=true to close a step whose session "
        "produced nothing readable.",
        _S({"task_id": _STR, "force": _BOOL}, ["task_id"])),
    ("get_step_output", "One pipeline step's FULL recorded answer — get_task "
        "truncates them. This is where a finished pipeline's final report is.",
        _S({"task_id": _STR, "n": _INT}, ["task_id", "n"])),
    ("ask", "Send a message/prompt to a session. WRITE.",
        _S({"machine": _STR, "cid": _STR, "text": _STR, "confirm": _BOOL},
           ["machine", "cid", "text"])),
    ("answer_prompt", "Clear a `waiting` session by sending raw keys to its TUI "
        r"menu (e.g. '1\r' to pick option 1, '\r' for the default). Check "
        "blocked_on first. WRITE.",
        _S({"machine": _STR, "cid": _STR, "keys": _STR, "confirm": _BOOL},
           ["machine", "cid", "keys"])),
    ("interrupt", "Send ESC to a session to cancel its current prompt/turn. WRITE.",
        _S({"machine": _STR, "cid": _STR, "confirm": _BOOL}, ["machine", "cid"])),
    ("create_project", "Create a new GitHub repo + adopt it as a project. WRITE.",
        _S({"machine": _STR, "name": _STR, "confirm": _BOOL}, ["machine", "name"])),
    ("clone_project", "Clone a repo and adopt it as a project. WRITE.",
        _S({"machine": _STR, "repo_url": _STR, "confirm": _BOOL}, ["machine", "repo_url"])),
    ("spawn", "Start a NEW session in a project (`pid`) on a machine, with no task "
        "attached. Returns its cid so you can `ask` it next. For task-bound work use "
        "`assign`. `engine` picks the agent CLI: \"claude\" (default) or \"codex\". "
        "WRITE — needs confirm=true under autonomy=confirm.",
        _S({"machine": _STR, "pid": _STR, "engine": _STR, "confirm": _BOOL},
           ["machine", "pid"])),
    ("close", "Close/kill a session: its claude is terminated and dropped from the "
        "harness (the project stays). Irreversible — check session_digest first. WRITE.",
        _S({"machine": _STR, "cid": _STR, "confirm": _BOOL}, ["machine", "cid"])),
    ("pin", "📌 Park a finished session on the pin board (on=true, default) or "
        "restore it to the tab strip (on=false). The move for \"the work is "
        "done but a human still has to verify it\": the session stays alive and "
        "promptable, leaves the tab strip, and the harness derives its blue "
        "test-hint line. Side effect: it also /compacts the session once idle, "
        "so pin things that are parked, not mid-thought. WRITE.",
        _S({"machine": _STR, "cid": _STR, "on": _BOOL, "confirm": _BOOL},
           ["machine", "cid"])),
    ("add_local_project", "Adopt an existing folder on that machine's disk as a "
        "PRIVATE local project: sessions run in it normally, but the harness "
        "never runs gh/git-remote against it and it has no repo URL. Use when "
        "the operator names a folder that isn't a GitHub repo. WRITE.",
        _S({"machine": _STR, "path": _STR, "confirm": _BOOL},
           ["machine", "path"])),
    ("remove_project", "Detach a local (private) project: drop it from the "
        "registry and close its sessions. NEVER deletes the folder. Refused for "
        "gh projects — those go away by deleting the folder on the box, which "
        "is the operator's job, not yours. WRITE.",
        _S({"machine": _STR, "pid": _STR, "confirm": _BOOL}, ["machine", "pid"])),
]

RESOURCES = [
    {"uri": "fleet://world", "name": "world", "mimeType": "application/json",
     "description": "Full fleet state (machines→projects→sessions)."},
    {"uri": "fleet://attention", "name": "attention", "mimeType": "application/json",
     "description": "Ranked queue of sessions needing a human."},
    {"uri": "fleet://tasks", "name": "tasks", "mimeType": "application/json",
     "description": "The task ledger."},
]


class MCPServer:
    def __init__(self, verbs):
        self.v = verbs

    # -- the verb/resource bridges --------------------------------------------
    def call_tool(self, name, a):
        v = self.v
        if name == "get_world":
            return v.get_world(a.get("machine"), a.get("pid"),
                               a.get("verbose", False))
        if name == "find":
            return v.find(a["query"], a.get("machine"),
                          a.get("scope", "all"), a.get("limit", 20))
        if name == "transcript_tail":
            return v.transcript_tail(a["machine"], a["cid"], a.get("n", 30))
        if name == "peek_screen":
            return v.peek_screen(a["machine"], a["cid"])
        if name == "sweep":
            return v.sweep(a.get("max_items", 20))
        if name == "get_attention":
            return v.get_attention()
        if name == "get_pins":
            return v.get_pins(a.get("machine"))
        if name == "get_accounts":
            return v.get_accounts(a.get("machine"))
        if name == "escalate":
            return v.escalate(a["question"], a.get("machine"), a.get("cid"),
                              a.get("urgency", "digest"))
        if name == "remember_note":
            return v.remember_note(a["text"], a.get("scope", "general"))
        if name == "forget_note":
            return v.forget_note(a["scope"], a["index"])
        if name == "get_notes":
            return v.get_notes()
        if name == "set_priorities":
            return v.set_priorities(a.get("priorities") or [])
        if name == "session_digest":
            return v.session_digest(a["machine"], a["cid"])
        if name == "open_session":
            return v.open_session(a["machine"], a["cid"], a.get("view", "transcript"))
        if name == "open_project":
            return v.open_project(a["machine"], a["pid"])
        if name == "list_tasks":
            return v.list_tasks(a.get("status"))
        if name == "get_task":
            return v.get_task(a["task_id"])
        if name == "create_task":
            return v.create_task(a["goal"], a.get("project"), a.get("acceptance", ""), a.get("machine"))
        if name == "set_task_status":
            return v.set_task_status(a["task_id"], a["status"])
        if name == "note_task":
            return v.note_task(a["task_id"], a["text"])
        if name == "create_pipeline":
            return v.create_pipeline(a["goal"], a.get("steps") or [],
                                     a.get("project"), a.get("acceptance", ""),
                                     a.get("machine"))
        if name == "start_pipeline":
            return v.start_pipeline(a["task_id"], a.get("machine"), a.get("pid"),
                                    a.get("confirm", False))
        if name == "advance_pipeline":
            return v.advance_pipeline(a["task_id"], a.get("force", False))
        if name == "get_step_output":
            return v.get_step_output(a["task_id"], a["n"])
        if name == "assign":
            return v.assign(a["task_id"], a["machine"], a.get("spawn_in"),
                            a.get("existing"), a.get("confirm", False),
                            engine=a.get("engine", "claude"))
        if name == "ask":
            return v.ask(a["machine"], a["cid"], a["text"], a.get("confirm", False))
        if name == "answer_prompt":
            return v.answer_prompt(a["machine"], a["cid"], a["keys"], a.get("confirm", False))
        if name == "interrupt":
            return v.interrupt(a["machine"], a["cid"], a.get("confirm", False))
        if name == "create_project":
            return v.create_project(a["machine"], a["name"], a.get("confirm", False))
        if name == "clone_project":
            return v.clone_project(a["machine"], a["repo_url"], a.get("confirm", False))
        if name == "spawn":
            return v.spawn(a["machine"], a["pid"], a.get("confirm", False),
                           engine=a.get("engine", "claude"))
        if name == "close":
            return v.close(a["machine"], a["cid"], a.get("confirm", False))
        if name == "pin":
            return v.pin(a["machine"], a["cid"], a.get("on", True),
                         a.get("confirm", False))
        if name == "add_local_project":
            return v.add_local_project(a["machine"], a["path"],
                                       a.get("confirm", False))
        if name == "remove_project":
            return v.remove_project(a["machine"], a["pid"], a.get("confirm", False))
        raise ValueError(f"unknown tool: {name}")

    def read_resource(self, uri):
        if uri == "fleet://world":
            return self.v.get_world()
        if uri == "fleet://attention":
            return self.v.get_attention()
        if uri == "fleet://tasks":
            return self.v.list_tasks()
        raise ValueError(f"unknown resource: {uri}")

    # -- JSON-RPC dispatch -----------------------------------------------------
    def handle(self, msg):
        """Return a response dict, or None for notifications (no id / initialized)."""
        mid = msg.get("id")
        method = msg.get("method")
        p = msg.get("params") or {}
        try:
            if method == "initialize":
                return self._ok(mid, {
                    "protocolVersion": p.get("protocolVersion") or PROTOCOL_VERSION,
                    "capabilities": {"tools": {}, "resources": {}},
                    "serverInfo": {"name": "clawd-controller", "version": "0.1"}})
            if method in ("notifications/initialized", "initialized"):
                return None
            if method == "ping":
                return self._ok(mid, {})
            if method == "tools/list":
                return self._ok(mid, {"tools": [
                    {"name": n, "description": d, "inputSchema": s} for n, d, s in TOOLS]})
            if method == "tools/call":
                result = self.call_tool(p.get("name"), p.get("arguments") or {})
                is_err = isinstance(result, dict) and result.get("ok") is False \
                    and not result.get("needs_confirm")
                return self._ok(mid, {
                    "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                    "isError": bool(is_err)})
            if method == "resources/list":
                return self._ok(mid, {"resources": RESOURCES})
            if method == "resources/read":
                uri = p.get("uri")
                data = self.read_resource(uri)
                return self._ok(mid, {"contents": [
                    {"uri": uri, "mimeType": "application/json",
                     "text": json.dumps(data, indent=2)}]})
            if mid is None:
                return None
            return self._err(mid, -32601, f"method not found: {method}")
        except Exception as e:
            if mid is None:
                return None
            return self._err(mid, -32603, f"{type(e).__name__}: {e}")

    @staticmethod
    def _ok(mid, result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    @staticmethod
    def _err(mid, code, message):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}

    # -- stdio loop ------------------------------------------------------------
    def serve_stdio(self, infile=None, outfile=None):
        infile = infile or sys.stdin
        outfile = outfile or sys.stdout
        for line in infile:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            resp = self.handle(msg)
            if resp is not None:
                outfile.write(json.dumps(resp) + "\n")
                outfile.flush()


def _http_json(url, payload=None, timeout=60):
    """POST payload (or GET when None) to a local serve endpoint, parse JSON."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


class ProxyMCPServer(MCPServer):
    """Stdio MCP front-end that proxies every tool/resource to a running
    `serve` process's HTTP API instead of building its own fleet connection.

    `claude -p` spawns a fresh `python3 -m controller mcp` per turn. If that
    subprocess dialed the relay itself it would join as a SECOND `role=controller`
    under the reserved `__ctl__` ident, and the relay's same-ident supersede rule
    makes the two controller processes kill each other's links for the whole turn
    — the world-flap bug (1,000+ reconnects/day, spawn focus-waits expiring,
    per-turn last_answer amnesia). Proxying into serve also means tools see the
    live Guard (autonomy flips apply instantly) and there is a single writer on
    the ledger file."""

    def __init__(self, base_url):
        super().__init__(verbs=None)
        self.base = base_url.rstrip("/")

    def call_tool(self, name, a):
        # 60s covers the slowest verb (assign/spawn's focus wait + fan-outs).
        out = _http_json(self.base + "/api/tool", {"name": name, "args": a}, timeout=60)
        return out.get("result") if isinstance(out, dict) and "result" in out else out

    def read_resource(self, uri):
        path = {"fleet://world": "/api/world",
                "fleet://attention": "/api/attention",
                "fleet://tasks": "/api/tasks"}.get(uri)
        if not path:
            raise ValueError(f"unknown resource: {uri}")
        return _http_json(self.base + path)
