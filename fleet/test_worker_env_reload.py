#!/usr/bin/env python3
"""Guard: a worker self-restart must re-read fleet.env, not inherit its old values.

worker.py layers fleet.env into os.environ with setdefault at import, and
update_watch_loop restarts by exec'ing itself. exec with os.environ handed the
child every fleet.env value the FIRST boot loaded, and setdefault then refused to
overwrite it — so an edited fleet.env never took effect until a real
launchd/systemd restart. That is how the 2026-09-05 passkey cadence change
(FLEET_E2E_MAX_TTL 86400 → 604800) stayed a no-op on clawd-heart for four days
while the worker "restarted to pick up new code" twice.

Checks:
  1. importing worker with a fleet.env beside it loads the value into os.environ
     but _BOOT_ENV (the exec env) does NOT contain it;
  2. the restart exec passes _BOOT_ENV, and it is snapshotted before the load;
  3. the self-heal for boxes ALREADY carrying a stale bake-in when this shipped
     (clawd-head, clawd-leftclaw on 09-09): an inherited value that disagrees
     with fleet.env re-execs once with it stripped, so the file wins; an equal
     value (EnvironmentFile=) or FLEET_SELF_RESTART=0 does not.

Run: python3 fleet/test_worker_env_reload.py
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
FAILED = []


def check(name, ok):
    print(f"  {'OK ' if ok else 'BAD'} {name}")
    if not ok:
        FAILED.append(name)


def main():
    src = (HERE / "worker.py").read_text()
    snap = src.find("_BOOT_ENV = {k: v for k, v in os.environ.items()")
    load = src.find("\n_load_env_file()\n")
    check("_BOOT_ENV snapshotted before fleet.env is loaded", 0 < snap < load)
    check("restart exec hands the child _BOOT_ENV, not os.environ",
          re.search(r"os\.execve\(sys\.executable, \[sys\.executable\] \+ sys\.argv, _BOOT_ENV\)", src) is not None
          and "os.execv(sys.executable" not in src)

    # Behavioral: a copy of worker.py next to a fleet.env, imported in a clean env.
    tmp = Path(tempfile.mkdtemp(prefix="clawd-worker-env-"))
    try:
        shutil.copy(HERE / "worker.py", tmp / "worker.py")
        (tmp / "fleet.env").write_text("FLEET_E2E_MAX_TTL=12345\nFLEET_TEST_MARK=from-file\n")
        code = (
            "import os, sys, json\n"
            f"sys.path.append({str(HERE)!r})\n"   # siblings (e2e/fleet_ws/webauthn); cwd stays first so the COPY is imported
            "import worker\n"
            "print(json.dumps({'env': os.environ.get('FLEET_TEST_MARK'),"
            " 'boot': worker._BOOT_ENV.get('FLEET_TEST_MARK'),"
            " 'ttl_env': os.environ.get('FLEET_E2E_MAX_TTL'),"
            " 'ttl_boot': worker._BOOT_ENV.get('FLEET_E2E_MAX_TTL')}))\n"
        )
        env = {k: v for k, v in os.environ.items() if not k.startswith("FLEET_")}
        py = HERE / ".venv" / "bin" / "python3"
        r = subprocess.run([str(py if py.exists() else sys.executable), "-c", code], cwd=tmp,
                           env=env, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            print(r.stderr[-2000:])
        check("worker import exits 0", r.returncode == 0)
        import json
        out = json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else {}
        check("fleet.env value IS loaded into os.environ", out.get("env") == "from-file" and out.get("ttl_env") == "12345")
        check("fleet.env value is NOT in the exec env (_BOOT_ENV)", out.get("boot") is None and out.get("ttl_boot") is None)

        # 3. The self-heal: an inherited env that DISAGREES with fleet.env for a key
        #    the file defines (= a pre-09-09 exec bake-in) triggers ONE clean re-exec
        #    and the file wins; an inherited env that AGREES is left alone.
        runner = tmp / "runner.py"
        runner.write_text(
            "import os, sys, json\n"
            f"sys.path.append({str(HERE)!r})\n"
            "import worker\n"
            "print(json.dumps({'ttl_env': os.environ.get('FLEET_E2E_MAX_TTL'),"
            " 'mark': os.environ.get('FLEET_CLEAN_REEXEC'),"
            " 'boot_mark': worker._BOOT_ENV.get('FLEET_CLEAN_REEXEC')}))\n")
        def run(extra):
            e = dict(env); e.update(extra)
            r = subprocess.run([str(py if py.exists() else sys.executable), str(runner)], cwd=tmp,
                               env=e, capture_output=True, text=True, timeout=90)
            if r.returncode != 0:
                print(r.stderr[-2000:])
            return json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else {}
        out = run({"FLEET_E2E_MAX_TTL": "86400"})   # stale bake-in: file says 12345
        check("stale inherited value → re-exec'd once (marker set)", out.get("mark") == "1")
        check("…and fleet.env wins after the re-exec", out.get("ttl_env") == "12345")
        check("…marker not carried into the next exec env", out.get("boot_mark") is None)
        out = run({"FLEET_E2E_MAX_TTL": "12345"})   # EnvironmentFile= the same file: equal
        check("inherited value equal to fleet.env → no re-exec", out.get("mark") is None and out.get("ttl_env") == "12345")
        out = run({"FLEET_E2E_MAX_TTL": "86400", "FLEET_SELF_RESTART": "0"})
        check("FLEET_SELF_RESTART=0 opts out of the re-exec (env keeps winning)",
              out.get("mark") is None and out.get("ttl_env") == "86400")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if FAILED:
        print(f"FAILED: {FAILED}")
        return 1
    print("PASSED: worker self-restart re-reads fleet.env")
    return 0


if __name__ == "__main__":
    sys.exit(main())
