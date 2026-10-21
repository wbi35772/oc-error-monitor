"""Hold a TCP port so the retry proxy has to move somewhere else."""
import socket
import sys
import time

port = int(sys.argv[1]) if len(sys.argv) > 1 else 18787
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", port))
s.listen(5)
print(f"squatting 127.0.0.1:{port}", flush=True)
time.sleep(float(sys.argv[2]) if len(sys.argv) > 2 else 60)
