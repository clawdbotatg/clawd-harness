"""☑ per-iron to-do lists — the ONE implementation both halves share.

An iron ("irons in the fire", a named group of projects) can carry a short
shared to-do list: what's still open across the whole effort, left there by the
operator from the page or by a session (`harness-todo`) when asked. It is NOT
the operator's life list (todo.atg.link — `todo` CLI) and never feeds it: eight
issues from one project belong on that project's iron, not on his phone.

The list is item-level: every write is one op (add / done / undone / rm / clear
/ order) applied by the owner of the store — the relay in fleet mode (the lists
must span machines, like irons), the harness registry in direct mode — and the
whole snapshot is broadcast after. Two phones and three sessions never clobber
each other because nobody ever writes the whole list.

Pure functions, no I/O: `apply_op` mutates a `{iron_id: [item, ...]}` dict in
place and reports what happened. `fleet/relay.py` and `server.py` both import
this module (server.py by path — fleet code never imports server.py, the other
direction is fine). Bounds are enforced HERE, once, for both modes.

item = {"id": 8 hex, "text": ≤ TEXT_MAX code points, "done": bool,
        "created": float, "doneAt": float, "key": "" | fleet projectKey / pid,
        "via": "" | "agent" | "page"}
"""
import secrets
import time

MAX_ITEMS = 200      # per iron (open + done); an add past this is refused, loudly
TEXT_MAX = 300       # code points; a to-do is a line, not a spec
OPS = ("add", "done", "undone", "rm", "clear", "order")


def _clip(s, n):
    s = "" if not isinstance(s, str) else s
    return s[:n]


def clean_item(e):
    """Bounded-not-trusted (same stance as clean_irons): anything that isn't an
    item with an id and text is dropped, everything else is clipped."""
    if not isinstance(e, dict):
        return None
    iid, text = e.get("id"), e.get("text")
    if not (isinstance(iid, str) and 0 < len(iid) <= 32):
        return None
    if not (isinstance(text, str) and text.strip()):
        return None
    created = e.get("created")
    done_at = e.get("doneAt")
    return {"id": iid,
            "text": _clip(text.strip(), TEXT_MAX),
            "done": bool(e.get("done")),
            "created": created if isinstance(created, (int, float)) and not isinstance(created, bool) else 0.0,
            "doneAt": done_at if isinstance(done_at, (int, float)) and not isinstance(done_at, bool) else 0.0,
            "key": _clip(e.get("key") if isinstance(e.get("key"), str) else "", 512),
            "via": _clip(e.get("via") if isinstance(e.get("via"), str) else "", 24)}


def clean_todos(raw):
    """{iron_id: [item...]} from disk or the wire → the same shape, sanitized.
    Empty lists are dropped so a deleted iron's leftovers don't linger forever."""
    out = {}
    if not isinstance(raw, dict):
        return out
    for iron, items in raw.items():
        if not (isinstance(iron, str) and 0 < len(iron) <= 64) or not isinstance(items, list):
            continue
        seen, keep = set(), []
        for e in items[:MAX_ITEMS]:
            it = clean_item(e)
            if it is None or it["id"] in seen:
                continue
            seen.add(it["id"])
            keep.append(it)
        if keep:
            out[iron] = keep
    return out


def new_id():
    return secrets.token_hex(4)


def find(items, ref):
    """An item by exact id, else by a unique case-insensitive substring of its
    text (what `harness-todo done <words>` types). Returns (item, error)."""
    ref = (ref or "").strip()
    if not ref:
        return None, "which item? give its id or a few words of it"
    for it in items:
        if it["id"] == ref:
            return it, ""
    low = ref.lower()
    hits = [it for it in items if low in it["text"].lower()]
    if len(hits) == 1:
        return hits[0], ""
    if not hits:
        return None, f"no item matches {ref!r}"
    return None, f"{len(hits)} items match {ref!r} — use the id"


def apply_op(todos, iron, op, text="", ref="", ids=None, key="", via="", now=None):
    """Apply one op to `todos[iron]` in place.

    Returns (changed: bool, message: str, item: dict|None). `message` is the
    one line a CLI prints / a session reads; on a refused op `changed` is False
    and the message says why. Unknown op → refused. An iron id that isn't in
    the caller's iron list is the CALLER's check (this module doesn't know the
    irons); an empty iron id is refused here."""
    now = time.time() if now is None else now
    if not (isinstance(iron, str) and 0 < len(iron) <= 64):
        return False, "no iron given", None
    if op not in OPS:
        return False, f"unknown op {op!r}", None
    items = todos.setdefault(iron, [])
    try:
        if op == "add":
            t = _clip((text or "").strip(), TEXT_MAX) if isinstance(text, str) else ""
            if not t:
                return False, "nothing to add (empty text)", None
            if len(items) >= MAX_ITEMS:
                return False, f"this iron's list is full ({MAX_ITEMS} items) — clear done items first", None
            it = {"id": new_id(), "text": t, "done": False, "created": now, "doneAt": 0.0,
                  "key": _clip(key if isinstance(key, str) else "", 512),
                  "via": _clip(via if isinstance(via, str) else "", 24)}
            items.append(it)
            return True, f"added [{it['id']}] {t}", it
        if op in ("done", "undone"):
            it, err = find(items, ref)
            if it is None:
                return False, err, None
            want = op == "done"
            if it["done"] == want:
                return False, f"[{it['id']}] is already {'done' if want else 'open'}: {it['text']}", it
            it["done"] = want
            it["doneAt"] = now if want else 0.0
            return True, f"{'done' if want else 'reopened'} [{it['id']}] {it['text']}", it
        if op == "rm":
            it, err = find(items, ref)
            if it is None:
                return False, err, None
            items.remove(it)
            return True, f"removed [{it['id']}] {it['text']}", it
        if op == "clear":
            n = sum(1 for it in items if it["done"])
            items[:] = [it for it in items if not it["done"]]
            return n > 0, f"cleared {n} done item(s)", None
        if op == "order":
            # The given id list (top first) becomes the order of the OPEN items;
            # ids we don't know are skipped, items the list missed keep their
            # relative order after the named ones, done items stay put at the
            # end (same stance as iron_order: never drop or sink what the
            # caller forgot). Order is a page gesture, never an agent's.
            wanted = [i for i in (ids or []) if isinstance(i, str)]
            by_id = {it["id"]: it for it in items}
            named = [by_id[i] for i in wanted if i in by_id and not by_id[i]["done"]]
            rest_open = [it for it in items if not it["done"] and it not in named]
            done = [it for it in items if it["done"]]
            new = named + rest_open + done
            if new == items:
                return False, "order unchanged", None
            items[:] = new
            return True, "reordered", None
    finally:
        if not items:
            todos.pop(iron, None)
    return False, "unreachable", None


def render(items, all_=False):
    """The `harness-todo` listing: open items in order, then (with all_) done
    ones struck; same shape as the operator's `todo` CLI so a session that
    knows one knows the other."""
    open_ = [it for it in items if not it["done"]]
    lines = [f"[ ] {it['id']}  {it['text']}" for it in open_]
    if all_:
        lines += [f"[x] {it['id']}  {it['text']}" for it in items if it["done"]]
    return "\n".join(lines) if lines else ("nothing to do" if not all_ else "list is empty")
