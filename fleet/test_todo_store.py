#!/usr/bin/env python3
"""☑ fleet/todo_store.py — the one op engine both halves share (relay in fleet
mode, the harness registry in direct mode).

  1. add trims/clips/mints ids, refuses empty text and a full list
  2. done/undone/rm find by id OR a unique substring; ambiguity is refused
  3. clear drops done items only; an iron with nothing left vanishes from the map
  4. order: named open items first, forgotten ones keep their order, done stay last
  5. clean_todos drops junk, dedupes ids, clips, and is idempotent
  6. render matches the operator's `todo` CLI shape

Run: python3 fleet/test_todo_store.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import todo_store as ts

FAILS = []


def check(name, ok, detail=""):
    print(f"  {'✓' if ok else '✗ FAIL'} {name}" + ("" if ok or not detail else f" — {detail}"))
    if not ok:
        FAILS.append(name)


def test_add():
    todos = {}
    ok, msg, it = ts.apply_op(todos, "i1", "add", text="   ", key="k")
    check("empty add refused, no list created", not ok and "i1" not in todos, msg)
    ok, msg, it = ts.apply_op(todos, "i1", "add", text="  ship the thing  " + "x" * 500, key="k", via="page", now=5.0)
    check("add trims + clips + mints an id", ok and it["text"].startswith("ship the thing") and len(it["text"]) == ts.TEXT_MAX
          and len(it["id"]) == 8 and it["created"] == 5.0 and not it["done"] and it["key"] == "k" and it["via"] == "page", msg)
    check("message names the id", f"[{it['id']}]" in msg, msg)
    ok, msg, _ = ts.apply_op(todos, "", "add", text="x")
    check("blank iron refused", not ok, msg)
    ok, msg, _ = ts.apply_op(todos, "i1", "bogus", text="x")
    check("unknown op refused", not ok and "unknown op" in msg, msg)
    for n in range(ts.MAX_ITEMS - 1):
        ts.apply_op(todos, "i1", "add", text=f"item {n}")
    ok, msg, _ = ts.apply_op(todos, "i1", "add", text="one too many")
    check("full list refuses an add, loudly", not ok and "full" in msg and len(todos["i1"]) == ts.MAX_ITEMS, msg)


def test_done_find():
    todos = {}
    _, _, a = ts.apply_op(todos, "i1", "add", text="wire the mic")
    _, _, b = ts.apply_op(todos, "i1", "add", text="test the mic on the phone")
    ok, msg, it = ts.apply_op(todos, "i1", "done", ref="mic")
    check("ambiguous words refused", not ok and "2 items match" in msg, msg)
    ok, msg, it = ts.apply_op(todos, "i1", "done", ref="phone", now=9.0)
    check("unique substring checks off (case-insensitive) + stamps doneAt", ok and it is b and b["done"] and b["doneAt"] == 9.0, msg)
    ok, msg, it = ts.apply_op(todos, "i1", "done", ref=b["id"])
    check("already done is a no-op refusal", not ok and "already done" in msg, msg)
    ok, msg, it = ts.apply_op(todos, "i1", "undone", ref=b["id"])
    check("undone by id reopens + clears doneAt", ok and not b["done"] and b["doneAt"] == 0.0, msg)
    ok, msg, it = ts.apply_op(todos, "i1", "rm", ref="WIRE")
    check("rm by words removes", ok and it is a and todos["i1"] == [b], msg)
    ok, msg, _ = ts.apply_op(todos, "i1", "rm", ref="nope")
    check("rm of nothing is refused", not ok and "no item matches" in msg, msg)
    ok, msg, _ = ts.apply_op(todos, "i1", "done", ref="")
    check("empty ref asks for one", not ok, msg)


def test_clear_and_vanish():
    todos = {}
    _, _, a = ts.apply_op(todos, "i1", "add", text="a")
    _, _, b = ts.apply_op(todos, "i1", "add", text="b")
    ts.apply_op(todos, "i1", "done", ref=a["id"])
    ok, msg, _ = ts.apply_op(todos, "i1", "clear")
    check("clear drops the done item only", ok and todos["i1"] == [b] and "1 done" in msg, msg)
    ok, msg, _ = ts.apply_op(todos, "i1", "clear")
    check("clear with nothing done is unchanged", not ok and todos["i1"] == [b], msg)
    ts.apply_op(todos, "i1", "rm", ref=b["id"])
    check("the last rm removes the iron's entry from the map", "i1" not in todos)


def test_order():
    todos = {}
    ids = [ts.apply_op(todos, "i1", "add", text=t)[2]["id"] for t in ("a", "b", "c", "d")]
    ts.apply_op(todos, "i1", "done", ref=ids[1])                          # b done
    ok, msg, _ = ts.apply_op(todos, "i1", "order", ids=[ids[3], "zzz", ids[0]])
    got = [i["id"] for i in todos["i1"]]
    check("named open first, forgotten open next, done last, unknown skipped",
          ok and got == [ids[3], ids[0], ids[2], ids[1]], str(got))
    ok, msg, _ = ts.apply_op(todos, "i1", "order", ids=[ids[3], ids[0]])
    check("same order is a no-op", not ok and "unchanged" in msg, msg)
    ok, msg, _ = ts.apply_op(todos, "i1", "order", ids=[ids[1]])
    check("naming only a done item can't move it up", not ok and [i["id"] for i in todos["i1"]] == got)


def test_clean():
    raw = {"i1": [{"id": "aa", "text": " keep ", "done": 1, "created": "x", "doneAt": True, "key": 7, "via": "agent" * 20},
                  {"id": "aa", "text": "dup"}, {"id": "", "text": "no id"}, {"id": "bb", "text": "   "}, "junk",
                  {"id": "cc", "text": "y" * 999}],
           "": [{"id": "x", "text": "blank iron"}], "i2": "not a list", "i3": []}
    c = ts.clean_todos(raw)
    check("junk dropped, dedupe by id, empty irons gone", set(c) == {"i1"} and [i["id"] for i in c["i1"]] == ["aa", "cc"], str(c))
    a = c["i1"][0]
    check("fields coerced + clipped", a["text"] == "keep" and a["done"] is True and a["created"] == 0.0 and a["doneAt"] == 0.0
          and a["key"] == "" and len(a["via"]) == 24 and len(c["i1"][1]["text"]) == ts.TEXT_MAX, str(a))
    check("clean is idempotent", ts.clean_todos(c) == c)
    check("non-dict → empty", ts.clean_todos([1, 2]) == {} and ts.clean_todos(None) == {})


def test_render():
    todos = {}
    _, _, a = ts.apply_op(todos, "i1", "add", text="open one")
    _, _, b = ts.apply_op(todos, "i1", "add", text="done one")
    ts.apply_op(todos, "i1", "done", ref=b["id"])
    check("render: open only by default", ts.render(todos["i1"]) == f"[ ] {a['id']}  open one")
    check("render --all adds done struck", ts.render(todos["i1"], all_=True) == f"[ ] {a['id']}  open one\n[x] {b['id']}  done one")
    check("render empty", ts.render([]) == "nothing to do" and ts.render([], all_=True) == "list is empty")


if __name__ == "__main__":
    for t in (test_add, test_done_find, test_clear_and_vanish, test_order, test_clean, test_render):
        print(t.__name__)
        t()
    print("\nOK" if not FAILS else f"\nFAILED: {FAILS}")
    sys.exit(1 if FAILS else 0)
