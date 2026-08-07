"""The autopilot — the PM works when the operator isn't talking to it.

Before this, the brain only thought when spoken to: the Reactor saw a session
go blocked and pinged Telegram, and the ledger's tasks sat in `in_progress`
forever with nothing following up. The autopilot closes both gaps by turning
Reactor events into **budgeted PM turns**:

  blocked (rising edge)      → a TRIAGE turn: read the evidence, clear it if
                               trivial (per the persona's sweep protocol),
                               else escalate with a concrete question.
  turn_done on a task-linked → a VERIFY turn: judge the work against the
  session                      task's acceptance; set review / nudge once /
                               escalate. (The Phase-3 TODO, finally closed.)

Escalations don't spam the phone: the `escalate` verb queues items here and a
window flush sends ONE batched digest ("cleared 2, need you on 3"), with
urgency="now" reserved for immediate pushes.

Runaway guards, because an event-driven agent with write verbs is a loop
hazard: per-session and per-task cooldowns; **own-action suppression** (an
event on a cid the PM itself just wrote to is its own echo — skipped);
hourly + daily turn budgets with a hard stop; paused entirely under
autonomy=readonly; a persisted runtime kill switch (POST /api/autopilot).
Every decision is visible: turns are recorded into a dedicated "🤖 autopilot"
chat thread and [auto] journal lines.
"""
import collections
import threading
import time


