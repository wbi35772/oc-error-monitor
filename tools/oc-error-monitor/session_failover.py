#!/usr/bin/env python3
"""Per-session model failover for OpenChamber / OpenCode.

Why this exists
---------------
The Clash layer can only move the *whole* OpenCode selector, so every
OpenChamber session shares one egress. But OpenCode's own API can change the
model of a *single* session:

    POST /api/session/{sessionID}/model
    {"model": {"providerID": "openrouter", "id": "minimax/minimax-m3.1-flash:free"}}

Measured 2026-10-20 with probe_matrix.py on one fixed node:
    opencode/deepseek-v4.1-flash-free   60% success (intermittent 403)
    opencode/minimax-m3.1-flash-free    80%
    opencode/glm-5.3-flash-free          80%
    openrouter/minimax/minimax-m3.1-flash:free   100%, no account, no API key

So the correct per-session mitigation for an `AUTH_IP_REPUTATION` failure is
"move this one session to a model whose upstream is reachable", not "rotate
the node for everyone".

This tool:
  - reads the managed OpenCode server URL + password (from the OpenChamber
    process environment; the password is never written to disk),
  - tails the P1 monitor's JSONL,
  - for a session that keeps failing with AUTH_IP_REPUTATION, switches that
    session's model to a configured fallback,
  - restores the original model when the session succeeds again.

Dry-run by default. `--apply` is required to change anything.

Usage:
  python session_failover.py --list-models
  python session_failover.py --list-sessions
  python session_failover.py --follow
  python session_failover.py --follow --apply
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent

# Fallback chain used when a session is stuck on a flaky model.
# Ordered best-first: measured 100% / 80% / 80% on 2026-10-20.
DEFAULT_FALLBACKS = [
    {"providerID": "openrouter", "id": "minimax/minimax-m3.1-flash:free", "variant": "default"},
    {"providerID": "opencode", "id": "minimax-m3.1-flash-free", "variant": "default"},
    {"providerID": "opencode", "id": "glm-5.3-flash-free", "variant": "default"},
]

# Error classes that justify moving a session to another model.
#
# The backtest (backtest.py, 2.1 days of real history) shows the threshold
# matters: requiring 3 failures fires only on sessions that were already
# surviving. 51 of the 52 sessions that died outright had exactly ONE
# AUTH_IP_REPUTATION error and were never tried again. So the default is
# to act on the first occurrence; the per-session blast radius is small
# (one session's model), unlike a global node switch.
FAILOVER_CLASSES = {"AUTH_IP_REPUTATION", "AUTH_FREE_TIER", "LOCAL_RELAY"}


# ---------------------------------------------------------------------------
# OpenCode server discovery
# ---------------------------------------------------------------------------

def _read_proc_env(pid: int) -> dict[str, str]:
    """Read a process environment via read_proc_env (remote PEB reads)."""
    sys.path.insert(0, str(HERE))
    from read_proc_env import process_environment  # type: ignore

    return process_environment(pid)


def _openchamber_pid() -> int | None:
    if os.name != "nt":
        return None
    try:
        out = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-Process -Name OpenChamber -ErrorAction SilentlyContinue | "
                "Select-Object -First 1 -ExpandProperty Id)",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout.strip()
    except Exception:
        return None
    return int(out) if out.isdigit() else None


def _managed_config_path(env: dict[str, str]) -> Path | None:
    raw = env.get("OPENCODE_CONFIG")
    return Path(raw) if raw else None


def discover_server() -> tuple[str, str]:
    """Return (base_url, password) for the managed OpenCode server."""
    explicit = os.environ.get("OPENCODE_SERVER_URL")
    explicit_pw = os.environ.get("OPENCODE_PASSWORD") or os.environ.get(
        "OPENCODE_SERVER_PASSWORD"
    )
    if explicit and explicit_pw:
        return explicit, explicit_pw

    pid = _openchamber_pid()
    if pid is None:
        raise SystemExit(
            "cannot locate OpenChamber; set OPENCODE_SERVER_URL and OPENCODE_PASSWORD"
        )
    env = _read_proc_env(pid)
    password = env.get("OPENCODE_SERVER_PASSWORD") or env.get("OPENCODE_PASSWORD") or ""
    if not password:
        raise SystemExit(
            f"no OpenCode password found in OpenChamber env (pid {pid}); "
            "set OPENCODE_PASSWORD"
        )

    # The port is assigned at launch; find it from the running opencode.exe.
    port = _managed_port()
    if not port:
        raise SystemExit("cannot determine the managed OpenCode port")
    return f"http://127.0.0.1:{port}", password


def _managed_port() -> int | None:
    if os.name != "nt":
        return None
    try:
        out = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "$p = Get-CimInstance Win32_Process -Filter "
                "\"Name='opencode.exe'\" | Where-Object { $_.CommandLine -match 'serve' } "
                "| Select-Object -First 1; if ($p) { "
                "if ($p.CommandLine -match '--port\\s+(\\d+)') { $matches[1] } }",
            ],
            capture_output=True,
            text=True,
            timeout=25,
        ).stdout.strip()
    except Exception:
        return None
    return int(out) if out.isdigit() else None


# ---------------------------------------------------------------------------
# OpenCode API client
# ---------------------------------------------------------------------------

class OpenCodeClient:
    def __init__(self, base_url: str, password: str, timeout: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        token = base64.b64encode(f"opencode:{password}".encode("utf-8")).decode("ascii")
        self.headers = {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
        }

    def request(self, method: str, path: str, body: Any = None) -> Any:
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers=self.headers,
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read()
            if not raw:
                return None
            return json.loads(raw.decode("utf-8"))

    def sessions(self) -> list[dict[str, Any]]:
        data = self.request("GET", "/api/session")
        rows = data.get("data") if isinstance(data, dict) else data
        return rows or []

    def models(self) -> list[dict[str, Any]]:
        data = self.request("GET", "/api/model")
        rows = data.get("data") if isinstance(data, dict) else data
        return rows or []

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        try:
            data = self.request("GET", f"/api/session/{session_id}")
        except urllib.error.HTTPError:
            return None
        return data.get("data") if isinstance(data, dict) else data

    def switch_model(self, session_id: str, model: dict[str, str]) -> int:
        quoted = urllib.parse.quote(session_id, safe="")
        req = urllib.request.Request(
            self.base_url + f"/api/session/{quoted}/model",
            data=json.dumps({"model": model}).encode("utf-8"),
            method="POST",
            headers=self.headers,
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return resp.status


# ---------------------------------------------------------------------------
# Failover policy
# ---------------------------------------------------------------------------

class FailoverPolicy:
    """Decide whether a session should move to a fallback model."""

    def __init__(self, state: dict[str, Any], min_failures: int, window: float) -> None:
        self.state = state
        self.min_failures = min_failures
        self.window = window

    def failures(self, session_id: str, now: float) -> list[float]:
        key = f"fail:{session_id}"
        entries = self.state.get("failures", {}).get(key, [])
        return [float(e[0]) for e in entries if now - float(e[0]) < self.window]

    def record_failure(self, session_id: str, error_class: str, now: float) -> int:
        """Record one failure for a session and return the running count.

        Failures are counted per session across all failover-eligible
        classes: a session that keeps failing for any of these reasons is
        equally stuck, and the fallback model fixes all of them.
        """
        buckets = self.state.setdefault("failures", {})
        key = f"fail:{session_id}"
        entries = [e for e in buckets.get(key, []) if now - float(e[0]) < self.window]
        entries.append([now, error_class])
        buckets[key] = entries
        return len(entries)

    def failure_mix(self, session_id: str) -> dict[str, int]:
        mix: dict[str, int] = {}
        for entry in (self.state.get("failures") or {}).get(f"fail:{session_id}", []):
            cls = entry[1] if len(entry) > 1 else "?"
            mix[cls] = mix.get(cls, 0) + 1
        return mix

    def clear_failures(self, session_id: str) -> None:
        (self.state.setdefault("failures", {})).pop(f"fail:{session_id}", None)

    def should_failover(self, session_id: str, now: float) -> tuple[bool, str]:
        count = len(self.failures(session_id, now))
        if count < self.min_failures:
            return False, f"{count}/{self.min_failures} recent failures"
        return True, f"{count} failures within {int(self.window)}s"

    def current_model(self, session_id: str) -> dict[str, str] | None:
        return (self.state.get("original_models") or {}).get(session_id)

    def remember_original(self, session_id: str, model: dict[str, str]) -> None:
        models = self.state.setdefault("original_models", {})
        models.setdefault(session_id, model)

    def fallback_for(self, current: dict[str, str] | None, chain: list[dict[str, str]]):
        """First fallback in the chain that is not the current model."""
        if not current:
            return chain[0] if chain else None
        cur_key = (current.get("providerID"), current.get("id"))
        for cand in chain:
            if (cand.get("providerID"), cand.get("id")) != cur_key:
                return cand
        return None


def load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("failures", {})
            data.setdefault("original_models", {})
            data.setdefault("applied", {})
            return data
    except Exception:
        pass
    return {"failures": {}, "original_models": {}, "applied": {}}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Event tailer
# ---------------------------------------------------------------------------

class EventTailer:
    def __init__(self, events_dir: Path, state: dict[str, Any], from_start: bool) -> None:
        self.events_dir = events_dir
        self.offsets: dict[str, int] = state.setdefault("offsets", {})
        self.from_start = from_start

    def files(self) -> list[Path]:
        return sorted(self.events_dir.glob("events-*.jsonl"))

    def bootstrap(self) -> None:
        for path in self.files():
            key = str(path)
            if key not in self.offsets:
                self.offsets[key] = 0 if self.from_start else path.stat().st_size

    def poll(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for path in self.files():
            key = str(path)
            try:
                size = path.stat().st_size
            except OSError:
                continue
            offset = int(self.offsets.get(key, 0))
            if size < offset:
                offset = 0
            if size == offset:
                continue
            try:
                with path.open("rb") as fh:
                    fh.seek(offset)
                    chunk = fh.read()
            except OSError:
                continue
            complete = chunk.rsplit(b"\n", 1)[0] + b"\n" if b"\n" in chunk else b""
            for raw in complete.splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw.decode("utf-8-sig", "replace"))
                except Exception:
                    continue
                if isinstance(obj, dict):
                    events.append(obj)
            self.offsets[key] = offset + len(complete)
        return events


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Per-session model failover for OpenCode.")
    p.add_argument("--events-dir", type=Path, default=HERE.parents[1] / "logs" / "resilience")
    p.add_argument("--state", type=Path, default=HERE.parents[1] / "logs" / "resilience" / "failover-state.json")
    p.add_argument("--server-url", default=None, help="Override the OpenCode server URL")
    p.add_argument("--password", default=None, help="Override the OpenCode server password")
    p.add_argument(
        "--fallbacks",
        default=None,
        help="Comma-separated providerID:id[:variant] fallback chain",
    )
    p.add_argument(
        "--min-failures",
        type=int,
        default=1,
        help="Failures before a session moves (1 = act on the first failure)",
    )
    p.add_argument("--window", type=float, default=600.0, help="Failure window in seconds")
    p.add_argument("--restore-on-success", action="store_true", help="Restore the original model when a session succeeds")
    p.add_argument("--apply", action="store_true", help="Actually change session models (default: dry-run)")
    p.add_argument("--once", action="store_true", help="Process pending events and exit")
    p.add_argument("--from-start", action="store_true", help="Replay all events from the beginning")
    p.add_argument(
        "--event-time",
        action="store_true",
        help="Use each event's own timestamp for the failure window (historical replay)",
    )
    p.add_argument("--list-models", action="store_true")
    p.add_argument("--list-sessions", action="store_true")
    p.add_argument(
        "--scores",
        type=Path,
        default=HERE.parents[1] / "logs" / "resilience" / "node-scores.json",
        help="Measured model/node success rates written by probe_matrix.py",
    )
    p.add_argument("--interval", type=float, default=5.0)
    return p


def load_model_scores(path: Path) -> dict[str, float]:
    """Load measured per-model success rates (written by probe_matrix.py).

    Returns a map of "providerID/id" -> measured success rate.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    scores: dict[str, float] = {}
    for model, info in (data.get("model_results") or {}).items():
        rate = info.get("rate") if isinstance(info, dict) else info
        if isinstance(rate, (int, float)):
            scores[model] = float(rate)
    return scores


