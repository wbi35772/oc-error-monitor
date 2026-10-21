"""Measure P(retry succeeds | previous attempt was a 403) against the real
opencode free tier, using the exact client identity the CLI sends.

Identity gate (verified from a captured CLI request):
    User-Agent: opencode/latest/2.0.32/cli
    x-opencode-client: cli
    Authorization: Bearer public

Run:  python probe_retry2.py [attempts_per_model]
"""
import json
import statistics
import sys
import time
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "https://opencode.ai/inference/openai/v1"
PROXY = "http://127.0.0.1:7899"
UA = "opencode/latest/2.0.32/cli"

OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})
)

MODELS = [
    "deepseek-v4.1-flash-free",
    "minimax-m3.1-flash-free",
    "big-pickle",
]


def classify(status, body):
    t = body.decode("utf-8", "replace")
    if status == 200:
        return "OK"
    if "abusive traffic" in t:
        return "IP_REP"
    if "FreeTierError" in t:
        return "IDENTITY"
    if "1010" in t:
        return "CF"
    if status == 429:
        return "RATE"
    if status >= 500:
        return "5XX"
    return f"HTTP{status}"


def attempt(model):
    payload = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
            "max_tokens": 2,
            "stream": False,
        }
    ).encode()
    req = urllib.request.Request(
        f"{BASE}/chat/completions",
        data=payload,
        method="POST",
        headers={
            "Authorization": "Bearer public",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": UA,
            "x-opencode-client": "cli",
        },
    )
    t0 = time.time()
    try:
        with OPENER.open(req, timeout=60) as r:
            st, body = r.status, r.read()
    except urllib.error.HTTPError as e:
        st, body = e.code, e.read()
    except Exception as e:
        return -1, f"ERR:{type(e).__name__}", int((time.time() - t0) * 1000), b""
    return st, classify(st, body), int((time.time() - t0) * 1000), body


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    all_rows = []
    for model in MODELS:
        rows = []
        print(f"\n=== {model} ({n} sequential attempts) ===")
        for i in range(n):
            st, cls, ms, body = attempt(model)
            rows.append({"i": i, "status": st, "class": cls, "ms": ms})
            all_rows.append({"model": model, **rows[-1]})
            mark = "ok " if cls == "OK" else "   "
            print(f"  {mark}{i:02d} {st} {cls:9} {ms:5}ms")
            time.sleep(0.4)
        ok = sum(1 for r in rows if r["class"] == "OK")
        print(f"  -> success {ok}/{n} = {ok/n:.0%}")
        # conditioned on previous failure
        resc = 0
        resc_n = 0
        for a, b in zip(rows, rows[1:]):
            if a["class"] != "OK":
                resc_n += 1
                if b["class"] == "OK":
                    resc += 1
        print(f"  -> P(ok | prev fail) = {resc}/{resc_n}" + (f" = {resc/resc_n:.0%}" if resc_n else ""))

    with open("probe_retry2_results.json", "w", encoding="utf-8") as f:
        json.dump(all_rows, f, indent=1)

    print("\n=== aggregate ===")
    ok = sum(1 for r in all_rows if r["class"] == "OK")
    print(f"total {ok}/{len(all_rows)} = {ok/len(all_rows):.0%}")
    from collections import Counter

    print("classes:", dict(Counter(r["class"] for r in all_rows)))
    oks = [r["ms"] for r in all_rows if r["class"] == "OK"]
    if oks:
        print(f"latency ok: median {statistics.median(oks):.0f}ms  min {min(oks)} max {max(oks)}")
    # longest failure streaks
    streak = best = 0
    for r in all_rows:
        streak = streak + 1 if r["class"] != "OK" and r["model"] == all_rows[0]["model"] else streak
    print("rows saved: probe_retry2_results.json")


if __name__ == "__main__":
    main()
