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

PROTOCOL_VERSION = "2025-06-18"

# (name, description, inputSchema). Kept flat + explicit — a model reads these
# verbatim and should be able to act with no further docs.
_S = lambda props, req=(): {"type": "object", "properties": props, "required": list(req)}  # noqa: E731
_STR = {"type": "string"}
_BOOL = {"type": "boolean"}

TOOLS = [
    ("get_world", "Snapshot of the whole fleet: machines→projects→sessions with "
        "status (blocked|working|idle), digest, blocked_on, task link, idle_for_s, "
        "and last_answer. The cheap way to see everything at once.", _S({})),
    ("get_attention", "Ranked queue of sessions needing a human, each with a "
        "suggested_action verb. Read this first to triage.", _S({})),
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
            return v.get_world()
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
