"""Measure rescue probability on the byte-identical captured request."""
import json
import sys
import time
import urllib.error
import urllib.request
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "https://opencode.ai/inference/openai/v1"
OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler(
        {"http": "http://127.0.0.1:7899", "https": "http://127.0.0.1:7899"}
    )
)
HOP = {"host", "connection", "content-length", "transfer-encoding", "accept-encoding"}
rows = [json.loads(l) for l in open("relay_log.jsonl", encoding="utf-8")]
row = next(r for r in rows if r["body_len"] < 4000)
full = {k: v for k, v in row["headers"].items() if k.lower() not in HOP}
HDRS = {
    "Authorization": full["Authorization"],
    "Content-Type": "application/json",
    "Accept": full.get("Accept", "*/*"),
    "User-Agent": full["User-Agent"],
}
HDRS.update({k: v for k, v in full.items() if k.lower().startswith("x-")})
RAW = row["body"].encode()


def send():
    req = urllib.request.Request(BASE + "/chat/completions", data=RAW, method="POST", headers=HDRS)
    t0 = time.time()
    try:
        with OPENER.open(req, timeout=120) as r:
            st, data = r.status, r.read(64)
    except urllib.error.HTTPError as e:
        st, data = e.code, e.read(200)
    except Exception as e:
        return "ERR", int((time.time() - t0) * 1000)
    ms = int((time.time() - t0) * 1000)
    t = data.decode("utf-8", "replace")
    if st == 200:
        return "OK", ms
    if "abusive" in t:
        return "IP_REP", ms
    if "FreeTierError" in t:
        return "IDENTITY", ms
    return f"HTTP{st}", ms


n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
seq = []
for i in range(n):
    cls, ms = send()
    seq.append(cls)
    print(f"  {i:02d} {cls:9} {ms:5}ms", flush=True)
    time.sleep(0.3)

print("\nseq:", " ".join(seq))
print("counts:", dict(Counter(seq)))
ok = seq.count("OK")
print(f"success {ok}/{n} = {ok/n:.0%}")
# conditional: after a failure
pair_ok = sum(1 for a, b in zip(seq, seq[1:]) if a != "OK" and b == "OK")
pair_n = sum(1 for a in seq[:-1] if a != "OK")
print(f"P(ok | prev fail) = {pair_ok}/{pair_n}" + (f" = {pair_ok/pair_n:.0%}" if pair_n else ""))
# P(ok within 3 retries | failure)
resc = 0
resc_n = 0
for i, c in enumerate(seq):
    if c != "OK":
        resc_n += 1
        if any(x == "OK" for x in seq[i + 1 : i + 4]):
            resc += 1
print(f"P(ok within 3 next attempts | fail) = {resc}/{resc_n}" + (f" = {resc/resc_n:.0%}" if resc_n else ""))
# streak distribution
streaks, cur = [], 0
for c in seq:
    if c != "OK":
        cur += 1
    elif cur:
        streaks.append(cur)
        cur = 0
if cur:
    streaks.append(cur)
print("failure streaks:", streaks)
json.dump(seq, open("measure_rescue_seq.json", "w"), indent=1)
