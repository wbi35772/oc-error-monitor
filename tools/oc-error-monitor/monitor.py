#!/usr/bin/env python3
"""Read-only error monitor for OpenChamber / OpenCode.

What it does:
  - Polls the OpenCode SQLite database (read-only) for new assistant errors.
  - Tails OpenChamber's main.log for session-assist / small-model errors.
  - Classifies each error and writes a JSONL audit record.
  - Prints a compact line to the console.

What it does NOT do:
  - It never writes to the OpenCode database or OpenChamber config.
  - It never calls the Clash/mihomo API.
  - It never retries a request or switches proxies/models.

Usage:
  python monitor.py --once --backfill 30
  python monitor.py --follow
  python monitor.py --follow --interval 2

Exit with Ctrl+C.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def default_db_path() -> Path:
    return Path.home() / ".local" / "share" / "opencode" / "opencode.db"


def default_log_path() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "OpenChamber" / "logs" / "main.log"
    return Path.home() / "AppData" / "Roaming" / "OpenChamber" / "logs" / "main.log"


def default_out_dir() -> Path:
    # tools/oc-error-monitor/monitor.py -> workspace/logs/resilience
    return Path(__file__).resolve().parents[2] / "logs" / "resilience"


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Classification:
    error_class: str
    action: str
    retryable: bool


# Ordered checks: first match wins.
def classify(text: str, error_type: str = "", status: int = 0) -> Classification:
    t = f"{text} {error_type}".lower()

    # The IP-reputation body also carries FreeTierError, so this must be
    # checked before the generic free-tier token.
    #
    # Measured 2026-10-20 (probe_matrix.py): this error is *intermittent*,
    # not a deterministic per-IP ban. On one fixed node and one fixed model,
    # 3/8 attempts succeeded; across nodes the success rate ranged 25%-75%.
    # The upstream rotates provider IPs, so a retry on the same node often
    # succeeds. It is therefore retryable, and a node switch only helps
    # because a different exit has a different upstream mix.
    if "abusive traffic from this ip" in t:
        return Classification(
            "AUTH_IP_REPUTATION",
            "retry_then_switch_node_if_persistent",
            True,
        )

    if "only be used from within opencode" in t or "freetiererror" in t:
        return Classification(
            "AUTH_FREE_TIER",
            "do_not_retry_switch_provider_or_use_opencode",
            False,
        )

    if (
        "model unavailable" in t
        or "small-model-unavailable" in t
        or "eac/our-free-model" in t
        or "127.0.0.1:8326" in t
        or "localhost:8326" in t
        or "127.0.0.1:8080" in t
        or "localhost:8080" in t
    ):
        return Classification(
            "LOCAL_RELAY",
            "start_or_switch_local_relay",
            True,
        )

    if status == 429 or "rate limit" in t or "too many requests" in t:
        return Classification("RATE_LIMIT", "backoff_and_retry", True)

    if error_type == "provider.auth" or status in (401, 403):
        return Classification("AUTH_CREDENTIAL", "refresh_credentials", False)

    if error_type == "provider.internal" or 500 <= status < 600:
        return Classification("UPSTREAM_5XX", "retry_or_failover", True)

    if "enotfound" in t or "eai_again" in t or "getaddrinfo" in t:
        return Classification("NET_DNS", "check_dns_then_retry", True)

    if "econnrefused" in t or "ehostunreach" in t or "enetunreach" in t:
        return Classification("NET_CONNECT", "check_proxy_then_retry", True)

    if (
        "econnreset" in t
        or "socket connection was closed" in t
        or "terminated" in t
        or "fetch failed" in t
        or "stream" in t and "error" in t
    ):
        # A reset after HTTP 200 is a stream drop; before/without a response it
        # is a connect failure.
        if status == 200 or "reading the response" in t:
            return Classification("NET_STREAM", "retry_once_then_switch_node", True)
        return Classification("NET_CONNECT", "check_proxy_then_retry", True)

    if "etimedout" in t or "timeout" in t or "timed out" in t or "aborterror" in t:
        return Classification("NET_TIMEOUT", "retry_with_longer_timeout", True)

    if "certificate" in t or "tls" in t or "handshake" in t:
        return Classification("NET_TLS", "check_tls_and_proxy", False)

    if error_type == "aborted" or "step interrupted" in t:
        return Classification("ABORTED", "none", False)

    if status in (400, 422) or "invalid request" in t or "context length" in t:
        return Classification("CLIENT_REQUEST", "adjust_request", False)

    return Classification("UNKNOWN", "record_and_notify", False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def iso_from_ms(ms: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc).astimezone().isoformat(
            timespec="milliseconds"
        )
    except Exception:
        return datetime.now().astimezone().isoformat(timespec="milliseconds")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def safe_int(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


def compact(value: str, limit: int) -> str:
    value = " ".join(value.split())
    return value if len(value) <= limit else value[:limit] + "…"


def parse_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except Exception:
        return value


@dataclass
class Event:
    ts: str
    source: str
    session_id: str | None
    session_title: str | None
    provider: str | None
    model: str | None
    error_class: str
    action: str
    retryable: bool
    status: int
    error_type: str
    message: str
    raw_id: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "source": self.source,
            "session_id": self.session_id,
            "session_title": self.session_title,
            "provider": self.provider,
            "model": self.model,
            "error_class": self.error_class,
            "action": self.action,
            "retryable": self.retryable,
            "status": self.status,
            "error_type": self.error_type,
            "message": self.message,
            "raw_id": self.raw_id,
        }


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------

class Monitor:
    def __init__(
        self,
        db_path: Path,
        log_path: Path,
        out_dir: Path,
        interval: float = 2.0,
        tail_log: bool = True,
    ) -> None:
        self.db_path = db_path
        self.log_path = log_path
        self.out_dir = out_dir
        self.interval = interval
        self.tail_log = tail_log

        self.con: sqlite3.Connection | None = None
        self.last_rowid: int = 0
        self.log_pos: int = 0
        self.log_partial: str = ""
        self.session_cache: dict[str, tuple[str | None, str | None, str | None]] = {}
        self.seen: set[tuple[str, str]] = set()
        self.out_dir.mkdir(parents=True, exist_ok=True)

    # -- DB ----------------------------------------------------------------

    def open_db(self) -> sqlite3.Connection:
        if self.con is not None:
            return self.con
        uri = f"file:{self.db_path.as_posix()}?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=5.0)
        con.execute("PRAGMA query_only = ON")
        con.execute("PRAGMA busy_timeout = 5000")
        self.con = con
        return con

    def max_rowid(self) -> int:
        con = self.open_db()
        row = con.execute("SELECT COALESCE(MAX(rowid), 0) FROM session_message").fetchone()
        return int(row[0]) if row else 0

    def session_info(self, session_id: str | None) -> tuple[str | None, str | None, str | None]:
        if not session_id:
            return None, None, None
        if session_id in self.session_cache:
            return self.session_cache[session_id]
        con = self.open_db()
        try:
            row = con.execute(
                "SELECT title, model, idle_outcome FROM session_v2 WHERE id = ?",
                (session_id,),
            ).fetchone()
        except sqlite3.Error:
            row = None
        if not row:
            result = (None, None, None)
        else:
            title, model_raw, outcome = row
            model_obj = parse_json(model_raw) if model_raw else {}
            provider = None
            model = None
            if isinstance(model_obj, dict):
                provider = model_obj.get("providerID")
                model = model_obj.get("id")
            result = (title, provider, model)
        self.session_cache[session_id] = result
        return result

    def emit(self, event: Event) -> None:
        key = (event.source, event.raw_id)
        if key in self.seen:
            return
        self.seen.add(key)
        if len(self.seen) > 50_000:
            self.seen.clear()

        out_file = self.out_dir / f"events-{datetime.now().astimezone().date().isoformat()}.jsonl"
        with out_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event.as_dict(), ensure_ascii=False) + "\n")

        who = f"{event.provider or '?'}/{event.model or '?'}"
        sid = event.session_id or "-"
        print(
            f"[{event.ts}] {event.error_class:<18} {sid} {who} | "
            f"{event.action} | {compact(event.message, 180)}",
            flush=True,
        )

    def process_message_row(self, row: tuple[Any, ...]) -> None:
        rowid, msg_id, session_id, msg_type, seq, created, data_raw = row
        obj = parse_json(data_raw)
        if not isinstance(obj, dict):
            return

        # Some message shapes nest the payload under `data`.
        candidates: list[dict[str, Any]] = [obj]
        nested = obj.get("data")
        if isinstance(nested, dict):
            candidates.append(nested)

        error: Any = None
        payload: dict[str, Any] = obj
        for candidate in candidates:
            if candidate.get("error"):
                error = candidate.get("error")
                payload = candidate
                break

        if error is None:
            return

        err = error if isinstance(error, dict) else {"message": as_text(error)}
        error_type = as_text(err.get("type"))
        status = safe_int(err.get("status"))
        message = as_text(err.get("message"))
        response = err.get("response")
        body = ""
        if isinstance(response, dict):
            body = as_text(response.get("body"))
        text = " | ".join(part for part in (message, body) if part)

        cls = classify(text, error_type, status)

        title, provider, model = self.session_info(session_id)
        if not provider or not model:
            model_obj = payload.get("model")
            if isinstance(model_obj, dict):
                provider = provider or model_obj.get("providerID")
                model = model or model_obj.get("id")

        event = Event(
            ts=iso_from_ms(created),
            source="opencode.db",
            session_id=session_id,
            session_title=title,
            provider=provider,
            model=model,
            error_class=cls.error_class,
            action=cls.action,
            retryable=cls.retryable,
            status=status,
            error_type=error_type,
            message=compact(text, 4000),
            raw_id=f"db:{rowid}:{msg_id}",
        )
        self.emit(event)

    def poll_db(self, backfill: int = 0) -> None:
        con = self.open_db()
        try:
            if backfill > 0:
                rows = con.execute(
                    """
                    SELECT rowid, id, session_id, type, seq, time_created, data
                    FROM session_message
                    ORDER BY rowid DESC
                    LIMIT ?
                    """,
                    (backfill,),
                ).fetchall()
                rows = list(reversed(rows))
                self.last_rowid = max((int(r[0]) for r in rows), default=self.last_rowid)
            else:
                rows = con.execute(
                    """
                    SELECT rowid, id, session_id, type, seq, time_created, data
                    FROM session_message
                    WHERE rowid > ?
                    ORDER BY rowid ASC
                    """,
                    (self.last_rowid,),
                ).fetchall()
                if rows:
                    self.last_rowid = int(rows[-1][0])

            for row in rows:
                try:
                    self.process_message_row(row)
                except Exception as exc:
                    print(f"[monitor] skipped message row: {exc}", file=sys.stderr)
        except sqlite3.Error as exc:
            print(f"[monitor] db read failed: {exc}", file=sys.stderr)

    # -- main.log ----------------------------------------------------------

    def init_log_cursor(self, backfill: int = 0) -> None:
        try:
            size = self.log_path.stat().st_size
        except OSError:
            self.log_pos = 0
            return
        if backfill > 0:
            self.log_pos = max(0, size - 128 * 1024)
        else:
            self.log_pos = size

    def process_log_line(self, line: str) -> None:
        if not line:
            return
        low = line.lower()
        if "session-assist" not in low and "small-model" not in low:
            return
        if "generation failed" not in low and "small-model-unavailable" not in low and "model unavailable" not in low:
            return

        cls = classify(line, "", 0)
        ts_match = re.match(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\]", line)
        ts = ts_match.group(1) if ts_match else now_iso()

        session_match = re.search(r"(ses_[A-Za-z0-9]+)", line)
        session_id = session_match.group(1) if session_match else None

        # Try to pull a provider/model pair out of the line.
        provider = None
        model = None
        model_match = re.search(r"([a-zA-Z0-9_.-]+)/([a-zA-Z0-9_.:/+-]+)", line)
        if model_match:
            provider, model = model_match.group(1), model_match.group(2)

        event = Event(
            ts=ts,
            source="openchamber.main.log",
            session_id=session_id,
            session_title=None,
            provider=provider,
            model=model,
            error_class=cls.error_class,
            action=cls.action,
            retryable=cls.retryable,
            status=0,
            error_type="",
            message=compact(line, 4000),
            raw_id="log:" + hashlib.sha1(line.encode("utf-8", "replace")).hexdigest()[:16],
        )
        self.emit(event)

    def poll_log(self, backfill: int = 0) -> None:
        if not self.tail_log:
            return
        try:
            size = self.log_path.stat().st_size
        except OSError:
            return
        if size < self.log_pos:
            # Truncated/rotated.
            self.log_pos = 0
            self.log_partial = ""

        try:
            with self.log_path.open("rb") as fh:
                fh.seek(self.log_pos)
                chunk = fh.read()
                self.log_pos = fh.tell()
        except OSError as exc:
            print(f"[monitor] log read failed: {exc}", file=sys.stderr)
            return

        if not chunk:
            return

        text = self.log_partial + chunk.decode("utf-8", "replace")
        lines = text.splitlines(keepends=False)
        if text and not text.endswith(("\n", "\r")):
            self.log_partial = lines.pop() if lines else text
        else:
            self.log_partial = ""

        if backfill > 0:
            lines = lines[-backfill:]

        for line in lines:
            try:
                self.process_log_line(line)
            except Exception as exc:
                print(f"[monitor] skipped log line: {exc}", file=sys.stderr)

    # -- lifecycle ---------------------------------------------------------

    def run_once(self, backfill: int = 0) -> None:
        if backfill > 0:
            self.last_rowid = max(0, self.max_rowid() - backfill)
        else:
            self.last_rowid = self.max_rowid()
        self.poll_db(backfill=backfill)

        self.init_log_cursor(backfill=backfill)
        # For a one-shot backfill, read the tail even though the cursor is at
        # the end: rewind a bounded amount and process it.
        if backfill > 0:
            try:
                size = self.log_path.stat().st_size
                self.log_pos = max(0, size - 128 * 1024)
            except OSError:
                self.log_pos = 0
        self.poll_log(backfill=backfill)

    def run_forever(self, backfill: int = 0) -> None:
        self.last_rowid = max(0, self.max_rowid() - backfill) if backfill > 0 else self.max_rowid()
        self.init_log_cursor(backfill=backfill)
        if backfill > 0:
            self.poll_db(backfill=backfill)
            self.poll_log(backfill=backfill)

        print(
            f"[monitor] watching db={self.db_path} log={self.log_path} out={self.out_dir}",
            flush=True,
        )
        print("[monitor] read-only; no Clash/model actions. Ctrl+C to stop.", flush=True)

        while True:
            self.poll_db()
            self.poll_log()
            time.sleep(self.interval)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only OpenChamber/OpenCode error monitor and classifier."
    )
    parser.add_argument("--db", type=Path, default=default_db_path(), help="OpenCode SQLite DB")
    parser.add_argument("--log", type=Path, default=default_log_path(), help="OpenChamber main.log")
    parser.add_argument("--out-dir", type=Path, default=default_out_dir(), help="JSONL output directory")
    parser.add_argument("--interval", type=float, default=2.0, help="Poll interval in seconds")
    parser.add_argument("--once", action="store_true", help="Process a batch and exit")
    parser.add_argument("--follow", action="store_true", help="Keep watching (default)")
    parser.add_argument("--backfill", type=int, default=0, help="Process the last N records/lines on start")
    parser.add_argument("--no-log-tail", action="store_true", help="Do not read main.log")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    args = build_parser().parse_args(argv)

    if not args.db.exists():
        print(f"[monitor] database not found: {args.db}", file=sys.stderr)
        return 2

    monitor = Monitor(
        db_path=args.db,
        log_path=args.log,
        out_dir=args.out_dir,
        interval=max(0.5, args.interval),
        tail_log=not args.no_log_tail,
    )

    try:
        if args.once:
            monitor.run_once(backfill=max(0, args.backfill))
        else:
            monitor.run_forever(backfill=max(0, args.backfill))
    except KeyboardInterrupt:
        print("\n[monitor] stopped", file=sys.stderr)
        return 0
    finally:
        if monitor.con is not None:
            monitor.con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