def chain_from_scores(scores: dict[str, float]) -> list[dict[str, str]]:
    """Build a fallback chain from measured model success rates, best first."""
    chain: list[dict[str, str]] = []
    for model, _rate in sorted(scores.items(), key=lambda kv: kv[1], reverse=True):
        provider, _, model_id = model.partition("/")
        if not model_id:
            continue
        chain.append({"providerID": provider, "id": model_id, "variant": "default"})
    return chain


def event_time(event: dict[str, Any]) -> float | None:
    """Epoch seconds of an event's own timestamp, if it has one.

    For historical replay: the failure window is wall-clock based, so
    replaying two days of history against the real clock would collapse
    every event into one instant.
    """
    raw = event.get("ts")
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.timestamp()


def parse_chain(raw: str | None) -> list[dict[str, str]]:
    if not raw:
        return list(DEFAULT_FALLBACKS)
    chain = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":")
        provider = parts[0]
        model_id = parts[1] if len(parts) > 1 else ""
        variant = parts[2] if len(parts) > 2 else "default"
        chain.append({"providerID": provider, "id": model_id, "variant": variant})
    return chain


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    base_url = args.server_url
    password = args.password
    if not base_url or not password:
        base_url, discovered = discover_server()
        password = password or discovered

    client = OpenCodeClient(base_url, password)
    chain = parse_chain(args.fallbacks)
    if not args.fallbacks:
        # No explicit chain: prefer the models probe_matrix.py measured.
        measured = chain_from_scores(load_model_scores(args.scores))
        if measured:
            chain = measured

    if args.list_models:
        print(f"server: {base_url}")
        for m in client.models():
            pid = m.get("providerID")
            mid = m.get("id")
            if pid in ("opencode", "openrouter") and (
                str(mid).endswith("-free") or str(mid).endswith(":free")
            ):
                print(f"  {pid:12} {mid}")
        return 0

    if args.list_sessions:
        for s in client.sessions()[:40]:
            model = s.get("model") or {}
            title = (s.get("title") or "")[:40]
            print(
                f"{s.get('id')} | {model.get('providerID')}/{model.get('id')} | {title}"
            )
        return 0

    state = load_state(args.state)
    policy = FailoverPolicy(state, args.min_failures, args.window)
    tailer = EventTailer(args.events_dir, state, args.from_start)
    tailer.bootstrap()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[failover] mode={mode} server={base_url} min_failures={args.min_failures}")
    chain_desc = ", ".join(c["providerID"] + "/" + c["id"] for c in chain)
    print(f"[failover] fallback chain: {chain_desc}")
    print("[failover] no session is modified without --apply", flush=True)

    processed = 0
    actions = 0

    def handle(event: dict[str, Any]) -> None:
        nonlocal actions
        cls = str(event.get("error_class") or "")
        session_id = event.get("session_id")
        if not session_id or cls not in FAILOVER_CLASSES:
            return
        now = time.time()
        if args.event_time:
            stamp = event_time(event)
            if stamp is not None:
                now = stamp
        count = policy.record_failure(session_id, cls, now)
        ok, reason = policy.should_failover(session_id, now)
        mix = policy.failure_mix(session_id)
        mix_txt = " ".join(f"{k}={v}" for k, v in sorted(mix.items()))
        print(
            f"[failover] {cls} {session_id} failures={count} [{mix_txt}] "
            f"{'SWITCH' if ok else 'WAIT'} ({reason})",
            flush=True,
        )
        if not ok:
            return

        session = client.get_session(session_id)
        if not session:
            return
        current = session.get("model") or {}
        policy.remember_original(session_id, current)
        target = policy.fallback_for(current, chain)
        if not target:
            print(f"[failover] no fallback available for {session_id}", flush=True)
            return

        # Do not re-switch a session that is already on a fallback model.
        # The live `current` model does not change during a dry-run, so the
        # shadow record below is what stops repeats: compare the *target* we
        # would switch to against the target we already chose.
        applied = (state.get("applied") or {}).get(session_id)
        if applied:
            prev_to = applied.get("to") or {}
            if (prev_to.get("providerID"), prev_to.get("id")) == (
                target.get("providerID"),
                target.get("id"),
            ):
                print(
                    f"[failover] SKIP {session_id}: already targeted "
                    f"{target.get('providerID')}/{target.get('id')}",
                    flush=True,
                )
                return

        if not args.apply:
            print(
                f"[failover] WOULD_SWITCH {session_id}: "
                f"{current.get('providerID')}/{current.get('id')} -> "
                f"{target['providerID']}/{target['id']}",
                flush=True,
            )
            # Shadow the switch so the preview matches --apply: without this
            # every repeat event would print another WOULD_SWITCH.
            state.setdefault("applied", {})[session_id] = {
                "from": current,
                "to": target,
                "ts": now,
                "dry_run": True,
            }
            actions += 1
            return

        try:
            status = client.switch_model(session_id, target)
        except urllib.error.HTTPError as exc:
            print(f"[failover] switch failed for {session_id}: HTTP {exc.code}", flush=True)
            return
        except Exception as exc:
            print(f"[failover] switch failed for {session_id}: {exc}", flush=True)
            return

        state.setdefault("applied", {})[session_id] = {
            "from": current,
            "to": target,
            "ts": time.time(),
        }
        print(
            f"[failover] SWITCHED {session_id} HTTP {status}: "
            f"{current.get('providerID')}/{current.get('id')} -> "
            f"{target['providerID']}/{target['id']}",
            flush=True,
        )
        actions += 1

    try:
        if args.once:
            for event in tailer.poll():
                processed += 1
                handle(event)
        else:
            print("[failover] watching events; Ctrl+C to stop", flush=True)
            while True:
                for event in tailer.poll():
                    processed += 1
                    handle(event)
                save_state(args.state, state)
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n[failover] stopped", flush=True)
    finally:
        save_state(args.state, state)
        print(f"[failover] processed={processed} actions={actions}", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
