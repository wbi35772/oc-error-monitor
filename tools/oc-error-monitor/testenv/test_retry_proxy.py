"""Offline test suite for retry_proxy.py.

Everything runs inside this project:
  * the upstream is mock_gateway.py on loopback
  * the proxy runs from tools/oc-error-monitor/testenv/_run/ with its own logs
  * no Clash Verge, no real gateway, no live network

Run:  python tools/oc-error-monitor/testenv/test_retry_proxy.py
"""
import http.client
import json
import os
import shutil
import socket
import subprocess
import threading
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent
PROXY_SRC = TOOLS / "retry_proxy.py"
RUN_DIR = HERE / "_run"

sys.path.insert(0, str(HERE))
import mock_gateway  # noqa: E402

RESULTS = []


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def dead_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail and not cond else ""))
    return bool(cond)


def start_proxy(port, upstream, attempts="4", proxy_upstream="", timeout="10", handover=False, ports=None):
    env = os.environ.copy()
    env.update(
        {
            "RETRY_PROXY_HOST": "127.0.0.1",
            "RETRY_PROXY_PORT": str(port),
            "RETRY_PROXY_UPSTREAM": upstream,
            "RETRY_PROXY_UPSTREAM_PROXY": proxy_upstream,
            "RETRY_PROXY_ATTEMPTS": attempts,
            "RETRY_PROXY_TIMEOUT": timeout,
            "no_proxy": "*",
            "NO_PROXY": "*",
        }
    )
    if ports:
        env["RETRY_PROXY_PORTS"] = ports
    log = open(RUN_DIR / "proxy.stdout.log", "a", encoding="utf-8")
    args = [sys.executable, str(RUN_DIR / "retry_proxy.py")]
    if handover:
        args.append("--handover")
    return subprocess.Popen(
        args,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        cwd=str(RUN_DIR),
    )


def wait_health(port, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            c.request("GET", "/__health")
            r = c.getresponse()
            if r.status == 200:
                r.read()
                return True
        except Exception:
            time.sleep(0.2)
    return False


def stats(port):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", "/__stats")
    r = c.getresponse()
    data = json.loads(r.read())
    c.close()
    return data


def post(port, body=None, path="/v1/chat/completions", timeout=30):
    payload = json.dumps(body if body is not None else {"model": "mock", "stream": True, "messages": []}).encode()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    conn.request(
        "POST",
        path,
        body=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "opencode/latest/2.0.32/cli",
            "x-opencode-client": "cli",
            "x-opencode-session": "ses_test0000000000000000",
            "x-opencode-project": "0" * 32,
            "Authorization": "Bearer public",
        },
    )
    resp = conn.getresponse()
    data = resp.read()
    ctype = resp.getheader("Content-Type", "")
    conn.close()
    return resp.status, data, ctype


def plan(mock_port, responses, default="ok_sse", delay=None):
    c = http.client.HTTPConnection("127.0.0.1", mock_port, timeout=5)
    payload = {"responses": responses, "default": default}
    if delay is not None:
        payload["delay"] = delay
    c.request("POST", "/__plan", body=json.dumps(payload))
    r = c.getresponse()
    r.read()
    c.close()


def mock_counts(mock_port):
    c = http.client.HTTPConnection("127.0.0.1", mock_port, timeout=5)
    c.request("GET", "/__stats")
    r = c.getresponse()
    d = json.loads(r.read())
    c.close()
    return d["counts"]


def _owns(port, pid):
    """True when the given pid listens on the port."""
    try:
        sys.path.insert(0, str(TOOLS))
        import port_owner

        return any(o.get("pid") == pid for o in port_owner.owners(port))
    except Exception:
        return False


