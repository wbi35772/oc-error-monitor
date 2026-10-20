"""Supervisor for the retry proxy.

The scheduled task runs once a minute, which is a long outage when the proxy
dies. This process stays alive instead: it owns the proxy as a child process
and respawns it within a second of a crash. The scheduled task remains the
last-resort net, because it also restarts this supervisor.

    supervisor.py
      -> starts retry_proxy.py as a child
      -> child exits with 0  -> another instance owns the port, supervisor exits
      -> child dies          -> respawn after 1 s (crash-loop guard included)

Run it detached:  pythonw.exe supervisor.py
"""
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PROXY = os.path.join(HERE, "retry_proxy.py")
PORT = int(os.environ.get("RETRY_PROXY_PORT", "8787"))
HOST = os.environ.get("RETRY_PROXY_HOST", "127.0.0.1")
RESPAWN_DELAY = float(os.environ.get("RETRY_PROXY_RESPAWN_DELAY", "1.0"))
MAX_RESPAWNS = int(os.environ.get("RETRY_PROXY_MAX_RESPAWNS", "60"))
WINDOW_SEC = 60.0

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def healthy():
    try:
        with _opener.open(f"http://{HOST}:{PORT}/__health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def log(msg):
    try:
        path = os.path.join(HERE, "supervisor.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%S") + " " + msg + "\n")
    except Exception:
        pass


def main():
    if healthy():
        log("healthy already, nothing to supervise")
        return 0
    if not os.path.exists(PROXY):
        log(f"missing {PROXY}")
        return 1

    respawns = []
    while True:
        try:
            proc = subprocess.Popen([sys.executable, PROXY], cwd=HERE)
        except Exception as e:
            log(f"spawn failed: {e}")
            return 1
        rc = proc.wait()
        if rc == 0:
            log("child exited cleanly, supervisor done")
            return 0
        now = time.time()
        respawns = [t for t in respawns if now - t < WINDOW_SEC]
        respawns.append(now)
        if len(respawns) > MAX_RESPAWNS:
            log(f"crash loop: {len(respawns)} restarts in {WINDOW_SEC:.0f}s, giving up")
            return 1
        log(f"child exited rc={rc}, respawning in {RESPAWN_DELAY:.1f}s")
        time.sleep(RESPAWN_DELAY)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        import traceback

        log(traceback.format_exc())
        raise
