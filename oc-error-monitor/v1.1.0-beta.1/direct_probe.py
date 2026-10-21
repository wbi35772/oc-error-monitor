#!/usr/bin/env python3
"""Temporarily switch the OpenCode selector to DIRECT, run the OpenCode CLI,
then restore the original node.

This is a diagnostic, not part of the P1/P2 runtime. It changes the live
selector for the duration of one CLI invocation. Use it only when you accept
a brief network interruption.

Examples:
  python direct_probe.py
  python direct_probe.py --ca-bundle "$env:TEMP\\win-ca-bundle.pem" --timeout 90
  python direct_probe.py --model opencode/deepseek-v4.1-flash-free
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
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
        raise SystemExit("controller secret not found")
    return m.group(1).strip().strip("'\"")


def api(secret: str, method: str, path: str, body=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        raw = r.read()
        if not raw:
            return None
        return json.loads(raw.decode("utf-8"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="One-shot DIRECT TLS/free-model diagnostic.")
    parser.add_argument("--group", default="OpenCode", help="Selector group name or substring")
    parser.add_argument("--target", default="DIRECT", help="Target node (default DIRECT)")
    parser.add_argument("--model", default="opencode/deepseek-v4.1-flash-free")
    parser.add_argument("--prompt", default="Reply with exactly: OK")
    parser.add_argument("--timeout", type=int, default=150)
    parser.add_argument("--repeat", type=int, default=1, help="Run the CLI N times")
    parser.add_argument(
        "--ca-bundle",
        type=Path,
        default=None,
        help="Set NODE_EXTRA_CA_CERTS to this PEM bundle for the CLI",
    )
    parser.add_argument(
        "--keep-env",
        action="store_true",
        help="Do not strip HTTP(S)_PROXY/ALL_PROXY from the child environment",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    secret = get_secret()

    proxies = api(secret, "GET", "/proxies")["proxies"]
    group = next((n for n in proxies if args.group in n), None)
    if not group:
        print("OpenCode selector group not found")
        return 2

    p = proxies[group]
    old = p.get("now")
    names = p.get("all", [])
    print("group:", repr(group))
    print("old:", repr(old))
    print("members:", len(names), f"{args.target} present:", args.target in names)
    if args.target not in names:
        print(f"{args.target} is not a member; aborting")
        return 2
    if not OPENCODE.exists():
        print(f"OpenCode CLI not found: {OPENCODE}")
        return 2

    quoted = urllib.parse.quote(group, safe="")
    api(secret, "PUT", f"/proxies/{quoted}", {"name": args.target})
    now = api(secret, "GET", f"/proxies/{quoted}")["now"]
    print("switched:", repr(now))
    if now != args.target:
        print("switch did not apply; aborting")
        return 2

    env = os.environ.copy()
    if not args.keep_env:
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            env.pop(k, None)
        env["NO_PROXY"] = "*"
    if args.ca_bundle:
        env["NODE_EXTRA_CA_CERTS"] = str(args.ca_bundle)
        print("NODE_EXTRA_CA_CERTS:", args.ca_bundle)

    cmd = [
        str(OPENCODE),
        "run",
        "--standalone",
        "--model",
        args.model,
        args.prompt,
    ]
    print("running:", " ".join(cmd))
    ok = 0
    total = max(1, args.repeat)
    try:
        for attempt in range(1, total + 1):
            print(f"--- attempt {attempt}/{total} ---")
            try:
                r = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=args.timeout,
                    env=env,
                    cwd=str(Path.home()),
                )
                print("exit:", r.returncode)
                print("--- stdout ---")
                print(r.stdout[-6000:])
                print("--- stderr ---")
                print(r.stderr[-6000:])
                if r.returncode == 0:
                    ok += 1
            except subprocess.TimeoutExpired as e:
                print(f"timeout after {args.timeout}s")
                if e.stdout:
                    print("--- partial stdout ---")
                    print(str(e.stdout)[-4000:])
                if e.stderr:
                    print("--- partial stderr ---")
                    print(str(e.stderr)[-4000:])
        print(f"RESULT {ok}/{total} ok")
    finally:
        api(secret, "PUT", f"/proxies/{quoted}", {"name": old})
        restored = api(secret, "GET", f"/proxies/{quoted}")["now"]
        print("restored:", repr(restored))

    return 0


if __name__ == "__main__":
    sys.exit(main())
