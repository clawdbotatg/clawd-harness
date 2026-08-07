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
        "questions use `find`, NOT this.", _S({"machine": _STR, "pid": _STR,
                                               "verbose": _BOOL})),
    ("find", "Deterministic fleet-wide search in ONE call: session titles/"
        "digests/blocked_on/lastAnswer, project names, the task ledger, AND "
        "each machine's transcript store (searched server-side on the machine). "
        "THE way to answer 'which session/task is about X' — never use Bash, "
        "GitHub, or get_world for that. Returns matches with deep links; "
        "scope=meta skips transcripts.", _S({"query": _STR, "machine": _STR,
                                             "scope": _STR, "limit": _INT},
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
        "everything' / 'what needs me'.", _S({"max_items": _INT})),
    ("get_attention", "Ranked queue of sessions needing a human, each with a "
        "suggested_action verb. Lighter than sweep (no tails/links).", _S({})),
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
        "as the first message. WRITE — needs confirm=true under autonomy=confirm.",
        _S({"task_id": _STR, "machine": _STR, "spawn_in": _STR, "existing": _STR,
            "confirm": _BOOL}, ["task_id", "machine"])),
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
        "`assign`. WRITE — needs confirm=true under autonomy=confirm.",
        _S({"machine": _STR, "pid": _STR, "confirm": _BOOL}, ["machine", "pid"])),
    ("close", "Close/kill a session: its claude is terminated and dropped from the "
        "harness (the project stays). Irreversible — check session_digest first. WRITE.",
        _S({"machine": _STR, "cid": _STR, "confirm": _BOOL}, ["machine", "cid"])),
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
        if name == "assign":
            return v.assign(a["task_id"], a["machine"], a.get("spawn_in"), a.get("existing"), a.get("confirm", False))
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
            return v.spawn(a["machine"], a["pid"], a.get("confirm", False))
        if name == "close":
            return v.close(a["machine"], a["cid"], a.get("confirm", False))
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