def main():
    if RUN_DIR.exists():
        shutil.rmtree(RUN_DIR, ignore_errors=True)
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy(PROXY_SRC, RUN_DIR / "retry_proxy.py")
    shutil.copy(PROXY_SRC.parent / "ui.html", RUN_DIR / "ui.html")

    mock_srv, _, mock_port = mock_gateway.start(port=0)
    proxy_port = free_port()
    proc = start_proxy(proxy_port, f"http://127.0.0.1:{mock_port}/v1")
    if not wait_health(proxy_port):
        print("FATAL: proxy did not start")
        proc.kill()
        mock_srv.shutdown()
        return 1

    try:
        print("\n== 1. 403 abusive traffic (IP reputation) is retried and rescued ==")
        plan(mock_port, ["ip_rep", "ok_sse"])
        s0 = stats(proxy_port)
        st, body, _ = post(proxy_port)
        s1 = stats(proxy_port)
        check("client gets 200", st == 200, f"status={st} body={body[:120]!r}")
        check("body is the SSE stream", b"finish_reason" in body, repr(body[:120]))
        check("retried_requests +1", s1["retried_requests"] == s0["retried_requests"] + 1)
        check("rescued_requests +1", s1["rescued_requests"] == s0["rescued_requests"] + 1)
        check("exhausted unchanged", s1["exhausted_requests"] == s0["exhausted_requests"])
        check("upstream saw ip_rep then ok_sse", mock_counts(mock_port).get("ip_rep") == 1, str(mock_counts(mock_port)))

        print("\n== 2. identity 403 is passed through, never retried ==")
        plan(mock_port, ["identity"])
        s0 = stats(proxy_port)
        st, body, _ = post(proxy_port)
        s1 = stats(proxy_port)
        check("client gets 403", st == 403, f"status={st}")
        check("no retry happened", s1["attempts"] == s0["attempts"] + 1, f"attempts {s0['attempts']} -> {s1['attempts']}")
        check("exhausted not counted", s1["exhausted_requests"] == s0["exhausted_requests"])

        print("\n== 3. 200 with an empty body is retried ==")
        plan(mock_port, ["empty", "ok_sse"])
        s0 = stats(proxy_port)
        st, body, _ = post(proxy_port)
        s1 = stats(proxy_port)
        check("client gets 200", st == 200, f"status={st}")
        check("empty stream counted", s1["classes"].get("EMPTY_STREAM", 0) >= 1, str(s1["classes"]))
        check("rescued +1", s1["rescued_requests"] == s0["rescued_requests"] + 1)

        print("\n== 4. all attempts fail -> 403 to the client, counted as exhausted ==")
        plan(mock_port, ["ip_rep"] * 6)
        s0 = stats(proxy_port)
        st, body, _ = post(proxy_port)
        s1 = stats(proxy_port)
        check("client gets 403", st == 403, f"status={st}")
        check("exactly 4 attempts", s1["attempts"] == s0["attempts"] + 4, f"attempts {s0['attempts']} -> {s1['attempts']}")
        check("exhausted +1", s1["exhausted_requests"] == s0["exhausted_requests"] + 1)
        check("body carries the upstream error", b"abusive traffic" in body, repr(body[:160]))

        print("\n== 5. 429 is retried ==")
        plan(mock_port, ["rate", "ok_sse"])
        s0 = stats(proxy_port)
        st, body, _ = post(proxy_port)
        s1 = stats(proxy_port)
        check("client gets 200", st == 200, f"status={st}")
        check("rescued +1", s1["rescued_requests"] == s0["rescued_requests"] + 1)

        print("\n== 6. SSE truncated upstream is observed, not retried ==")
        plan(mock_port, ["truncated"])
        s0 = stats(proxy_port)
        st, body, _ = post(proxy_port)
        s1 = stats(proxy_port)
        check("client gets 200 with partial stream", st == 200 and b"half" in body, f"status={st} body={body[:80]!r}")
        check("no real finish_reason in stream", b'"finish_reason":"stop"' not in body)
        check("truncated_streams +1", s1["truncated_streams"] == s0["truncated_streams"] + 1, str(s1.get("truncated_streams")))
        check("not retried", s1["attempts"] == s0["attempts"] + 1)

        print("\n== 7. dead upstream -> 502 with a clear error ==")
        p2 = free_port()
        dead = dead_port()
        proc2 = start_proxy(p2, f"http://127.0.0.1:{dead}/v1", attempts="2", timeout="3")
        if wait_health(p2):
            st, body, _ = post(p2, timeout=30)
            check("client gets 502", st == 502, f"status={st}")
            check("error names the upstream failure", b"ProxyUpstreamError" in body, repr(body[:160]))
        else:
            check("second proxy instance started", False, "no health")
        proc2.kill()

        print("\n== 8. second instance on the same port exits quietly ==")
        proc3 = start_proxy(proxy_port, f"http://127.0.0.1:{mock_port}/v1")
        try:
            rc = proc3.wait(timeout=15)
        except subprocess.TimeoutExpired:
            rc = None
            proc3.kill()
        check("exit code 0", rc == 0, f"rc={rc}")
        check("first instance still serving", wait_health(proxy_port, timeout=5))

        print("\n== 9. dashboard and manual control ==")
        c = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=5)
        c.request("GET", "/__ui")
        r = c.getresponse()
        page = r.read()
        c.close()
        check("/__ui serves the dashboard", r.status == 200 and b"Retry Proxy" in page, f"status={r.status}")

        def ctl(payload):
            cc = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=5)
            cc.request("POST", "/__control", body=json.dumps(payload).encode(),
                       headers={"Content-Type": "application/json"})
            rr = cc.getresponse()
            data = json.loads(rr.read())
            cc.close()
            return rr.status, data

        st, d = ctl({"action": "pause"})
        check("pause accepted", st == 200 and d.get("ok") is True, str(d))
        s_p = stats(proxy_port)
        check("paused flag visible in stats", s_p["settings"]["paused"] is True, str(s_p["settings"]))

        plan(mock_port, ["ip_rep", "ok_sse"])
        a0 = s_p["attempts"]
        st, body, _ = post(proxy_port)
        s_p2 = stats(proxy_port)
        check("paused = pass through, no retry", st == 403, f"status={st}")
        check("paused = exactly one attempt", s_p2["attempts"] == a0 + 1, f"{a0} -> {s_p2['attempts']}")

        ctl({"action": "resume"})
        st, d = ctl({"action": "set", "attempts": 2, "force_route": "proxy"})
        s_c = stats(proxy_port)
        check("set applies attempts", s_c["settings"]["attempts"] == 2, str(s_c["settings"]["attempts"]))
        check("set applies route", s_c["settings"]["force_route"] == "proxy", str(s_c["settings"]["force_route"]))

        ctl({"action": "reset"})
        s_r = stats(proxy_port)
        check("reset zeroes counters", s_r["requests"] == 0 and s_r["attempts"] == 0, f"{s_r['requests']}/{s_r['attempts']}")
        ctl({"action": "set", "attempts": 4, "force_route": None})

        print("\n== 10. live event feed ==")
        sock = socket.create_connection(("127.0.0.1", proxy_port), timeout=5)
        sock.sendall(b"GET /__events HTTP/1.1\r\nHost: 127.0.0.1\r\nAccept: text/event-stream\r\n\r\n")
        sock.settimeout(5)
        buf = b""
        try:
            while b"data: " not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
        except socket.timeout:
            pass
        sock.close()
        check("SSE stream opens with 200", buf.startswith(b"HTTP/1.1 200") or buf.startswith(b"HTTP/1.0 200"), buf[:40].decode("latin1"))
        check("SSE pushes JSON events", b'"attempts"' in buf and b'"ts"' in buf, buf[-160:].decode("latin1", "replace"))

        print("\n== 11. zero-downtime handover ==")
        old_pid = stats(proxy_port)["pid"]
        proc4 = start_proxy(proxy_port, f"http://127.0.0.1:{mock_port}/v1", handover=True)
        time.sleep(1.5)
        check("handover instance is alive (bound alongside)", proc4.poll() is None, f"rc={proc4.poll()}")
        proc.kill()
        proc.wait(timeout=10)
        time.sleep(0.4)
        st, body, _ = post(proxy_port)
        check("still serving after the old instance exits", st == 200, f"status={st}")
        check("new pid now answers /__health", stats(proxy_port)["pid"] == proc4.pid,
              f"{stats(proxy_port)['pid']} vs {proc4.pid}")
        # The handover instance is the live one now; keep it for the next cases.
        proc.wait(timeout=10)
        proc = proc4
        print("\n== 12. a probe of our own root is answered locally, not by the gateway ==")
        c = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=5)
        c.request("GET", "/")
        r = c.getresponse()
        root = r.read()
        c.close()
        check("GET / is 200 with local metadata", r.status == 200 and b"retry proxy" in root, f"status={r.status} body={root[:120]!r}")

        print("\n== 13. a drain lets an in-flight request finish ==")
        plan(mock_port, ["slow"], delay=2.5)
        slow = threading.Thread(target=lambda: post(proxy_port, timeout=60), daemon=True)
        slow.start()
        time.sleep(0.8)
        st, d = ctl({"action": "drain", "pid": proc.pid})
        check("drain accepted on the addressed instance", st == 200 and d.get("ok") is True, str(d))
        time.sleep(0.5)
        check("proxy is still alive while draining", proc.poll() is None, f"rc={proc.poll()}")
        slow.join(timeout=20)
        check("the in-flight request completed", not slow.is_alive())
        time.sleep(1.0)
        check("proxy exited after the drain", proc.poll() is not None, f"rc={proc.poll()}")
        proc = None

        print("\n== 14. a foreign server on the port pushes the proxy to the next one ==")
        squatted = free_port()
        spare = free_port()
        squatter = subprocess.Popen(
            [sys.executable, str(HERE / "squat_port.py"), str(squatted), "40"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(1.0)
        proc5 = start_proxy(
            squatted, f"http://127.0.0.1:{mock_port}/v1",
            ports=f"{squatted},{spare}",
        )
        time.sleep(2.5)
        check("the proxy is alive", proc5.poll() is None, f"rc={proc5.poll()}")
        check("it left the squatted port alone", proc5.pid is not None and not _owns(squatted, proc5.pid))
        check("it serves on the next port", wait_health(spare, timeout=5),
              f"spare={spare} squatted={squatted}")
        published = (RUN_DIR / "active-port.txt")
        if published.exists():
            check("it published the active port", published.read_text().strip() == str(spare),
                  published.read_text().strip())
        proc5.kill()
        squatter.kill()
        squatter.wait(timeout=10)

    finally:
        if proc is not None:
            proc.kill()
        mock_srv.shutdown()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{passed}/{total} checks passed")
    failed = [n for n, ok, _ in RESULTS if not ok]
    if failed:
        print("failed: " + ", ".join(failed))
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
