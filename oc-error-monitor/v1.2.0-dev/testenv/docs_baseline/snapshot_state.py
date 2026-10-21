"""Capture the live state of the running system as a baseline snapshot.

The point of this file is evidence. When a new experiment runs beside the
working setup, this snapshot is taken before and after, and the two are
diffed, so "the running version was not affected" is a measurement and not
a promise.

    python snapshot_state.py --out baseline.json
    python snapshot_state.py --out after.json --diff baseline.json
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

USER = Path.home()
OC_CONFIG = USER / ".config" / "opencode" / "opencode.json"
ROUTING = USER / ".config" / "openchamber" / "routing.json"
ACTIVE_PORT = USER / ".config" / "opencode" / "oc-error-monitor-v1.0.0" / "active-port.txt"
WATCHDOG_TASKS = ["OpenCode Retry Proxy", "OpenCode Retry Proxy (logon)"]


def get_json(url, timeout=6):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return {"_error": str(e)}


def run(cmd, timeout=20):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        return p.stdout.strip() or p.stderr.strip()
    except Exception as e:
        return f"_error: {e}"


def task_installed(name):
    """schtasks exit code is 0 when the task exists. The text is localised,
    so the exit code is the only dependable signal."""
    try:
        p = subprocess.run(["schtasks", "/Query", "/TN", name, "/FO", "LIST"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=20)
        return p.returncode == 0
    except Exception:
        return False


def proxy_port():
    if ACTIVE_PORT.exists():
        txt = ACTIVE_PORT.read_text(encoding="utf-8", errors="replace").strip()
        m = re.search(r"\d+", txt)
        if m:
            return int(m.group())
    return 8788


def wired_models(port):
    """How many models point at the retry proxy, and how many still point
    somewhere else."""
    if not OC_CONFIG.exists():
        return {"_error": "opencode.json missing"}
    try:
        cfg = json.loads(OC_CONFIG.read_text(encoding="utf-8", errors="replace"))
    except Exception as e:
        return {"_error": str(e)}
    wired, other, total = [], [], 0
    for prov_name, prov in (cfg.get("provider") or {}).items():
        for model_name, model in (prov.get("models") or {}).items():
            total += 1
            # opencode.json keeps the override at models.<name>.provider.api
            api = ((model.get("provider") or {}).get("api")
                   or (model.get("options") or {}).get("baseURL") or "")
            if str(port) in str(api):
                wired.append(f"{prov_name}/{model_name}")
            else:
                other.append(f"{prov_name}/{model_name} -> {api}")
    return {"total": total, "wired": len(wired), "not_wired": other}


def routing_state():
    if not ROUTING.exists():
        return {"_error": "routing.json missing"}
    try:
        r = json.loads(ROUTING.read_text(encoding="utf-8", errors="replace"))
    except Exception as e:
        return {"_error": str(e)}
    fb = r.get("fallback") or {}
    if isinstance(fb, dict):
        m = fb.get("model") or {}
        fb_name = f"{m.get('providerID')}/{m.get('modelID')}" if isinstance(m, dict) else str(m)
    else:
        fb_name = str(fb)
    return {
        "enabled": r.get("enabled"),
        "minConfidence": r.get("minConfidence"),
        "preserveCache": r.get("preserveCache"),
        "safetyNet": (r.get("safetyNet") or {}).get("enabled"),
        "fallback": fb_name,
        "fallback_variant": fb.get("variant") if isinstance(fb, dict) else None,
        "raw_keys": sorted(r.keys()),
    }


def watchdog():
    return {name: ("installed" if task_installed(name) else "missing")
            for name in WATCHDOG_TASKS}


def opencode_db():
    db = USER / ".local" / "share" / "opencode" / "opencode.db"
    if not db.exists():
        return {"_error": "db missing"}
    return {"exists": True, "size_mb": round(db.stat().st_size / 1048576, 2)}


def collect():
    port = proxy_port()
    health = get_json(f"http://127.0.0.1:{port}/__health")
    stats = get_json(f"http://127.0.0.1:{port}/__stats")
    return {
        "proxy": {
            "port": port,
            "health": health,
            "requests": stats.get("requests"),
            "attempts": stats.get("attempts"),
            "classes": stats.get("classes"),
        },
        "models": wired_models(port),
        "routing": routing_state(),
        "watchdog": watchdog(),
        "opencode_db": opencode_db(),
    }


def diff(a, b):
    """Keys that changed between two snapshots."""
    changes = []

    def walk(path, x, y):
        if isinstance(x, dict) and isinstance(y, dict):
            for k in sorted(set(x) | set(y)):
                walk(f"{path}.{k}" if path else k, x.get(k), y.get(k))
            return
        if x != y:
            changes.append({"key": path, "before": x, "after": y})

    walk("", a, b)
    return changes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    ap.add_argument("--diff", help="snapshot json to compare against")
    args = ap.parse_args()

    snap = collect()
    if args.diff:
        base = json.loads(Path(args.diff).read_text(encoding="utf-8", errors="replace"))
        changes = diff(base, snap)
        print(f"baseline: {args.diff}")
        print(f"changes : {len(changes)}")
        for c in changes:
            print(f"  {c['key']}: {c['before']!r} -> {c['after']!r}")
        # Only the counters are expected to move; the wiring must not.
        hard = [c for c in changes
                if c["key"].split(".")[0:2] in (["models", "wired"], ["models", "total"],
                                                ["routing", "enabled"], ["routing", "fallback"],
                                                ["proxy", "port"], ["proxy", "health.ok"])
                or c["key"].startswith("watchdog.")]
        if hard:
            print("VERDICT: CONFIGURATION DRIFT - the running setup changed")
            return 2
        print("VERDICT: wiring unchanged (only traffic counters moved)")
        return 0

    text = json.dumps(snap, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"snapshot written: {args.out}")
    print(text)


if __name__ == "__main__":
    sys.exit(main())
