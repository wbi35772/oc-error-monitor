"""Supervisor for the retry proxy.

The scheduled task runs once a minute, which is a long outage when the proxy
dies. This process stays alive instead: it owns the proxy as a child process
and respawns it within a second of a crash. The scheduled task remains the
last-resort net, because it also restarts this supervisor.

    supervisor.py
      -> port already served (a handover instance, or a proxy started by hand):
         watch it and take over if it dies
      -> otherwise: start retry_proxy.py as a child and keep it alive
      -> child exits cleanly because another instance owns the port -> watch
      -> child dies -> respawn after 1 s (crash-loop guard included)

A lock file makes this idempotent, so the minute task can call it blindly.

Run it detached:  pythonw.exe supervisor.py
"""
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PROXY = os.path.join(HERE, "retry_proxy.py")
WIRE = os.path.join(HERE, "wire_retry_proxy.py")
ACTIVE_PORT_FILE = os.path.join(HERE, "active-port.txt")
PORT = int(os.environ.get("RETRY_PROXY_PORT", "8899"))
HOST = os.environ.get("RETRY_PROXY_HOST", "127.0.0.1")
RESPAWN_DELAY = float(os.environ.get("RETRY_PROXY_RESPAWN_DELAY", "1.0"))
MAX_RESPAWNS = int(os.environ.get("RETRY_PROXY_MAX_RESPAWNS", "60"))
WINDOW_SEC = 60.0
POLL_SEC = 2.0
LOCK_PATH = os.path.join(HERE, f"supervisor-{PORT}.lock")

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_lock_handle = None


def active_port():
    """The port the proxy actually uses. It moves when the port is taken."""
    try:
        with open(ACTIVE_PORT_FILE, encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return PORT


def healthy(port=None):
    try:
        with _opener.open(f"http://{HOST}:{port or active_port()}/__health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def rewire(port):
    """Point the opencode config at the port the proxy ended up on."""
    if not os.path.exists(WIRE):
        return
    try:
        out = subprocess.run(
            [sys.executable, WIRE, "--port", str(port)],
            capture_output=True, text=True, timeout=60,
        )
        msg = (out.stdout or "").strip().splitlines()
        if msg:
            log(msg[-1])
    except Exception as e:
        log(f"rewire failed: {e}")


def log(msg):
    try:
        path = os.path.join(HERE, "supervisor.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%S") + " " + msg + "\n")
    except Exception:
        pass


def acquire_lock():
    """One supervisor per port; a second one exits instead of double-spawning."""
    global _lock_handle
    try:
        f = open(LOCK_PATH, "a+b")
    except OSError:
        return None
    try:
        f.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            f.close()
        except OSError:
            pass
        return False
    _lock_handle = f
    return f


def main():
    lock = acquire_lock()
    if lock is False:
        log("another supervisor holds the lock, exiting")
        return 0
    if not os.path.exists(PROXY):
        log(f"missing {PROXY}")
        return 1

    if healthy():
        log("port already served, watching it")

    respawns = []
    wired_port = None
    while True:
        port = active_port()
        if healthy(port):
            if port != wired_port:
                # The proxy moved to another port: the opencode config has to
                # follow, or every model request fails.
                rewire(port)
                wired_port = port
            time.sleep(POLL_SEC)
            continue

        try:
            proc = subprocess.Popen([sys.executable, PROXY], cwd=HERE)
        except Exception as e:
            log(f"spawn failed: {e}")
            return 1
        time.sleep(1.0)
        port = active_port()
        if port != wired_port:
            rewire(port)
            wired_port = port
        rc = proc.wait()

        if rc == 0:
            # The child declined to run because someone else owns the port.
            if healthy():
                log("child exited cleanly: another instance owns the port, watching")
                continue
            log("child exited cleanly but the port is dead, retrying")
            time.sleep(RESPAWN_DELAY)
            continue

        now = time.time()
        respawns = [t for t in respawns if now - t < WINDOW_SEC]
        respawns.append(now)
        if len(respawns) > MAX_RESPAWNS:
            log(f"crash loop: {len(respawns)} restarts in {WINDOW_SEC:.0f}s, giving up")
            return 1
        log(f"child exited rc={rc}, checking the port again in {RESPAWN_DELAY:.1f}s")
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
