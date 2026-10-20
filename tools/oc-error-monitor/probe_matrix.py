#!/usr/bin/env python3
"""Measure OpenCode free-model success rate per mihomo node.

Why this exists
---------------
The `AUTH_IP_REPUTATION` error ("we've detected abusive traffic from this IP
address") turned out to be *intermittent* on a fixed node: the same node and
the same model returned 403 on some attempts and 200 on others. That means the
mitigation policy for this class cannot be "never retry" -- it has to be
"retry, and only switch node when retries keep failing".

This tool produces the evidence for that policy:

  for each candidate node:
      switch the OpenCode selector to it
      run the OpenCode CLI N times with a free model
      record success/failure, latency, and the upstream host/IP that mihomo
      actually connected to (from /connections)
      restore the original node

It never edits Clash configuration and it always restores the original node.

Usage:
  python probe_matrix.py                        # 3 nodes x 4 attempts
  python probe_matrix.py --nodes 5 --attempts 6
  python probe_matrix.py --model opencode/minimax/minimax-m3.1-flash-free
  python probe_matrix.py --list-nodes           # show candidates and exit
  python probe_matrix.py --dry-run             # measure without switching nodes
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

CFG = Path.home() / "AppData/Roaming/io.github.clash-verge-rev.clash-verge-rev/config.yaml"
BASE = "http://127.0.0.1:9097"
OPENCODE = Path.home() / "AppData/Local/Programs/@openchamberelectron/resources/opencode-cli/opencode.exe"


def get_secret() -> str:
    text = CFG.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"^\s*secret:\s*(.+?)\s*$", text, re.M)
    if not m:
        raise SystemExit("mihomo controller secret not found")
    return m.group(1).strip().strip("'\"")


def api(secret: str, method: str, path: str, body=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        raw = r.read()
        return json.loads(raw.decode("utf-8")) if raw else None


class ConnectionWatcher(threading.Thread):
    """Poll /connections and collect the upstreams used while a request runs."""

    def __init__(self, secret: str, interval: float = 0.4) -> None:
        super().__init__(daemon=True)
        self.secret = secret
        self.interval = interval
        self._halt = threading.Event()
        self.seen: dict[str, dict] = {}

    def run(self) -> None:
        while not self._halt.is_set():
            try:
                data = api(self.secret, "GET", "/connections") or {}
            except Exception:
                data = {}
            for c in data.get("connections") or []:
                meta = c.get("metadata") or {}
                host = meta.get("host") or ""
                dst = meta.get("destinationIP") or ""
                if not host and not dst:
                    continue
                if "opencode" not in host.lower() and "opencode" not in dst:
                    continue
                key = f"{host}|{dst}"
                chains = c.get("chains") or []
                self.seen[key] = {
                    "host": host,
                    "dst": dst,
                    "rule": c.get("rule"),
                    "rule_payload": c.get("rulePayload"),
                    "chains": chains,
                }
            self._halt.wait(self.interval)

    def stop(self) -> dict[str, dict]:
        self._halt.set()
        self.join(timeout=2)
        return dict(self.seen)


def run_cli(model: str, prompt: str, timeout: int) -> dict:
    env = os.environ.copy()
    env.setdefault("NO_PROXY", "")
    cmd = [str(OPENCODE), "run", "--standalone", "--model", model, prompt]
    started = time.time()
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=str(Path.home()),
        )
        elapsed = time.time() - started
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode == 0:
            return {"ok": True, "elapsed": round(elapsed, 2), "detail": "ok"}
        if "abusive traffic" in out:
            reason = "ip_reputation"
        elif "only be used from within OpenCode" in out:
            reason = "free_tier_client"
        elif "Model unavailable" in out:
            reason = "model_unavailable"
        else:
            reason = out.strip().splitlines()[-1][:200] if out.strip() else "unknown"
        return {"ok": False, "elapsed": round(elapsed, 2), "detail": reason}
    except subprocess.TimeoutExpired:
        return {"ok": False, "elapsed": round(time.time() - started, 2), "detail": "timeout"}


SKIP_NAME_RE = re.compile(
    r"(?i)(剩余流量|套餐|到期|过期|官网|续费|邀请|DIRECT|REJECT|PASS|COMPATIBLE|故障|失效)"
)
SKIP_GROUP_TYPES = {
    "Selector",
    "URLTest",
    "Fallback",
    "LoadBalance",
    "Direct",
    "Reject",
    "Compatible",
    "Pass",
}


def publish_model_scores(path: Path, model_results: list[dict]) -> None:
    """Merge measured per-model success rates into the score file.

    Kept under "model_results" so the failover tool can pick the most
    reliable model instead of a hard-coded chain.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}

    merged: dict[str, dict] = data.setdefault("model_results", {})
    for row in model_results:
        model = row.get("model")
        rate = row.get("rate")
        attempts = len(row.get("attempts") or [])
        if not isinstance(model, str) or not isinstance(rate, (int, float)):
            continue
        prev = merged.get(model) or {"rate": 0.0, "samples": 0}
        total = prev["samples"] + attempts
        avg = (prev["rate"] * prev["samples"] + rate * attempts) / total if total else rate
        merged[model] = {"rate": round(avg, 4), "samples": total}

    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def publish_scores(path: Path, model: str, results: list[dict]) -> None:
    """Merge measured success rates into the controller's score file.

    Rates are averaged per (node, model) so repeated runs accumulate rather
    than overwrite each other.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}

    models: dict[str, dict] = data.setdefault("models", {})
    entry = models.setdefault(model, {})
    for row in results:
        node = row.get("node")
        rate = row.get("rate")
        attempts = len(row.get("attempts") or [])
        if not isinstance(node, str) or not isinstance(rate, (int, float)):
            continue
        prev = entry.get(node) or {"rate": 0.0, "samples": 0}
        total = prev["samples"] + attempts
        merged = (prev["rate"] * prev["samples"] + rate * attempts) / total if total else rate
        entry[node] = {"rate": round(merged, 4), "samples": total}

    # The controller reads a flat {node: rate} map under "results".
    flat: dict[str, float] = {}
    for per_model in models.values():
        for node, info in per_model.items():
            flat[node] = max(flat.get(node, 0.0), float(info.get("rate", 0.0)))
    data["results"] = [
        {"node": node, "rate": rate} for node, rate in sorted(flat.items())
    ]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def node_delay(secret: str, node: str, url: str, timeout_ms: int) -> int | None:
    """Latency to an arbitrary URL through a specific node (mihomo delay API)."""
    quoted = urllib.parse.quote(node, safe="")
    q = urllib.parse.urlencode({"url": url, "timeout": timeout_ms})
    try:
        data = api(secret, "GET", f"/proxies/{quoted}/delay?{q}")
    except Exception:
        return None
    if isinstance(data, dict):
        delay = data.get("delay")
        if isinstance(delay, int):
            return delay
    return None


def leaf_candidates(
    group: dict,
    proxies: dict,
    limit: int,
    name_filter: re.Pattern[str] | None = None,
) -> list[str]:
    """Leaf nodes only, using the same filters the controller uses to switch."""
    out = []
    for name in group.get("all") or []:
        if SKIP_NAME_RE.search(name):
            continue
        if name_filter is not None and not name_filter.search(name):
            continue
        obj = proxies.get(name)
        if not isinstance(obj, dict):
            continue
        if obj.get("type") in SKIP_GROUP_TYPES:
            continue
        if obj.get("alive") is False:
            continue
        out.append(name)
    return out[:limit]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Per-node OpenCode free-model success probe.")
    p.add_argument("--group", default="OpenCode", help="Selector group name hint")
    p.add_argument("--model", default="opencode/deepseek-v4.1-flash-free")
    p.add_argument("--prompt", default="Reply with exactly: OK")
    p.add_argument("--nodes", type=int, default=3, help="How many candidate nodes to test")
    p.add_argument(
        "--models",
        default=None,
        help="Comma-separated models to compare on one fixed node (overrides --model)",
    )
    p.add_argument(
        "--filter",
        default=None,
        help="Regex to select node names (e.g. '联通|电信|移动|多线')",
    )
    p.add_argument("--attempts", type=int, default=4, help="CLI attempts per node")
    p.add_argument("--timeout", type=int, default=120, help="Per-attempt CLI timeout in seconds")
    p.add_argument("--settle", type=float, default=1.5, help="Pause after switching a node")
    p.add_argument(
        "--health-url",
        default="https://www.gstatic.com/generate_204",
        help="URL used for the per-node latency check (same default as the controller)",
    )
    p.add_argument("--health-timeout", type=int, default=5000, help="Latency probe timeout in ms")
    p.add_argument("--out", type=Path, default=None, help="Write the JSON report here")
    p.add_argument("--list-nodes", action="store_true", help="List candidates and exit")
    p.add_argument("--dry-run", action="store_true", help="Measure without switching nodes")
    p.add_argument(
        "--scores",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "logs" / "resilience" / "node-scores.json",
        help="Where the controller reads measured per-node success rates",
    )
    p.add_argument("--no-scores", action="store_true", help="Do not publish scores")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    secret = get_secret()

    proxies = api(secret, "GET", "/proxies")["proxies"]
    group_name = next((n for n in proxies if args.group in n), None)
    if not group_name:
        print(f"group hint {args.group!r} not found", file=sys.stderr)
        return 2
    group = proxies[group_name]
    original = group.get("now")
    quoted = urllib.parse.quote(group_name, safe="")

    name_filter = re.compile(args.filter) if args.filter else None
    candidates = leaf_candidates(group, proxies, 100, name_filter)
    print(f"group:   {group_name}")
    print(f"current: {original}")
    print(f"model:   {args.model}")
    print(f"leaves:  {len(candidates)}")
    if name_filter:
        print(f"filter:  {args.filter}")

    if args.models:
        # Model comparison: one fixed node, every model gets the same attempts.
        models = [m.strip() for m in args.models.split(",") if m.strip()]
        print(f"comparing {len(models)} model(s) on node {original}")
        print()
        model_results = []
        try:
            for model in models:
                ok = 0
                rows = []
                for i in range(args.attempts):
                    res = run_cli(model, args.prompt, args.timeout)
                    res["attempt"] = i + 1
                    res["model"] = model
                    rows.append(res)
                    if res["ok"]:
                        ok += 1
                    tag = "OK  " if res["ok"] else "FAIL"
                    print(f"  [{tag}] {model} try {i + 1} {res['elapsed']:>6}s {res['detail']}")
                rate = ok / len(rows) if rows else 0.0
                print(f"  => {model}: {ok}/{len(rows)} ok ({rate:.0%})")
                print()
                model_results.append({"model": model, "attempts": rows, "ok": ok, "rate": rate})
        finally:
            if original:
                print(f"node unchanged: {original}")
        print("MODEL COMPARISON")
        for r in model_results:
            print(f"  {r['rate']:>4.0%}  {r['model']}")
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(
                json.dumps(
                    {"node": original, "models": model_results}, ensure_ascii=False, indent=2
                ),
                encoding="utf-8",
            )
            print(f"report: {args.out}")
        if not args.no_scores:
            publish_model_scores(args.scores, model_results)
            print(f"model scores: {args.scores}")
        return 0

    if args.list_nodes:
        for name in candidates[: args.nodes]:
            print("  -", name)
        return 0

    if args.dry_run:
        nodes = [original]
    else:
        nodes = [n for n in candidates if n != original][: args.nodes]
        if original:
            nodes = [original] + nodes

    print(f"testing {len(nodes)} node(s) x {args.attempts} attempt(s)")
    print()

    results = []
    try:
        for node in nodes:
            if not args.dry_run:
                api(secret, "PUT", f"/proxies/{quoted}", {"name": node})
                time.sleep(args.settle)
            print(f"--- node: {node}")
            node_results = []
            node_delay_ms = node_delay(secret, node, args.health_url, args.health_timeout)
            print(f"  latency({args.health_url}): {node_delay_ms} ms")
            for i in range(args.attempts):
                watcher = ConnectionWatcher(secret)
                watcher.start()
                res = run_cli(args.model, args.prompt, args.timeout)
                upstreams = list(watcher.stop().values())
                res["attempt"] = i + 1
                res["node"] = node
                res["node_delay_ms"] = node_delay_ms
                res["upstreams"] = upstreams
                node_results.append(res)
                tag = "OK  " if res["ok"] else "FAIL"
                print(
                    f"  [{tag}] try {i + 1} {res['elapsed']:>6}s "
                    f"{res['detail']}"
                    + (f" via {upstreams[0]['dst']}" if upstreams else "")
                )
            ok = sum(1 for r in node_results if r["ok"])
            rate = ok / len(node_results) if node_results else 0.0
            print(f"  => {node}: {ok}/{len(node_results)} ok ({rate:.0%})")
            results.append({"node": node, "attempts": node_results, "ok": ok, "rate": rate})
            print()
    finally:
        if not args.dry_run and original:
            try:
                api(secret, "PUT", f"/proxies/{quoted}", {"name": original})
                print(f"restored: {original}")
            except Exception as exc:
                print(f"restore failed: {exc}", file=sys.stderr)

    total = sum(r["ok"] for r in results)
    attempts = sum(len(r["attempts"]) for r in results)
    print(f"OVERALL {total}/{attempts} ok")
    for r in results:
        print(f"  {r['rate']:.0%}  {r['node']}")

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(
                {
                    "model": args.model,
                    "group": group_name,
                    "original": original,
                    "results": results,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"report: {args.out}")

    # Publish the measured rates where the controller reads them.
    if not args.no_scores:
        publish_scores(args.scores, args.model, results)
        print(f"scores: {args.scores}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
