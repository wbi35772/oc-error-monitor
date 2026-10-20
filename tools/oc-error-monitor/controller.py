#!/usr/bin/env python3
"""P2 controller for OpenChamber / OpenCode network resilience.

It tails the P1 monitor's JSONL events, applies an error-to-action policy,
and (only with --apply) switches the dedicated mihomo selector group for
transient network errors.

Design boundaries:
  - Auth / entitlement / IP-reputation / TLS errors are NEVER fixed by
    rotating proxy nodes. They are logged and surfaced only.
  - Only NET_DNS, NET_CONNECT, NET_STREAM and NET_TIMEOUT may switch nodes.
  - Dry-run is the default. --apply is required for any mihomo write.
  - A dedicated group (default: substring "OpenCode") is the only group
    this controller is allowed to touch.
  - Cooldowns, per-class cooldowns, hourly/15-minute budgets, candidate
    health checks, and a state file limit flapping.

Usage:
  python controller.py --list-candidates
  python controller.py --follow                 # dry-run
  python controller.py --follow --apply         # real switching
  python controller.py --once --from-start      # replay for testing
  python controller.py --self-test              # PUT current node back (safe write test)

Exit with Ctrl+C.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# Constants / paths
# ---------------------------------------------------------------------------

DEFAULT_API = "http://127.0.0.1:9097"
DEFAULT_GROUP_HINT = "OpenCode"
DEFAULT_HEALTH_URL = "https://www.gstatic.com/generate_204"

SWITCH_CLASSES = {"NET_DNS", "NET_CONNECT", "NET_STREAM", "NET_TIMEOUT"}

# Classes that are explicitly not network-node problems.
NO_SWITCH_REASONS = {
    "AUTH_FREE_TIER": "client entitlement; switch provider or use OpenCode itself",
    "AUTH_CREDENTIAL": "credential problem; refresh the credential",
    "RATE_LIMIT": "provider rate limit; back off, do not rotate nodes",
    "UPSTREAM_5XX": "upstream error; retry/failover, do not rotate nodes",
    "NET_TLS": "TLS/certificate problem; fix trust/time/egress, do not rotate nodes",
    "LOCAL_RELAY": "local relay is down; start it or switch provider",
    "CLIENT_REQUEST": "request/context problem; adjust the request",
    "ABORTED": "user interrupted; no action",
    "UNKNOWN": "unknown error; record and notify",
}

# `AUTH_IP_REPUTATION` is measured to be intermittent (probe_matrix.py,
# 2026-10-20: 3/8 success on one fixed node/model, 25%-75% across nodes),
# so a single 403 must not trigger a node switch. It only becomes
# switch-eligible once the same class has failed repeatedly in a window.
PERSISTENT_CLASSES = {"AUTH_IP_REPUTATION"}
PERSISTENT_MIN_FAILURES = 3
PERSISTENT_WINDOW = 600.0

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


def default_events_dir() -> Path:
    # tools/oc-error-monitor/controller.py -> workspace/logs/resilience
    return Path(__file__).resolve().parents[2] / "logs" / "resilience"


def default_state_path() -> Path:
    return default_events_dir() / "controller-state.json"


def default_clash_config() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "io.github.clash-verge-rev.clash-verge-rev" / "config.yaml"
    return Path.home() / "AppData" / "Roaming" / "io.github.clash-verge-rev.clash-verge-rev" / "config.yaml"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def read_secret_from_clash_config(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    match = re.search(r"(?m)^\s*secret:\s*(.+?)\s*$", text)
    if not match:
        return ""
    return match.group(1).strip().strip('"').strip("'")


# ---------------------------------------------------------------------------
# mihomo API
# ---------------------------------------------------------------------------

class MihomoError(RuntimeError):
    pass


class MihomoClient:
    def __init__(self, api: str, secret: str, timeout: float = 10.0) -> None:
        self.api = api.rstrip("/")
        self.secret = secret
        self.timeout = timeout

    def _request(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        url = self.api + path
        headers = {"Accept": "application/json"}
        if self.secret:
            headers["Authorization"] = f"Bearer {self.secret}"
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                status = int(resp.status)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            text = raw.decode("utf-8", "replace")[:800]
            raise MihomoError(f"{method} {path} -> HTTP {exc.code}: {text}") from exc
        except Exception as exc:  # URLError, timeout, connection refused...
            raise MihomoError(f"{method} {path} failed: {exc}") from exc

        if not raw:
            return status, None
        try:
            return status, json.loads(raw.decode("utf-8"))
        except Exception:
            return status, raw.decode("utf-8", "replace")

    def get_proxies(self) -> dict[str, Any]:
        _, data = self._request("GET", "/proxies")
        if not isinstance(data, dict) or not isinstance(data.get("proxies"), dict):
            raise MihomoError("/proxies returned an unexpected payload")
        return data["proxies"]

    def find_group(self, hint: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
        proxies = self.get_proxies()
        if hint in proxies and proxies[hint].get("type") == "Selector":
            return hint, proxies[hint], proxies

        matches = [
            name
            for name, obj in proxies.items()
            if hint.lower() in name.lower() and isinstance(obj, dict) and obj.get("type") == "Selector"
        ]
        if not matches:
            raise MihomoError(f"no Selector group matching {hint!r}")
        if len(matches) > 1:
            raise MihomoError(f"ambiguous Selector group {hint!r}: {matches}")
        name = matches[0]
        return name, proxies[name], proxies

    def set_group(self, group: str, node: str) -> int:
        path = f"/proxies/{urllib.parse.quote(group, safe='')}"
        status, _ = self._request("PUT", path, {"name": node})
        return status

    def get_delay(self, node: str, url: str, timeout_ms: int) -> int:
        path = (
            f"/proxies/{urllib.parse.quote(node, safe='')}/delay"
            f"?url={urllib.parse.quote(url, safe='')}&timeout={int(timeout_ms)}"
        )
        _, data = self._request("GET", path)
        if isinstance(data, dict) and "delay" in data:
            try:
                return int(data["delay"])
            except (TypeError, ValueError):
                pass
        raise MihomoError(f"delay probe for {node!r} returned {data!r}")


# ---------------------------------------------------------------------------
# Candidate selection
# ---------------------------------------------------------------------------

def build_candidates(
    group: dict[str, Any],
    proxies: dict[str, Any],
    allow_direct: bool,
    keep_nodes: set[str] | None = None,
) -> list[str]:
    """Return leaf-node candidates in the group's order, starting after `now`.

    `keep_nodes` holds nodes with a measured success rate. mihomo's cached
    `alive` flag can be stale (a node that measured 80% minutes ago may still
    be flagged dead), so measured nodes are kept in the candidate list even
    when `alive` is False. They still have to pass the live health probe
    before the controller actually switches to them.
    """
    all_names = list(group.get("all") or [])
    current = group.get("now")
    keep = keep_nodes or set()

    allowed: list[str] = []
    for name in all_names:
        if name == current:
            continue
        is_direct = name.upper() == "DIRECT"
        if is_direct and not allow_direct:
            continue
        if SKIP_NAME_RE.search(name) and not (is_direct and allow_direct):
            continue
        obj = proxies.get(name)
        if isinstance(obj, dict):
            is_allowed_direct = allow_direct and name.upper() == "DIRECT"
            if obj.get("type") in SKIP_GROUP_TYPES and not is_allowed_direct:
                continue
            if obj.get("alive") is False and not is_allowed_direct and name not in keep:
                continue
        allowed.append(name)

    if not allowed:
        return []

    if current in all_names:
        start = all_names.index(current) + 1
        rotated = all_names[start:] + all_names[:start]
        allowed_set = set(allowed)
        ordered = [name for name in rotated if name in allowed_set]
        if ordered:
            return ordered
    return allowed


def rotate(items: list[str], cursor: int) -> list[str]:
    if not items:
        return []
    cursor %= len(items)
    return items[cursor:] + items[:cursor]


def load_node_scores(path: Path) -> dict[str, float]:
    """Load measured per-node free-model success rates.

    Written by probe_matrix.py. Latency is a poor predictor of OpenCode
    free-tier success, so these measured rates take priority when the
    controller picks a node.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    scores: dict[str, float] = {}
    for row in data.get("results") or []:
        node = row.get("node")
        rate = row.get("rate")
        if isinstance(node, str) and isinstance(rate, (int, float)):
            scores[node] = float(rate)
    return scores


