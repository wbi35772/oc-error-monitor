"""Who owns a TCP port on this machine?

Windows lets a process bind a port another process is already listening on
(SO_REUSEADDR), so "can I bind it" proves nothing: that is how the retry proxy
ended up sharing 8787 with playwright-mcp's internal server and half of the
model requests came back as "HTTP 404: not found".

This asks the OS directly through the IP Helper API
(GetExtendedTcpTable, the same table psutil reads), so the answer is exact:
which pid listens on which port, and what that process is.

    python port_owner.py            # every listening port with its owner
    python port_owner.py 8787 8788  # only these ports
"""
import ctypes
import ctypes.wintypes as wintypes
import json
import os
import socket
import struct
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TCP_TABLE_OWNER_PID_ALL = 5
AF_INET = 2

if os.name == "nt":
    class MIB_TCPROW_OWNER_PID(ctypes.Structure):
        _fields_ = [
            ("dwState", wintypes.DWORD),
            ("dwLocalAddr", wintypes.DWORD),
            ("dwLocalPort", wintypes.DWORD),
            ("dwRemoteAddr", wintypes.DWORD),
            ("dwRemotePort", wintypes.DWORD),
            ("dwOwningPid", wintypes.DWORD),
        ]

    def _tcp_table():
        iphlpapi = ctypes.WinDLL("iphlpapi.dll")
        size = wintypes.DWORD(0)
        # First call sizes the buffer.
        iphlpapi.GetExtendedTcpTable(None, ctypes.byref(size), 0, AF_INET,
                                     TCP_TABLE_OWNER_PID_ALL, 0)
        if size.value == 0:
            return []
        buf = ctypes.create_string_buffer(size.value)
        if iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(size), 0, AF_INET,
                                        TCP_TABLE_OWNER_PID_ALL, 0) != 0:
            return []
        raw = buf.raw
        row = ctypes.sizeof(MIB_TCPROW_OWNER_PID)
        # The table starts with a 4 byte entry count, then the rows.
        count = struct.unpack_from("<I", raw, 0)[0]
        out = []
        for i in range(count):
            off = 4 + i * row
            if off + row > len(raw):
                break
            r = MIB_TCPROW_OWNER_PID.from_buffer_copy(raw[off:off + row])
            # dwLocalPort is in network byte order.
            port = socket.ntohs(struct.unpack_from("<H", raw, off + 8)[0])
            addr = socket.inet_ntop(AF_INET, raw[off + 4:off + 8])
            out.append({"port": port, "addr": addr, "pid": int(r.dwOwningPid),
                        "state": int(r.dwState)})
        return out
else:  # pragma: no cover - the proxy only runs on Windows here
    def _tcp_table():
        return []


def _proc_name(pid):
    try:
        if os.name == "nt":
            import ctypes as _c
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = _c.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return None
            buf = _c.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            try:
                _c.windll.kernel32.QueryFullProcessImageNameW(h, 0, buf, _c.byref(size))
            finally:
                _c.windll.kernel32.CloseHandle(h)
            return buf.value
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except Exception:
        return None


def _cmdline(pid):
    try:
        if os.name == "nt":
            out = subprocess_run_cim(pid)
            return out
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except Exception:
        return None


def subprocess_run_cim(pid):
    try:
        import subprocess

        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
            capture_output=True, text=True, timeout=10,
        )
        return (out.stdout or "").strip() or None
    except Exception:
        return None


def listeners(port=None):
    """Every listening TCP port with its owner, newest table first."""
    rows = _tcp_table()
    # MIB_TCP_STATE_LISTEN == 2
    out = []
    for r in rows:
        if r["state"] != 2:
            continue
        if port is not None and r["port"] != port:
            continue
        out.append(r)
    return out


def owners(port=None):
    """The processes listening on a port, with their names."""
    wanted = None
    if port is not None:
        wanted = {port} if isinstance(port, int) else set(port)
    out = []
    for r in listeners():
        if wanted is not None and r["port"] not in wanted:
            continue
        name = _proc_name(r["pid"])
        out.append({
            "port": r["port"],
            "addr": r["addr"],
            "pid": r["pid"],
            "exe": name,
            "cmd": _cmdline(r["pid"]) if name else None,
        })
    return out


def main():
    args = [a for a in sys.argv[1:] if a.isdigit()]
    ports = [int(a) for a in args] or None
    rows = owners(ports) if ports else None
    if rows is None:
        rows = []
        for r in listeners():
            rows.append({
                "port": r["port"], "addr": r["addr"], "pid": r["pid"],
                "exe": _proc_name(r["pid"]), "cmd": None,
            })
        rows.sort(key=lambda x: x["port"])
    print(f"{'PORT':>6}  {'PID':>7}  EXE")
    for r in rows:
        exe = (r.get("exe") or "").split("\\")[-1]
        cmd = (r.get("cmd") or "")
        short = cmd[:90] if cmd else ""
        print(f"{r['port']:>6}  {r['pid']:>7}  {exe}  {short}")
    if not rows:
        print("(no listeners)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
