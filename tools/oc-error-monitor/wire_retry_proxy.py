"""Wire the retry proxy into the global opencode config.

Adds provider.opencode.models.<free-id>.provider.api -> http://127.0.0.1:8787/v1
for every free model in the live catalog, leaving the rest of the config alone.
Idempotent: re-running rewrites the same block.
"""
import json
import shutil
import sys
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CFG = r"C:\Users\24999\.config\opencode\opencode.json"
CATALOG = r"D:\Project\Openchamber\Opencode网络\tools\oc-error-monitor\models.json"
LOCAL = "http://127.0.0.1:8787/v1"

cat = json.load(open(CATALOG, encoding="utf-8"))
free_ids = sorted(
    mid
    for mid in cat["opencode"]["models"]
    if mid.endswith("-free") or mid == "big-pickle"
)
print(f"free models: {len(free_ids)}")

cfg = json.load(open(CFG, encoding="utf-8"))
stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
shutil.copy2(CFG, CFG + f".bak-retryproxy-{stamp}")
print("backup:", CFG + f".bak-retryproxy-{stamp}")

prov = cfg.setdefault("provider", {})
oc = prov.setdefault("opencode", {})
models = oc.setdefault("models", {})
for mid in free_ids:
    entry = models.setdefault(mid, {})
    entry.setdefault("provider", {})["api"] = LOCAL

with open(CFG, "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
    f.write("\n")

print(f"wired {len(models)} models -> {LOCAL}")
print("sample:", json.dumps({k: models[k] for k in free_ids[:2]}, indent=1))
