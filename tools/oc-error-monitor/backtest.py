#!/usr/bin/env python3
"""Backtest the resilience policies against historical OpenCode data.

Why this exists
---------------
Turning `--apply` on needs evidence, and waiting a week for dry-run
telemetry is slow. The errors are already on disk: `opencode.db` holds every
assistant message of the last few days, with `error` set on the failures and
`finish: "stop"` on the successes. So the policies can be replayed against
real history instead of waiting for new data.

The metric
----------
For each error, look at the **very next attempt** in that session:

- next attempt succeeded -> the error was transient; intervening was a
  **false positive** (unnecessary node/model switch).
- next attempt failed too -> the error persisted; intervening was
  **justified**.
- no further attempt -> the session stalled outright. This is the worst
  outcome, and it is the case an intervention could have prevented.

That is the number needed before enabling `--apply`.

Usage:
  python backtest.py
  python backtest.py --json <out.json>
  python backtest.py --sweep
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from monitor import classify  # noqa: E402

# Import the live policy from the controllers so this backtest cannot drift
# away from what the tools actually do.
import controller  # noqa: E402
import session_failover  # noqa: E402

# Classes the controller may act on immediately.
SWITCH_CLASSES = controller.SWITCH_CLASSES
# Classes that need a sustained burst before acting.
PERSISTENT_CLASSES = controller.PERSISTENT_CLASSES
# Classes owned by the per-session layer instead of the node switch.
P3_OWNED_CLASSES = controller.P3_OWNED_CLASSES
# The live burst threshold for persistent classes.
PERSISTENT_MIN_FAILURES = controller.PERSISTENT_MIN_FAILURES


def load_timeline(db_path: Path) -> list[dict[str, Any]]:
    """Read every assistant message as a timeline entry, plus user turns."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT session_id, type, time_created, data FROM session_message "
            "ORDER BY rowid"
        ).fetchall()
    finally:
        con.close()

    entries: list[dict[str, Any]] = []
    for session_id, mtype, created, data_raw in rows:
        ts = float(created) / 1000.0
        if mtype == "user":
            entries.append(
                {"ts": ts, "session_id": session_id, "kind": "user", "error_class": None}
            )
            continue
        if mtype != "assistant":
            continue
        try:
            obj = json.loads(data_raw)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        payload = obj.get("data") if isinstance(obj.get("data"), dict) else obj

        model_obj = payload.get("model") or {}
        err = payload.get("error")
        if err:
            eo = err if isinstance(err, dict) else {"message": str(err)}
            error_type = str(eo.get("type") or "")
            try:
                status = int(eo.get("status") or 0)
            except Exception:
                status = 0
            body = ""
            if isinstance(eo.get("response"), dict):
                body = str(eo.get("response").get("body") or "")
            text = " | ".join(p for p in (str(eo.get("message") or ""), body) if p)
            cls = classify(text, error_type, status)
            entries.append(
                {
                    "ts": ts,
                    "session_id": session_id,
                    "kind": "err",
                    "error_class": cls.error_class,
                    "error_type": error_type,
                    "status": status,
                    "provider": model_obj.get("providerID"),
                    "model": model_obj.get("id"),
                    "message": text[:300],
                }
            )
        elif payload.get("finish"):
            entries.append(
                {
                    "ts": ts,
                    "session_id": session_id,
                    "kind": "ok",
                    "error_class": None,
                    "error_type": "",
                    "status": 0,
                    "provider": model_obj.get("providerID"),
                    "model": model_obj.get("id"),
                    "message": "",
                }
            )
    entries.sort(key=lambda e: e["ts"])
    return entries