def probe_candidates(
    client: MihomoClient,
    names: list[str],
    health_url: str,
    timeout_ms: int,
    max_delay: int,
    workers: int = 4,
    scores: dict[str, float] | None = None,
) -> tuple[str | None, dict[str, int | None]]:
    """Probe candidates in parallel, preserving order for selection.

    Latency alone is a poor predictor of OpenCode free-tier success
    (measured 2026-10-20 with probe_matrix.py: 移动-01 at 200 ms succeeded
    67% of the time, 多线-01 at 615 ms succeeded 83%, 电信-21 at 583 ms only
    33%). When measured success rates are known they take priority; latency
    only breaks ties and acts as a liveness filter.
    """
    results: dict[str, int | None] = {name: None for name in names}
    if not names:
        return None, results

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(names)))) as pool:
        future_map = {
            pool.submit(client.get_delay, name, health_url, timeout_ms): name for name in names
        }
        for future in as_completed(future_map):
            name = future_map[future]
            try:
                results[name] = int(future.result())
            except Exception:
                results[name] = None

    def rank(name: str) -> tuple[int, float, int]:
        delay = results.get(name)
        alive = 1 if (delay is not None and 0 <= delay <= max_delay) else 0
        rate = (scores or {}).get(name, -1.0)
        # alive first, then measured success rate, then latency.
        return (alive, rate, -(delay if delay is not None else 10**6))

    for name in sorted(names, key=rank, reverse=True):
        if rank(name)[0] == 1:
            return name, results
    return None, results


