"""An OpenAI-compatible endpoint whose backend is the real OpenCode CLI.

Why this exists
---------------
The free tiers are gated to the real client: the gateway rejects a request that
does not look like it came from OpenCode ("free tier can only be used from
within OpenCode"), and OpenRouter's free models want credentials. A third party
tool - a README generator, a doc writer - therefore cannot use them directly.

This shim closes that gap in the only legitimate way: the request is executed
by the actual OpenCode CLI, which is the client the free tier is meant for.

    opencode run --model <model> --format json "<prompt>"

Endpoints (OpenAI shaped):

    POST /v1/chat/completions   {model, messages, stream?}  ->  chat completion
    GET  /v1/models                                        ->  the models you may use
    GET  /__health

It is deliberately simple: one CLI call per request, no embeddings, no images.
That is enough for document generation, and it needs no API key and no account.
"""
import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CLI = os.path.join(
    os.environ.get("LOCALAPPDATA", ""),
    "Programs", "@openchamberelectron", "resources", "opencode-cli", "opencode.exe",
)
# Models this shim offers. They are the ones the retry proxy points at, so the
# 403 the free tier throws is rescued by the proxy instead of reaching us.
MODELS = [
    "opencode/big-pickle",
    "opencode/deepseek-v4.1-flash-free",
    "opencode/minimax-m3.1-flash-free",
    "opencode/beam-free",
    "opencode/mimo-v2.6-flash-free",
    "opencode/nemotron-3.5-lightning-free",
]
CLI = DEFAULT_CLI if os.path.exists(DEFAULT_CLI) else "opencode"


def _read_version():
    """Same rule as the proxy: VERSION is found by walking up, so this file
    reports the same build whether it runs from the repository or from a
    staged directory."""
    here = HERE
    for _ in range(5):
        try:
            with open(os.path.join(here, "VERSION"), "r", encoding="utf-8") as f:
                first = f.read().strip().splitlines()[0].strip()
                if first:
                    return first
        except (OSError, IndexError):
            pass
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    return "0.0.0-dev"


VERSION = _read_version()
CALL_TIMEOUT = float(os.environ.get("CLI_LLM_TIMEOUT", "300"))
CONCURRENCY = int(os.environ.get("CLI_LLM_CONCURRENCY", "3"))

_slots = queue.Queue(maxsize=CONCURRENCY)
_stats = {"requests": 0, "ok": 0, "failed": 0, "total_ms": 0, "failed_over": 0}
_lock = threading.Lock()


def _bump(key, n=1):
    with _lock:
        _stats[key] = _stats.get(key, 0) + n


def messages_to_prompt(messages):
    """Flatten the chat messages into one prompt for the CLI."""
    parts = []
    for m in messages or []:
        role = (m.get("role") or "user").lower()
        content = m.get("content")
        if isinstance(content, list):
            content = "\n".join(
                c.get("text", "") for c in content if isinstance(c, dict)
            )
        content = content or ""
        if role == "system":
            parts.append(f"[system]\n{content}")
        elif role == "assistant":
            parts.append(f"[assistant]\n{content}")
        else:
            parts.append(content)
    return "\n\n".join(p for p in parts if p.strip())


def run_cli(model, prompt, attempts=2, fallback=("opencode/big-pickle",)):
    """Run the CLI and return the assistant text. The prompt goes through
    stdin: as an argument it dies with WinError 206 once a doc generator
    sends a prompt of a few thousand characters.

    The free tier is flaky: a long prompt can come back as a stream without
    a finish_reason, and opencode reports that as provider.invalid-output.
    So a failed call is retried, and then a different model is tried."""
    tried = []
    errors = []
    chain = [model] + [m for m in fallback if m != model]
    for m in chain:
        for _ in range(attempts):
            tried.append(m)
            try:
                text, elapsed = _run_once(m, prompt)
                if text.strip():
                    return text, elapsed, tried
                errors.append(f"{m}: empty response")
            except subprocess.TimeoutExpired:
                errors.append(f"{m}: timeout after {CALL_TIMEOUT:.0f}s")
            except Exception as e:
                errors.append(f"{m}: {e}")
    raise RuntimeError("; ".join(errors[-3:]) or "all models failed")


def _run_once(model, prompt):
    args = [CLI, "run", "--model", model, "--format", "json"]
    started = time.time()
    proc = subprocess.run(
        args, input=prompt, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=CALL_TIMEOUT,
    )
    elapsed = int((time.time() - started) * 1000)
    text = ""
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "text":
            part = ev.get("part") or {}
            if part.get("text"):
                text = part["text"]
        if ev.get("type") == "error":
            err = ev.get("error") or {}
            raise RuntimeError(f"{err.get('type','error')}: {err.get('message','?')}")
    if not text and proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "cli failed").strip()[:400])
    return text, elapsed


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, obj, status=200):
        raw = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            self._json({
                "object": "list",
                "data": [
                    {"id": m, "object": "model", "owned_by": "opencode"}
                    for m in MODELS
                ],
            })
            return
        if self.path.startswith("/__health"):
            self._json({"ok": True, "service": "cli llm server", "version": VERSION,
                        "cli": CLI, "models": len(MODELS),
                        "stats": dict(_stats)})
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self.path.startswith("/v1/chat/completions"):
            self._json({"error": "not found"}, 404)
            return
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            self._json({"error": {"message": "bad json"}}, 400)
            return

        model = body.get("model") or MODELS[0]
        if "/" not in model:
            model = f"opencode/{model}"
        prompt = messages_to_prompt(body.get("messages"))
        stream = bool(body.get("stream"))

        _bump("requests")
        try:
            _slots.put(True, timeout=30)
        except queue.Full:
            self._json({"error": {"message": "server busy"}}, 503)
            return
        try:
            text, elapsed, tried = run_cli(model, prompt)
        except subprocess.TimeoutExpired:
            _bump("failed")
            self._json({"error": {"message": f"cli timed out after {CALL_TIMEOUT:.0f}s"}}, 504)
            return
        except Exception as e:
            _bump("failed")
            self._json({"error": {"message": str(e)}}, 502)
            return
        finally:
            try:
                _slots.get_nowait()
            except queue.Empty:
                pass

        _bump("ok")
        _bump("total_ms", elapsed)
        if len(tried) > 1:
            _bump("failed_over", len(tried) - 1)
        out = {
            "id": f"chatcmpl-cli-{int(time.time() * 1000)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            "x_cli_attempts": tried,
        }
        if not stream:
            self._json(out)
            return

        # Streamed shape, so OpenAI clients that ask for a stream work.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        chunk = dict(out)
        chunk["object"] = "chat.completion.chunk"
        chunk["choices"] = [{
            "index": 0,
            "delta": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }]
        self.wfile.write(b"data: " + json.dumps(chunk, ensure_ascii=False).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8890)
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print(f"cli llm server  http://{args.host}:{args.port}/v1  (cli: {CLI})")
    print(f"models: {', '.join(MODELS)}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