def build_sessions(entries: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    sessions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in entries:
        sessions[e["session_id"]].append(e)
    return sessions


def next_outcome(
    seq: list[dict[str, Any]], index: int
) -> tuple[str, float | None]:
    """Outcome of the next attempt after seq[index], and how long it took.

    Returns one of: "ok", "err", "none" plus the seconds elapsed.
    """
    ts = seq[index]["ts"]
    for e in seq[index + 1:]:
        if e["kind"] == "user":
            continue
        return e["kind"], e["ts"] - ts
    return "none", None


def replay(
    sessions: dict[str, list[dict[str, Any]]],
    policy: dict[str, int],
) -> list[dict[str, Any]]:
    """Replay a policy over the history and label each error."""
    records: list[dict[str, Any]] = []
    for session_id, seq in sessions.items():
        # Per-class burst counters, mirroring the controller's window logic.
        stamps: dict[str, list[float]] = defaultdict(list)
        for i, e in enumerate(seq):
            if e["kind"] != "err":
                continue
            cls = e["error_class"]
            stamps[cls] = [t for t in stamps[cls] if e["ts"] - t < policy["window"]]
            stamps[cls].append(e["ts"])

            burst = len(stamps[cls])
            immediate = cls in SWITCH_CLASSES
            sustained = cls in PERSISTENT_CLASSES and burst >= policy["min_failures"]
            p3_owned = cls in P3_OWNED_CLASSES
            would_act = (immediate or sustained) and not p3_owned

            outcome, delta = next_outcome(seq, i)
            records.append(
                {
                    "session_id": session_id,
                    "ts": e["ts"],
                    "error_class": cls,
                    "provider": e.get("provider"),
                    "model": e.get("model"),
                    "burst": burst,
                    "would_act": would_act,
                    "outcome": outcome,
                    "seconds_to_next": delta,
                }
            )
    return records


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    acted = [r for r in records if r["would_act"]]
    # False positive: the next attempt succeeded anyway, so the action was moot.
    fp = sum(1 for r in acted if r["outcome"] == "ok")
    # Justified: the next attempt failed too, or the session died outright.
    justified = sum(1 for r in acted if r["outcome"] in ("err", "none"))
    stalled = sum(1 for r in acted if r["outcome"] == "none")
    return {
        "errors": len(records),
        "would_act": len(acted),
        "false_positives": fp,
        "false_positive_rate": round(fp / len(acted), 4) if acted else 0.0,
        "justified": justified,
        "stalled_sessions": stalled,
    }


def per_class(records: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    by_cls: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        by_cls[r["error_class"]].append(r)
    for cls, rows in by_cls.items():
        acted = [r for r in rows if r["would_act"]]
        fp = sum(1 for r in acted if r["outcome"] == "ok")
        out[cls] = {
            "errors": len(rows),
            "would_act": len(acted),
            "false_positives": fp,
            "false_positive_rate": round(fp / len(acted), 4) if acted else 0.0,
            "stalled": sum(1 for r in rows if r["outcome"] == "none"),
            "next_ok": sum(1 for r in rows if r["outcome"] == "ok"),
            "next_err": sum(1 for r in rows if r["outcome"] == "err"),
        }
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Backtest resilience policies on history.")
    p.add_argument(
        "--db",
        type=Path,
        default=Path.home() / ".local/share/opencode/opencode.db",
    )
    p.add_argument(
        "--min-failures",
        type=int,
        default=PERSISTENT_MIN_FAILURES,
        help="Burst needed for persistent classes (default: the live policy)",
    )
    p.add_argument("--window", type=float, default=600.0, help="Burst window (seconds)")
    p.add_argument("--json", type=Path, default=None)
    p.add_argument("--sweep", action="store_true", help="Sweep thresholds and report")
    args = p.parse_args(argv)

    if not args.db.exists():
        print(f"db not found: {args.db}", file=sys.stderr)
        return 1

    entries = load_timeline(args.db)
    sessions = build_sessions(entries)
    policy = {"min_failures": args.min_failures, "window": args.window}
    records = replay(sessions, policy)

    fmt = lambda t: datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")
    print(f"db       : {args.db}")
    print(f"span     : {fmt(entries[0]['ts'])} -> {fmt(entries[-1]['ts'])}")
    print(f"sessions : {len(sessions)}   errors: {len(records)}")
    print(f"policy   : burst>={args.min_failures} in {args.window:.0f}s for {sorted(PERSISTENT_CLASSES)}")
    print()

    print(f"{'class':22} {'errors':>7} {'act':>5} {'FP':>5} {'FP rate':>8} {'stalled':>8}")
    print("-" * 62)
    pc = per_class(records)
    for cls, s in sorted(pc.items(), key=lambda kv: -kv[1]["errors"]):
        print(
            f"{cls:22} {s['errors']:>7} {s['would_act']:>5} {s['false_positives']:>5} "
            f"{s['false_positive_rate']:>7.0%} {s['stalled']:>8}"
        )
    print()

    tot = summarize(records)
    print(f"would act on        : {tot['would_act']} of {tot['errors']} errors")
    print(f"  false positives   : {tot['false_positives']}  ({tot['false_positive_rate']:.0%})")
    print(f"  justified         : {tot['justified']}")
    print(f"  stalled sessions  : {tot['stalled_sessions']}  (no further attempt at all)")
    print()
    p3_rows = [r for r in records if r["error_class"] in P3_OWNED_CLASSES]
    p2_rows = [r for r in records if r["error_class"] not in P3_OWNED_CLASSES]
    # P3 acts per session on the first failure, so every P3-owned error
    # counts as an action for that layer.
    p3_act = len(p3_rows)
    p3_fp = sum(1 for r in p3_rows if r["outcome"] == "ok")
    p3_stalled = sum(1 for r in p3_rows if r["outcome"] == "none")
    p2 = summarize(p2_rows)
    print(
        f"P2 node switch     : {p2['would_act']:>4} actions, {p2['false_positives']:>3} FP "
        f"({p2['false_positive_rate']:>4.0%}), {p2['stalled_sessions']:>3} stalled sessions"
    )
    print(
        f"P3 model switch    : {p3_act:>4} actions, {p3_fp:>3} FP "
        f"({p3_fp / p3_act if p3_act else 0:>4.0%}), {p3_stalled:>3} stalled sessions "
        f"(one session each, not the whole group)"
    )

    if args.sweep:
        print()
        print("sweep: persistent-class burst threshold")
        print(f"{'min':>4} {'act':>5} {'FP':>5} {'FP rate':>8} {'stalled':>8}")
        for m in (1, 2, 3, 4, 5):
            recs = replay(sessions, {"min_failures": m, "window": args.window})
            s = summarize(recs)
            print(
                f"{m:>4} {s['would_act']:>5} {s['false_positives']:>5} "
                f"{s['false_positive_rate']:>7.0%} {s['stalled_sessions']:>8}"
            )
        print()
        print("sweep: which classes are worth acting on at all")
        for cls in sorted(pc, key=lambda c: -pc[c]["errors"]):
            rows = [r for r in records if r["error_class"] == cls]
            act_all = rows
            fp_all = sum(1 for r in act_all if r["outcome"] == "ok")
            act_persist = [r for r in rows if r["outcome"] in ("err", "none")]
            print(
                f"  {cls:22} n={len(rows):3}  "
                f"act-on-all: FP={fp_all/len(rows):.0%}   "
                f"act-only-when-persistent: {len(act_persist)/len(rows):.0%} of errors"
            )

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(
                {
                    "span": [entries[0]["ts"], entries[-1]["ts"]],
                    "sessions": len(sessions),
                    "policy": policy,
                    "overall": tot,
                    "per_class": pc,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"report: {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