# ---------------------------------------------------------------------------
# Event tailing / state
# ---------------------------------------------------------------------------

class EventTailer:
    def __init__(self, events_dir: Path, state: dict[str, Any], from_start: bool) -> None:
        self.events_dir = events_dir
        self.state = state
        self.from_start = from_start
        self.offsets: dict[str, int] = state.setdefault("offsets", {})
        self.bootstrapped = False

    def _files(self) -> list[Path]:
        return sorted(self.events_dir.glob("events-*.jsonl"))

    def bootstrap(self) -> None:
        self.events_dir.mkdir(parents=True, exist_ok=True)
        for path in self._files():
            key = str(path)
            if key not in self.offsets:
                self.offsets[key] = 0 if self.from_start else path.stat().st_size
        self.bootstrapped = True

    def poll(self) -> list[dict[str, Any]]:
        if not self.bootstrapped:
            self.bootstrap()

        events: list[dict[str, Any]] = []
        for path in self._files():
            key = str(path)
            try:
                size = path.stat().st_size
            except OSError:
                continue

            offset = int(self.offsets.get(key, 0))
            if size < offset:
                offset = 0  # truncated or replaced
            if size == offset:
                continue

            try:
                with path.open("rb") as fh:
                    fh.seek(offset)
                    chunk = fh.read()
            except OSError as exc:
                print(f"[controller] cannot read {path}: {exc}", file=sys.stderr)
                continue

            if not chunk:
                continue

            last_nl = chunk.rfind(b"\n")
            if last_nl < 0:
                # Partial line; wait for the monitor to finish it.
                continue

            complete = chunk[: last_nl + 1]
            self.offsets[key] = offset + len(complete)

            for raw in complete.splitlines():
                if not raw.strip():
                    continue
                try:
                    obj = json.loads(raw.decode("utf-8-sig", "replace"))
                except Exception as exc:
                    print(f"[controller] skipped malformed event in {path}: {exc}", file=sys.stderr)
                    continue
                if isinstance(obj, dict):
                    events.append(obj)
        return events


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "offsets": {},
            "switch_times": [],
            "class_last_action": {},
            "class_failures": {},
            "last_switch_ts": 0.0,
            "candidate_cursor": 0,
            "original_group": None,
            "original_node": None,
            "changed": False,
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("state is not an object")
        data.setdefault("class_failures", {})
        return data
    except Exception as exc:
        backup = path.with_suffix(path.suffix + f".corrupt-{int(time.time())}")
        try:
            path.replace(backup)
        except OSError:
            pass
        print(f"[controller] corrupt state moved to {backup}: {exc}", file=sys.stderr)
        return {
            "offsets": {},
            "switch_times": [],
            "class_last_action": {},
            "class_failures": {},
            "last_switch_ts": 0.0,
            "candidate_cursor": 0,
            "original_group": None,
            "original_node": None,
            "changed": False,
        }


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Decision:
    kind: str  # "SWITCH" | "NOOP" | "SKIP"
    reason: str


