#!/usr/bin/env python3
"""Read one environment variable out of a running Windows process.

The OpenChamber-managed OpenCode server authenticates with a password that
OpenChamber generates in memory and injects into the child process as
OPENCODE_SERVER_PASSWORD. It is never written to disk, so the only way to
talk to that server's API is to read it from the child process environment.

Every remote read goes through ReadProcessMemory; remote addresses are never
dereferenced directly in this process.

Usage:
  python read_proc_env.py 21808 OPENCODE_SERVER_PASSWORD
  python read_proc_env.py 21808 --list
"""

from __future__ import annotations

import ctypes
import struct
import sys
from ctypes import wintypes

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010

# x64 PEB offsets
PEB_PROCESS_PARAMETERS_OFFSET = 0x20
# x64 RTL_USER_PROCESS_PARAMETERS offsets
RTLPP_ENVIRONMENT_OFFSET = 0x80


class PROCESS_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("Reserved1", ctypes.c_void_p),
        ("PebBaseAddress", ctypes.c_void_p),
        ("Reserved2", ctypes.c_void_p * 2),
        ("UniqueProcessId", ctypes.c_void_p),
        ("Reserved3", ctypes.c_void_p),
    ]


def _read_remote(handle, address: int, size: int) -> bytes:
    """Read `size` bytes at remote `address`. Returns b"" on failure."""
    if address == 0 or size <= 0:
        return b""
    buf = ctypes.create_string_buffer(size)
    got = ctypes.c_size_t(0)
    ok = ctypes.windll.kernel32.ReadProcessMemory(
        wintypes.HANDLE(handle),
        ctypes.c_void_p(address),
        ctypes.cast(buf, ctypes.c_void_p),
        ctypes.c_size_t(size),
        ctypes.byref(got),
    )
    if not ok:
        return b""
    return buf.raw[: got.value]


def _read_u64(handle, address: int) -> int:
    raw = _read_remote(handle, address, 8)
    return struct.unpack("<Q", raw)[0] if len(raw) == 8 else 0


def process_environment(pid: int) -> dict[str, str]:
    kernel32 = ctypes.windll.kernel32
    ntdll = ctypes.windll.ntdll

    handle = kernel32.OpenProcess(
        PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid
    )
    if not handle:
        raise SystemExit(
            f"OpenProcess failed for pid {pid} (err {kernel32.GetLastError()})"
        )

    try:
        pbi = PROCESS_BASIC_INFORMATION()
        ret = wintypes.DWORD(0)
        status = ntdll.NtQueryInformationProcess(
            handle, 0, ctypes.byref(pbi), ctypes.sizeof(pbi), ctypes.byref(ret)
        )
        if status != 0:
            raise SystemExit(
                f"NtQueryInformationProcess failed: 0x{status & 0xFFFFFFFF:x}"
            )

        peb = pbi.PebBaseAddress
        if not peb:
            raise SystemExit("PEB base address is null")

        params = _read_u64(handle, peb + PEB_PROCESS_PARAMETERS_OFFSET)
        if not params:
            raise SystemExit("ProcessParameters pointer is null")

        env_ptr = _read_u64(handle, params + RTLPP_ENVIRONMENT_OFFSET)
        if not env_ptr:
            raise SystemExit("Environment pointer is null")

        # Read the environment block in chunks until a double NUL terminator.
        chunk = 0x8000
        data = b""
        while len(data) < 8 * 1024 * 1024:
            part = _read_remote(handle, env_ptr + len(data), chunk)
            if not part:
                break
            data += part
            if b"\x00\x00\x00\x00" in data:
                break

        text = data.decode("utf-16-le", "replace")
        env: dict[str, str] = {}
        for entry in text.split("\x00"):
            if not entry or "=" not in entry:
                continue
            k, _, v = entry.partition("=")
            env[k] = v
        return env
    finally:
        kernel32.CloseHandle(handle)


def main(argv: list[str]) -> int:
    if len(argv) < 1:
        print(__doc__)
        return 2
    pid = int(argv[0])
    env = process_environment(pid)
    if len(argv) == 1 or argv[1] == "--list":
        interesting = (
            "OPENCODE",
            "OPENCHAMBER",
            "PROXY",
            "OPENROUTER",
            "API_KEY",
            "TOKEN",
            "PASSWORD",
            "AUTH",
        )
        for k in sorted(env):
            if any(s in k.upper() for s in interesting):
                print(f"{k}={env[k]}")
        return 0
    name = argv[1]
    if name not in env:
        print(f"{name} is not set in pid {pid}", file=sys.stderr)
        return 1
    print(env[name])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
