"""Point the free models at the local retry proxy.

The proxy can move to another port when 8787 is taken by something else
(playwright-mcp's internal server grabbed it once), so this rewrites the
provider.api of every free model to the port the proxy is actually on.

    python wire_retry_proxy.py                 # use the port in active-port.txt
    python wire_retry_proxy.py --port 8788     # use an explicit port
    python wire_retry_proxy.py --check         # report only, change nothing
"""
import json
import os
import shutil
import sys
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CFG = os.path.expanduser(r"~\.config\opencode\opencode.json")
CATALOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models_cache.json")
RUNDIR = os.path.expanduser(r"~\.config\opencode\oc-error-monitor-v1.0.0")
ACTIVE_PORT_FILE = os.path.join(RUNDIR, "active-port.txt")

DEFAULT_PORT = 8787
FALLBACK_PORTS = [8787, 8788, 8790, 8791]


def free_model_ids():
    """The free models of the opencode provider, from the live catalogue."""
    ids = []
    try:
        with open(CATALOG, encoding="utf-8") as f:
            cat = json.load(f)
        ids = [m for m in cat["opencode"]["models"] if m.endswith("-free") or m == "big-pickle"]
    except OSError:
        pass
    if not ids:
        # Fall back to whatever the config already knows.
        try:
            with open(CFG, encoding="utf-8") as f:
                cfg = json.load(f)
            ids = list(cfg.get("provider", {}).get("opencode", {}).get("models", {}))
        except OSError:
            ids = []
    return sorted(ids)


def active_port():
    try:
        with open(ACTIVE_PORT_FILE, encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return DEFAULT_PORT


def main():
    args = sys.argv[1:]
    check_only = "--check" in args
    port = DEFAULT_PORT
    if "--port" in args:
        port = int(args[args.index("--port") + 1])
    else:
        port = active_port()

    local = f"http://127.0.0.1:{port}/v1"
    with open(CFG, encoding="utf-8") as f:
        cfg = json.load(f)

    prov = cfg.setdefault("provider", {})
    oc = prov.setdefault("opencode", {})
    models = oc.setdefault("models", {})

    wanted = free_model_ids() or list(models)
    changed = []
    for mid in wanted:
        entry = models.setdefault(mid, {})
        p = entry.setdefault("provider", {})
        if p.get("api") != local:
            p["api"] = local
            changed.append(mid)
    # Drop stale entries that point at another port.
    for mid, entry in list(models.items()):
        p = (entry or {}).get("provider") or {}
        api = p.get("api")
        if api and api.startswith("http://127.0.0.1:") and api != local:
            p["api"] = local
            changed.append(mid)

    if check_only:
        print(f"port={port} local={local} would change {len(changed)} model(s)")
        for mid in changed[:8]:
            print("  ", mid)
        return 0

    if not changed:
        print(f"port={port} already wired, nothing to do")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(CFG, CFG + f".bak-retryproxy-{stamp}")
    with open(CFG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"rewired {len(changed)} model(s) -> {local}  (backup {CFG}.bak-retryproxy-{stamp})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
