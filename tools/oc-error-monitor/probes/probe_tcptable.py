import ctypes
import ctypes.wintypes as wintypes
import socket
import struct

TCP_TABLE_OWNER_PID_ALL = 5
AF_INET = 2


class ROW(ctypes.Structure):
    _fields_ = [
        ("dwState", wintypes.DWORD),
        ("dwLocalAddr", wintypes.DWORD),
        ("dwLocalPort", wintypes.DWORD),
        ("dwRemoteAddr", wintypes.DWORD),
        ("dwRemotePort", wintypes.DWORD),
        ("dwOwningPid", wintypes.DWORD),
    ]


iphlpapi = ctypes.WinDLL("iphlpapi.dll")
size = wintypes.DWORD(0)
rc = iphlpapi.GetExtendedTcpTable(None, ctypes.byref(size), 0, AF_INET, TCP_TABLE_OWNER_PID_ALL, 0)
print("first call rc=", rc, "size=", size.value)
buf = ctypes.create_string_buffer(size.value)
rc = iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(size), 0, AF_INET, TCP_TABLE_OWNER_PID_ALL, 0)
print("second call rc=", rc, "size=", size.value, "rowsize=", ctypes.sizeof(ROW))
row_size = ctypes.sizeof(ROW)
for base in (0, 8):
    print(f"--- rows starting at offset {base} ---")
    n = 0
    for off in range(base, size.value - row_size + 1, row_size):
        r = ROW.from_buffer_copy(buf.raw[off:off + row_size])
        port_le = struct.unpack_from("<H", buf.raw, off + 8)[0]
        port = socket.ntohs(port_le)
        addr = socket.inet_ntop(AF_INET, buf.raw[off + 4:off + 8])
        if n < 8:
            print(f"  state={r.dwState} addr={addr} port={port} pid={r.dwOwningPid}")
        n += 1
    print("  rows:", n)
