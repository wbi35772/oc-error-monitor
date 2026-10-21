import ctypes
import ctypes.wintypes as wintypes
import socket
import struct

TCP_TABLE_OWNER_PID_ALL = 5
AF_INET = 2

iphlpapi = ctypes.WinDLL("iphlpapi.dll")
size = wintypes.DWORD(0)
iphlpapi.GetExtendedTcpTable(None, ctypes.byref(size), 0, AF_INET, TCP_TABLE_OWNER_PID_ALL, 0)
buf = ctypes.create_string_buffer(size.value)
iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(size), 0, AF_INET, TCP_TABLE_OWNER_PID_ALL, 0)
raw = buf.raw[:size.value]
print("total bytes:", len(raw))
print("first 96 bytes:")
for i in range(0, 96, 16):
    chunk = raw[i:i + 16]
    print(f"  {i:3d}: {chunk.hex(' ')}")

print("\nDWORDs:")
for i in range(0, 48, 4):
    v = struct.unpack_from("<I", raw, i)[0]
    print(f"  off {i:3d}: {v:12d}  0x{v:08x}")
