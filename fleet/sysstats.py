"""Per-machine system stats for the fleet roster — CPU / RAM / disk.

Pure stdlib, cross-platform (the fleet spans macOS laptops + a Linux box), and
defensive: every probe is best-effort and returns None on failure rather than
raising, so a roster push never breaks because one metric is unavailable.

Shape (all percentages 0–100, byte counts raw):
  {"cpu":  {"pct", "load1", "cores"},
   "ram":  {"pct", "used", "total"},
   "disk": {"pct", "used", "free", "total"}}
"""
import os
import re
import shutil
import subprocess


def cpu():
    """Load-average-based CPU pressure. `pct` = 1-min load ÷ cores, capped at 100
    — a stdlib, cross-platform proxy (true %-busy needs platform-specific
    sampling). load1 + cores are included raw for an honest read."""
    try:
        load1 = os.getloadavg()[0]
        cores = os.cpu_count() or 1
        return {"pct": round(min(load1 / cores * 100, 100), 1),
                "load1": round(load1, 2), "cores": cores}
    except (OSError, AttributeError):
        return None


def disk(path="/"):
    try:
        d = shutil.disk_usage(path)
        return {"pct": round(d.used / d.total * 100, 1) if d.total else 0,
                "used": d.used, "free": d.free, "total": d.total}
    except OSError:
        return None


def ram():
    return _ram_linux() or _ram_macos()


def _ram_linux():
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                parts = v.split()
                if parts:
                    info[k.strip()] = int(parts[0]) * 1024     # kB → bytes
    except OSError:
        return None
    total = info.get("MemTotal")
    if not total:
        return None
    avail = info.get("MemAvailable", info.get("MemFree", 0))
    used = total - avail
    return {"pct": round(used / total * 100, 1), "used": used, "total": total}


def _ram_macos():
    try:
        total = int(subprocess.run(["sysctl", "-n", "hw.memsize"],
                                   capture_output=True, text=True, timeout=3).stdout.strip())
        vm = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if not total:
        return None
    page = 4096
    m = re.search(r"page size of (\d+)", vm)
    if m:
        page = int(m.group(1))

    def pages(name):
        mm = re.search(rf"{re.escape(name)}:\s+(\d+)", vm)
        return int(mm.group(1)) if mm else 0

    # free + inactive + speculative ≈ reclaimable; the rest is "used"
    free = (pages("Pages free") + pages("Pages inactive")
            + pages("Pages speculative")) * page
    used = max(total - free, 0)
    return {"pct": round(used / total * 100, 1), "used": used, "total": total}


def collect():
    """Best-effort {cpu, ram, disk}; missing probes are omitted (not None-valued)."""
    out = {}
    for key, fn in (("cpu", cpu), ("ram", ram), ("disk", disk)):
        v = fn()
        if v:
            out[key] = v
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(collect(), indent=2))