def decide(
    event: dict[str, Any],
    state: dict[str, Any],
    now: float,
    min_switch_interval: float,
    class_cooldown: float,
    max_per_hour: int,
    max_per_15min: int,
) -> Decision:
    error_class = str(event.get("error_class") or "UNKNOWN")

    if error_class in PERSISTENT_CLASSES:
        # Count recent occurrences of this class. Only a sustained burst is
        # treated as a real network problem worth a node switch.
        fails = state.setdefault("class_failures", {}).setdefault(error_class, [])
        fails = [float(t) for t in fails if now - float(t) < PERSISTENT_WINDOW]
        fails.append(now)
        state["class_failures"][error_class] = fails
        if len(fails) < PERSISTENT_MIN_FAILURES:
            return Decision(
                "NOOP",
                f"{error_class} is intermittent; retry first "
                f"({len(fails)}/{PERSISTENT_MIN_FAILURES} failures in "
                f"{int(PERSISTENT_WINDOW)}s)",
            )

    if error_class not in SWITCH_CLASSES and error_class not in PERSISTENT_CLASSES:
        return Decision("NOOP", NO_SWITCH_REASONS.get(error_class, "not a network-switch class"))

    last_switch = float(state.get("last_switch_ts") or 0.0)
    if now - last_switch < min_switch_interval:
        return Decision(
            "SKIP",
            f"min switch interval not elapsed ({int(now - last_switch)}s < {int(min_switch_interval)}s)",
        )

    class_last = float((state.get("class_last_action") or {}).get(error_class) or 0.0)
    if now - class_last < class_cooldown:
        return Decision(
            "SKIP",
            f"class cooldown not elapsed ({int(now - class_last)}s < {int(class_cooldown)}s)",
        )

    switch_times = [float(t) for t in (state.get("switch_times") or []) if now - float(t) < 3600]
    if len(switch_times) >= max_per_hour:
        return Decision("SKIP", f"hourly switch budget reached ({len(switch_times)}/{max_per_hour})")

    last_15 = [t for t in switch_times if now - t < 900]
    if len(last_15) >= max_per_15min:
        return Decision(
            "SKIP",
            f"15-minute switch budget reached ({len(last_15)}/{max_per_15min})",
        )

    return Decision("SWITCH", f"{error_class} is eligible for a node switch")


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class Controller:
    def __init__(
        self,
        args: argparse.Namespace,
        client: MihomoClient,
        state: dict[str, Any],
        state_path: Path,
    ) -> None:
        self.args = args
        self.client = client
        self.state = state
        self.state_path = state_path
        self.events_dir: Path = args.events_dir
        self.node_scores: dict[str, float] = load_node_scores(args.node_scores)
        self._dry_state: dict[str, Any] | None = None
        self.stats: Counter[str] = Counter()
        self.action_count = 0

    # -- logging -----------------------------------------------------------

    def action_file(self) -> Path:
        return self.events_dir / f"actions-{datetime.now().astimezone().date().isoformat()}.jsonl"

    def log_action(self, record: dict[str, Any]) -> None:
        self.events_dir.mkdir(parents=True, exist_ok=True)
        with self.action_file().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.action_count += 1

    def print_record(self, record: dict[str, Any]) -> None:
        cls = record.get("error_class", "?")
        decision = record.get("decision", "?")
        sid = record.get("session_id") or "-"
        frm = record.get("from_node") or "-"
        to = record.get("to_node") or "-"
        reason = record.get("reason") or ""
        if decision in ("SWITCHED", "WOULD_SWITCH"):
            print(
                f"[{record['ts']}] {cls:<18} {decision:<12} {sid} | {frm} -> {to} | {reason}",
                flush=True,
            )
        else:
            print(f"[{record['ts']}] {cls:<18} {decision:<12} {sid} | {reason}", flush=True)

    # -- state -------------------------------------------------------------

    def save(self) -> None:
        save_state(self.state_path, self.state)

    def budget_state(self) -> dict[str, Any]:
        """State used for budget decisions.

        Under `--apply` this is the real persisted state. In dry-run it is a
        shadow copy so the preview shows exactly what `--apply` would do
        (respecting the 3/hour cap) without consuming the real budget.
        """
        if self.args.apply:
            return self.state
        if self._dry_state is None:
            self._dry_state = {
                "switch_times": list(self.state.get("switch_times") or []),
                "class_last_action": dict(self.state.get("class_last_action") or {}),
                "last_switch_ts": float(self.state.get("last_switch_ts") or 0.0),
                "class_failures": self.state.get("class_failures") or {},
            }
        return self._dry_state

    def record_attempt(
        self,
        error_class: str,
        now: float,
        success: bool,
        state: dict[str, Any] | None = None,
    ) -> None:
        target = state if state is not None else self.budget_state()
        target["last_switch_ts"] = now
        times = [
            float(t)
            for t in (target.get("switch_times") or [])
            if now - float(t) < 3600
        ]
        times.append(now)
        target["switch_times"] = times[-20:]
        class_last = target.setdefault("class_last_action", {})
        class_last[error_class] = now
        if success:
            target["changed"] = True

    def remember_group(self, group: str, node: str | None) -> None:
        if not self.state.get("original_group"):
            self.state["original_group"] = group
            self.state["original_node"] = node

    # -- actions -----------------------------------------------------------

    def handle(self, event: dict[str, Any]) -> None:
        now = time.time()
        error_class = str(event.get("error_class") or "UNKNOWN")
        self.stats[error_class] += 1

        record: dict[str, Any] = {
            "ts": now_iso(),
            "event_ts": event.get("ts"),
            "source": event.get("source"),
            "raw_id": event.get("raw_id"),
            "session_id": event.get("session_id"),
            "session_title": event.get("session_title"),
            "provider": event.get("provider"),
            "model": event.get("model"),
            "error_class": error_class,
            "status": event.get("status"),
            "error_type": event.get("error_type"),
            "message": event.get("message"),
            "apply": bool(self.args.apply),
            "dry_run": not bool(self.args.apply),
        }

        decision = decide(
            event,
            self.budget_state(),
            now,
            self.args.min_switch_interval,
            self.args.class_cooldown,
            self.args.max_per_hour,
            self.args.max_per_15min,
        )

        if decision.kind != "SWITCH":
            record["decision"] = decision.kind
            record["reason"] = decision.reason
            record["result"] = "noop"
            self.log_action(record)
            self.print_record(record)
            return

        # Resolve the dedicated group only when a switch is actually eligible.
        try:
            group_name, group, proxies = self.client.find_group(self.args.group)
        except MihomoError as exc:
            record["decision"] = "ERROR"
            record["reason"] = f"mihomo group resolution failed: {exc}"
            record["result"] = "error"
            self.log_action(record)
            self.print_record(record)
            return

        if group.get("type") != "Selector":
            record["decision"] = "ERROR"
            record["reason"] = f"group {group_name!r} is not a Selector"
            record["result"] = "error"
            self.log_action(record)
            self.print_record(record)
            return

        self.remember_group(group_name, group.get("now"))
        from_node = group.get("now")
        record["group"] = group_name
        record["from_node"] = from_node

        all_candidates = build_candidates(
            group, proxies, self.args.allow_direct, set(self.node_scores)
        )
        if not all_candidates:
            record["decision"] = "NO_CANDIDATE"
            record["reason"] = "no leaf candidates left after filters"
            record["result"] = "noop"
            self.record_attempt(error_class, now, success=False)
            self.log_action(record)
            self.print_record(record)
            return

        cursor = int(self.state.get("candidate_cursor") or 0)
        rotated = rotate(all_candidates, cursor)
        max_candidates = max(1, self.args.max_candidates)
        record["candidate_cursor"] = cursor

        # If measured success rates are known, they lead the batch: a pure
        # rotation window can miss the reliable nodes entirely.
        batch: list[str] = []
        if self.node_scores:
            scored = sorted(
                (
                    (rate, name)
                    for name, rate in self.node_scores.items()
                    if name in all_candidates
                ),
                reverse=True,
            )
            batch.extend(name for _, name in scored[:max_candidates])
        for name in rotated:
            if len(batch) >= max_candidates:
                break
            if name not in batch:
                batch.append(name)
        record["candidates"] = batch

        if self.args.no_healthcheck:
            chosen = batch[0]
            health: dict[str, int | None] = {}
            record["health"] = health
        else:
            chosen, health = probe_candidates(
                self.client,
                batch,
                self.args.health_url,
                self.args.health_timeout,
                self.args.max_delay,
                self.args.health_workers,
                scores=self.node_scores,
            )
            record["health"] = health

        if not chosen:
            record["decision"] = "NO_HEALTHY_NODE"
            record["reason"] = (
                f"{len(batch)} candidate(s) probed, none under {self.args.max_delay}ms"
            )
            record["result"] = "noop"
            self.record_attempt(error_class, now, success=False)
            self.log_action(record)
            self.print_record(record)
            return

        record["to_node"] = chosen

        if not self.args.apply:
            record["decision"] = "WOULD_SWITCH"
            record["reason"] = decision.reason
            record["result"] = "dry_run"
            # Consume the shadow budget so the preview matches --apply.
            self.record_attempt(error_class, now, success=False)
            self.log_action(record)
            self.print_record(record)
            return

        try:
            status = self.client.set_group(group_name, chosen)
        except MihomoError as exc:
            record["decision"] = "SWITCH_FAILED"
            record["reason"] = decision.reason
            record["error"] = str(exc)
            record["result"] = "error"
            self.record_attempt(error_class, now, success=False)
            self.log_action(record)
            self.print_record(record)
            return

        # Verify the change took effect.
        verified_node: str | None = None
        verify_error: str | None = None
        try:
            _, group_after, _ = self.client.find_group(group_name)
            verified_node = group_after.get("now")
        except MihomoError as exc:
            verify_error = str(exc)

        ok = verified_node == chosen
        record["decision"] = "SWITCHED" if ok else "SWITCH_UNVERIFIED"
        record["reason"] = decision.reason
        record["http_status"] = status
        record["verified_node"] = verified_node
        if verify_error:
            record["verify_error"] = verify_error
        record["result"] = "ok" if ok else "mismatch"
        self.record_attempt(error_class, now, success=ok)
        if ok:
            self.state["candidate_cursor"] = 0
        else:
            self.state["candidate_cursor"] = (cursor + len(batch)) % len(all_candidates)
        self.log_action(record)
        self.print_record(record)

    # -- diagnostics -------------------------------------------------------

    def list_candidates(self) -> int:
        try:
            group_name, group, proxies = self.client.find_group(self.args.group)
        except MihomoError as exc:
            print(f"[controller] {exc}", file=sys.stderr)
            return 2

        current = group.get("now")
        all_candidates = build_candidates(
            group, proxies, self.args.allow_direct, set(self.node_scores)
        )
        print(f"group:   {group_name}")
        print(f"type:    {group.get('type')}")
        print(f"now:     {current}")
        print(f"members: {len(group.get('all') or [])}")
        print(f"leaves:  {len(all_candidates)}")
        if not all_candidates:
            return 0

        cursor = int(self.state.get("candidate_cursor") or 0)
        rotated = rotate(all_candidates, cursor)
        max_candidates = max(1, self.args.max_candidates)
        print(f"cursor:  {cursor}")
        if self.node_scores:
            print(f"scored:  {len(self.node_scores)} node(s) with measured rates")
            scored = sorted(
                (
                    (rate, name)
                    for name, rate in self.node_scores.items()
                    if name in all_candidates
                ),
                reverse=True,
            )
            batch = [name for _, name in scored[:max_candidates]]
        else:
            batch = []
        for name in rotated:
            if len(batch) >= max_candidates:
                break
            if name not in batch:
                batch.append(name)
        print(f"batch:   {len(batch)}")
        if self.args.no_healthcheck:
            for name in batch:
                print(f"  - {name}")
            return 0

        chosen, health = probe_candidates(
            self.client,
            batch,
            self.args.health_url,
            self.args.health_timeout,
            self.args.max_delay,
            self.args.health_workers,
            scores=self.node_scores,
        )
        for name in batch:
            delay = health.get(name)
            mark = "OK" if delay is not None and 0 <= delay <= self.args.max_delay else "FAIL"
            rate = self.node_scores.get(name)
            rate_txt = f" rate={rate:.0%}" if rate is not None else ""
            print(
                f"  [{mark}] {delay if delay is not None else '-':>6} ms  {name}{rate_txt}"
            )
        print(f"would choose: {chosen or '(none)'}")
        return 0

    def self_test(self) -> int:
        try:
            group_name, group, _ = self.client.find_group(self.args.group)
        except MihomoError as exc:
            print(f"[controller] {exc}", file=sys.stderr)
            return 2

        current = group.get("now")
        if not current:
            print(f"[controller] group {group_name!r} has no current node", file=sys.stderr)
            return 2

        print(f"[controller] self-test: PUT {group_name!r} -> current node {current!r}")
        try:
            status = self.client.set_group(group_name, current)
            time.sleep(0.3)
            _, group_after, _ = self.client.find_group(group_name)
        except MihomoError as exc:
            print(f"[controller] self-test failed: {exc}", file=sys.stderr)
            return 1

        after = group_after.get("now")
        ok = after == current
        print(f"[controller] HTTP {status}; now={after!r}; ok={ok}")
        return 0 if ok else 1


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="P2 OpenChamber/OpenCode network resilience controller (dry-run by default)."
    )
    parser.add_argument("--events-dir", type=Path, default=default_events_dir(), help="P1 JSONL directory")
    parser.add_argument("--state", type=Path, default=default_state_path(), help="Controller state file")
    parser.add_argument("--api", default=os.environ.get("MIHOMO_API", DEFAULT_API), help="mihomo API base")
    parser.add_argument("--secret", default=os.environ.get("MIHOMO_SECRET", ""), help="mihomo API secret")
    parser.add_argument("--clash-config", type=Path, default=default_clash_config(), help="Clash Verge config for secret")
    parser.add_argument("--group", default=DEFAULT_GROUP_HINT, help="Selector group name or substring")
    parser.add_argument("--interval", type=float, default=1.0, help="Tail poll interval (s)")
    parser.add_argument("--once", action="store_true", help="Process available events and exit")
    parser.add_argument("--follow", action="store_true", help="Follow events (default)")
    parser.add_argument("--from-start", action="store_true", help="Start at the beginning of existing JSONL files")
    parser.add_argument(
        "--allow-backfill-actions",
        action="store_true",
        help="Allow --apply to act on historical events when --from-start is used",
    )
    parser.add_argument("--apply", action="store_true", help="Actually switch the mihomo group")
    parser.add_argument("--min-switch-interval", type=float, default=120.0, help="Seconds between any switches")
    parser.add_argument("--class-cooldown", type=float, default=300.0, help="Seconds between switches for the same class")
    parser.add_argument("--max-per-hour", type=int, default=3, help="Maximum switch attempts per hour")
    parser.add_argument("--max-per-15min", type=int, default=2, help="Maximum switch attempts per 15 minutes")
    parser.add_argument("--healthcheck", dest="no_healthcheck", action="store_false", help="Probe candidates (default)")
    parser.add_argument("--no-healthcheck", dest="no_healthcheck", action="store_true", help="Pick the first candidate blindly")
    parser.add_argument("--health-url", default=DEFAULT_HEALTH_URL, help="Delay probe URL")
    parser.add_argument("--health-timeout", type=int, default=5000, help="Delay probe timeout (ms)")
    parser.add_argument("--max-delay", type=int, default=5000, help="Maximum acceptable delay (ms)")
    parser.add_argument("--health-workers", type=int, default=4, help="Parallel delay probes")
    parser.add_argument(
        "--node-scores",
        type=Path,
        default=default_events_dir() / "node-scores.json",
        help="Measured per-node success rates written by probe_matrix.py",
    )
    parser.add_argument("--max-candidates", type=int, default=6, help="Candidates per attempt")
    parser.add_argument("--allow-direct", action="store_true", help="Allow DIRECT as a candidate")
    parser.add_argument("--restore-on-exit", action="store_true", help="Restore the original node on Ctrl+C")
    parser.add_argument("--list-candidates", action="store_true", help="Print group/candidates and exit")
    parser.add_argument("--self-test", action="store_true", help="PUT the current node back and verify, then exit")
    parser.add_argument("--verbose", action="store_true", help="Print stack traces on unexpected errors")
    parser.set_defaults(no_healthcheck=False)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    args = build_parser().parse_args(argv)

    if args.apply and args.from_start and not args.allow_backfill_actions:
        print(
            "[controller] refusing --apply with --from-start without --allow-backfill-actions; "
            "forcing dry-run so historical errors do not trigger live switches.",
            file=sys.stderr,
        )
        args.apply = False

    secret = args.secret or os.environ.get("MIHOMO_SECRET", "")
    if not secret:
        secret = read_secret_from_clash_config(args.clash_config)
    if not secret:
        print(
            f"[controller] warning: no mihomo secret found at {args.clash_config}; "
            "pass --secret or set MIHOMO_SECRET if the API returns 401.",
            file=sys.stderr,
        )

    client = MihomoClient(args.api, secret, timeout=10.0)
    state = load_state(args.state)
    controller = Controller(args, client, state, args.state)

    if args.list_candidates:
        return controller.list_candidates()

    if args.self_test:
        return controller.self_test()

    if args.apply:
        mode = "APPLY"
    else:
        mode = "DRY-RUN"
    print(f"[controller] mode={mode} api={args.api} group~={args.group!r} state={args.state}", flush=True)
    print(f"[controller] events={args.events_dir}", flush=True)

    tailer = EventTailer(args.events_dir, state, args.from_start)
    tailer.bootstrap()
    controller.save()

    try:
        if args.once:
            events = tailer.poll()
            if not events:
                print("[controller] no new events")
            for event in events:
                controller.handle(event)
            controller.save()
            print(f"[controller] processed={len(events)} actions={controller.action_count}")
            return 0

        print("[controller] following; Ctrl+C to stop.", flush=True)
        while True:
            events = tailer.poll()
            for event in events:
                controller.handle(event)
                controller.save()
            if not events:
                time.sleep(max(0.2, args.interval))
    except KeyboardInterrupt:
        print("\n[controller] stopped", file=sys.stderr)
    finally:
        if args.restore_on_exit and args.apply and state.get("changed"):
            group = state.get("original_group")
            node = state.get("original_node")
            if group and node:
                try:
                    client.set_group(str(group), str(node))
                    print(f"[controller] restored {group} -> {node}", file=sys.stderr)
                except MihomoError as exc:
                    print(f"[controller] restore failed: {exc}", file=sys.stderr)
        controller.save()

    if controller.stats:
        print("[controller] classes:", dict(controller.stats), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
