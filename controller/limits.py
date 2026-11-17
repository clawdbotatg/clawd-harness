"""limits.py — read the CLI's usage-limit wall out of a failed PM turn.

A walled `claude -p` turn never runs: it exits 1 with the wall on stderr
("You've hit your Fable limit · resets 9pm (UTC). Switch to another model…"),
or says "Fable requires usage credits". The PM model can't react to that, so
agent.py detects it here and re-runs the turn on the fallback model.

Pure functions, no I/O — see test_model_fallback.py.
"""
import datetime
import re

try:
    from zoneinfo import ZoneInfo
except ImportError:                      # pragma: no cover — py<3.9
    ZoneInfo = None

_WALL_RE = re.compile(
    r"you.?ve (?:hit|reached) your (?P<what>[a-z0-9 .'’-]{0,40}?)\s*limit"
    r"|(?P<model>[a-z0-9.-]{2,24}) requires usage credits"
    r"|requires usage credits", re.I)
_RESET_RE = re.compile(
    r"resets\s+(?:at\s+)?"
    r"(?:(?P<mon>[a-z]{3})[a-z]*\.?\s+(?P<day>\d{1,2}),?\s+(?:at\s+)?)?"
    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)?"
    r"(?:\s*\((?P<tz>[A-Za-z_/+-]+)\))?", re.I)
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep",
           "oct", "nov", "dec"]
# Words in "your <X> limit" that name a window, not a model.
_NOT_MODELS = {"session", "weekly", "usage", "monthly", "daily", "spend", "rate"}
# A wall that comes back as the turn's TEXT (exit 0) is short and starts with
# the wall. A PM answer that merely quotes one is longer or starts elsewhere.
WALL_REPLY_MAX = 400
# Seconds past the stated reset before the primary is tried again.
RESET_GRACE_S = 120


def parse_wall(text, now=None):
    """Wall text → {"model": "Fable"|"", "until": epoch|None, "label": "21:00 UTC"|""},
    or None when the text is not a limit wall."""
    text = text or ""
    m = _WALL_RE.search(text)
    if not m:
        return None
    model = (m.group("model") or "").strip()
    if not model:
        words = (m.group("what") or "").replace("’", "'").split()
        if words and words[-1].lower() not in _NOT_MODELS and "'" not in words[-1]:
            model = words[-1]
    until, label = _parse_reset(text, now)
    return {"model": model.capitalize() if model else "", "until": until, "label": label}


def is_wall_reply(text):
    """True when a turn's whole reply IS the wall (not an answer quoting one)."""
    text = (text or "").strip()
    if not text or len(text) > WALL_REPLY_MAX:
        return False
    m = _WALL_RE.search(text)
    return bool(m and m.start() < 20)


def _parse_reset(text, now=None):
    m = _RESET_RE.search(text or "")
    if not m:
        return None, ""
    try:
        hour, minute = int(m.group("h")), int(m.group("m") or 0)
        ap = (m.group("ap") or "").lower()
        if ap:
            if not 1 <= hour <= 12:
                return None, ""
            hour = hour % 12 + (12 if ap == "pm" else 0)
        if hour > 23 or minute > 59:
            return None, ""
        tzname = m.group("tz") or ""
        tz = None
        if tzname.upper() == "UTC":
            tz = datetime.timezone.utc
        elif tzname and ZoneInfo is not None:
            try:
                tz = ZoneInfo(tzname)
            except Exception:
                tz = None
        cur = datetime.datetime.fromtimestamp(
            now if now is not None else datetime.datetime.now().timestamp(), tz
        ).astimezone(tz)            # tz None → the box's local time
        when = cur.replace(hour=hour, minute=minute, second=0, microsecond=0)
        mon = (m.group("mon") or "").lower()
        if mon in _MONTHS:
            when = when.replace(month=_MONTHS.index(mon) + 1, day=int(m.group("day")))
            if when <= cur:
                when = when.replace(year=when.year + 1)
        elif when <= cur:
            when += datetime.timedelta(days=1)
        label = when.strftime("%H:%M ") + (tzname or when.strftime("%Z") or "local")
        if mon in _MONTHS:
            label = when.strftime("%b ") + str(when.day) + " " + label
        return when.timestamp() + RESET_GRACE_S, label.strip()
    except (ValueError, OverflowError):
        return None, ""


def model_label(model_id):
    """claude-opus-5-5 → 'Opus 5.5'; unknown shapes come back unchanged."""
    s = re.sub(r"-\d{8}$", "", (model_id or "").strip())
    s = re.sub(r"^claude-", "", s)
    parts = s.split("-")
    name = [p for p in parts if not p.isdigit()]
    ver = [p for p in parts if p.isdigit()]
    if not name:
        return model_id or ""
    out = " ".join(p.capitalize() for p in name)
    return f"{out} {'.'.join(ver)}" if ver else out