class Autopilot:
    def __init__(self, run_pm, verbs, ledger, guard, notify=None,
                 enabled=True, toggle_path=None,
                 cooldown_s=300, verify_cooldown_s=900, own_action_s=120,
                 max_per_hour=10, max_per_day=60, digest_window_s=900,
                 verify_max_per_task=5):
        self.run_pm = run_pm            # callable(kind, prompt) -> reply (serialized upstream)
        self.verbs = verbs
        self.ledger = ledger
        self.guard = guard
        self.notify = notify or (lambda text: None)
        self.toggle_path = toggle_path
        self.enabled = self._load_toggle(enabled)
        self.cooldown_s = cooldown_s
        self.verify_cooldown_s = verify_cooldown_s
        self.own_action_s = own_action_s
        self.max_per_hour = max_per_hour
        self.max_per_day = max_per_day
        self.digest_window_s = digest_window_s
        self.verify_max_per_task = verify_max_per_task

        self._q = collections.deque()
        self._q_keys = set()             # dedupe (kind, cid) while pending
        self._wake = threading.Event()
        self._stop = False
        self._turns = collections.deque()          # timestamps of auto turns
        self._budget_notified = 0.0
        self._last_turn_for = {}                    # cid -> ts (triage cooldown)
        self._last_verify = {}                      # task_id -> ts
        self._verify_count = collections.Counter()  # task_id -> auto turns spent
        self.escalations = []                       # pending digest items
        self._esc_lock = threading.Lock()
        self.recent = collections.deque(maxlen=40)  # (t, kind, target, note) for /api
        self.lock = threading.RLock()

    # -- lifecycle ---------------------------------------------------------------
    def start(self):
        threading.Thread(target=self._worker, daemon=True, name="autopilot").start()
        threading.Thread(target=self._digest_loop, daemon=True,
                         name="autopilot-digest").start()
        return self

    def stop(self):
        self._stop = True
        self._wake.set()

    def _load_toggle(self, default):
        try:
            with open(self.toggle_path, encoding="utf-8") as f:
                return f.read().strip() == "1"
        except (OSError, TypeError):
            return default

    def set_enabled(self, on):
        self.enabled = bool(on)
        if self.toggle_path:
            try:
                with open(self.toggle_path, "w", encoding="utf-8") as f:
                    f.write("1" if on else "0")
            except OSError:
                pass
        self._log(f"{'enabled' if on else 'DISABLED (kill switch)'}")
        return self.enabled

    # -- reactor feed --------------------------------------------------------------
    def feed(self, event):
        """Reactor handler — cheap, never blocks the reactor thread."""
        kind = event.get("kind")
        if kind not in ("blocked", "turn_done"):
            return
        key = (kind, event.get("cid"))
        with self.lock:
            if key in self._q_keys:
                return
            self._q_keys.add(key)
            self._q.append(event)
        self._wake.set()

    # -- guards ---------------------------------------------------------------------
    def _paused_reason(self):
        if not self.enabled:
            return "kill switch off"
        if self.guard.autonomy == "readonly":
            return "autonomy=readonly"
        return None

    def _budget_left(self):
        now = time.time()
        while self._turns and now - self._turns[0] > 86400:
            self._turns.popleft()
        day = len(self._turns)
        hour = sum(1 for t in self._turns if now - t < 3600)
        if hour >= self.max_per_hour or day >= self.max_per_day:
            # tell the operator ONCE per starvation window, not per event
            if now - self._budget_notified > 3600:
                self._budget_notified = now
                self.notify(f"🤖 autopilot budget exhausted "
                            f"({hour}/{self.max_per_hour} this hour, "
                            f"{day}/{self.max_per_day} today) — pausing until it refills")
            return False
        return True

    def _own_echo(self, cid):
        """True if the PM itself wrote to this cid moments ago — the event is
        the echo of our own action, not news."""
        now = time.time()
        for a in self.ledger.recent_actions(50):
            if (a.get("args") or {}).get("cid") == cid and \
                    now - (a.get("t") or 0) < self.own_action_s:
                return True
        return False

    # -- the worker -------------------------------------------------------------------
    def _worker(self):
        while not self._stop:
            self._wake.wait(timeout=5)
            self._wake.clear()
            while not self._stop:
                with self.lock:
                    if not self._q:
                        break
                    ev = self._q.popleft()
                    self._q_keys.discard((ev.get("kind"), ev.get("cid")))
                try:
                    self._handle(ev)
                except Exception as e:
                    self._log(f"handler error: {type(e).__name__}: {e}")

    def _handle(self, ev):
        reason = self._paused_reason()
        if reason:
            return
        kind, cid, machine = ev.get("kind"), ev.get("cid"), ev.get("machine")
        now = time.time()
        if self._own_echo(cid):
            self._note(kind, f"{machine}/{str(cid)[:8]}", "skipped: own echo")
            return
        if kind == "blocked":
            if now - self._last_turn_for.get(cid, 0) < self.cooldown_s:
                return
            if not self._budget_left():
                return
            self._last_turn_for[cid] = now
            self._turns.append(now)
            self._turn("triage", self._triage_prompt(ev),
                       f"{machine}/{str(cid)[:8]}")
        elif kind == "turn_done":
            tid = self.ledger.task_for_cid(cid)
            task = self.ledger.get(tid) if tid else None
            if not task or task.get("status") != "in_progress":
                return
            if now - self._last_verify.get(tid, 0) < self.verify_cooldown_s:
                return
            if self._verify_count[tid] >= self.verify_max_per_task:
                return
            if not self._budget_left():
                return
            self._last_verify[tid] = now
            self._verify_count[tid] += 1
            self._turns.append(now)
            self._turn("verify", self._verify_prompt(ev, task),
                       f"{tid} @ {machine}/{str(cid)[:8]}")

    def _turn(self, kind, prompt, target):
        self._log(f"{kind} turn → {target}")
        try:
            reply = self.run_pm(kind, prompt)
        except Exception as e:
            self._note(kind, target, f"turn failed: {e}")
            return
        self._note(kind, target, (reply or "").strip()[:200])

    # -- prompts ---------------------------------------------------------------------
    @staticmethod
    def _triage_prompt(ev):
        return (f"[autopilot] Session {ev.get('machine')}/{ev.get('cid')} just went "
                f"BLOCKED: {ev.get('summary') or 'needs input'}.\n"
                "Handle THIS ONE item per the sweep protocol: read the evidence "
                "first (transcript_tail; peek_screen if the tail is inconclusive). "
                "If it is trivial, clear it now (answer_prompt / ask) and say what "
                "you did. If it needs the operator, call escalate(machine, cid, "
                "question, urgency) with the concrete question — urgency 'digest' "
                "unless something is actively breaking. Do not touch other "
                "sessions, do not create tasks. Final reply: one short line.")

    @staticmethod
    def _verify_prompt(ev, task):
        acc = (task.get("acceptance") or "").strip()
        return (f"[autopilot] Task {task['id']} — \"{task.get('goal', '')[:200]}\" — "
                f"its session {ev.get('machine')}/{ev.get('cid')} finished a turn.\n"
                f"Acceptance: {acc or '(none recorded — judge against the goal)'}\n"
                "Follow through: read session_digest / transcript_tail and judge the "
                "actual work.\n"
                f"(a) meets acceptance → set_task_status('{task['id']}','review') and "
                "escalate a one-line 'ready for review' at urgency 'digest'.\n"
                "(b) parked on a question → escalate the concrete question.\n"
                "(c) incomplete or drifting → ONE corrective ask() to that session "
                f"and note_task('{task['id']}', what you nudged).\n"
                "Never spawn sessions or create tasks here. Final reply: one short line.")

    # -- escalations / digest -----------------------------------------------------------
    def escalate(self, item):
        """Sink for the `escalate` verb. urgency 'now' pushes immediately;
        everything else waits for the windowed digest."""
        item = dict(item, t=time.time())
        if item.get("urgency") == "now":
            self.notify("🚨 " + self._fmt(item))
            self._note("escalate", item.get("cid") or "-", "pushed now")
            return {"ok": True, "pushed": "now"}
        with self._esc_lock:
            self.escalations.append(item)
            n = len(self.escalations)
        self._note("escalate", item.get("cid") or "-", "queued for digest")
        return {"ok": True, "queued": n,
                "flush_in_s": int(self.digest_window_s)}

    def _digest_loop(self):
        while not self._stop:
            time.sleep(max(30, self.digest_window_s))
            self.flush_digest()

    def flush_digest(self):
        with self._esc_lock:
            items, self.escalations = self.escalations, []
        if not items:
            return 0
        lines = [f"🤖 PM digest — {len(items)} item(s) need you:"]
        for it in items[:10]:
            where = f"{it.get('machine')}/{str(it.get('cid') or '')[:8]}".rstrip("/")
            line = f"• {where}: {it.get('question', '')[:200]}"
            if it.get("url"):
                line += f"\n  {it['url']}"
            lines.append(line)
        if len(items) > 10:
            lines.append(f"…and {len(items) - 10} more (ask me for the rest)")
        self.notify("\n".join(lines))
        self._log(f"digest flushed: {len(items)} item(s)")
        return len(items)

    @staticmethod
    def _fmt(it):
        where = f"{it.get('machine')}/{str(it.get('cid') or '')[:8]}".rstrip("/")
        out = f"{where}: {it.get('question', '')[:300]}"
        if it.get("url"):
            out += f"\n{it['url']}"
        return out

    # -- observability --------------------------------------------------------------
    def status(self):
        now = time.time()
        with self.lock:
            hour = sum(1 for t in self._turns if now - t < 3600)
            day = sum(1 for t in self._turns if now - t < 86400)
            pending = len(self._q)
        with self._esc_lock:
            esc = len(self.escalations)
        return {"enabled": self.enabled,
                "paused": self._paused_reason(),
                "turns_last_hour": hour, "max_per_hour": self.max_per_hour,
                "turns_last_day": day, "max_per_day": self.max_per_day,
                "queue": pending, "digest_pending": esc,
                "digest_window_s": self.digest_window_s,
                "recent": [{"t": t, "kind": k, "target": tg, "note": n}
                           for t, k, tg, n in list(self.recent)]}

    def _note(self, kind, target, note):
        self.recent.append((time.time(), kind, target, note))
        self._log(f"{kind} {target}: {note}")

    @staticmethod
    def _log(msg):
        print("[auto] " + msg, flush=True)
