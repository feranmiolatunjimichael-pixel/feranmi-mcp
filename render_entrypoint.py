import os
import subprocess
import sys
import time

from server import INTERNAL_MCP_PORT, start_public_mux

public_port = int(os.environ.get("PORT", "10000"))
internal_port = int(os.environ.get("INTERNAL_MCP_PORT", str(INTERNAL_MCP_PORT)))

child = subprocess.Popen([
    sys.executable,
    "server.py",
    "--transport", "http",
    "--host", "127.0.0.1",
    "--port", str(internal_port),
])

try:
    time.sleep(1)
    start_public_mux("0.0.0.0", public_port, internal_port)
finally:
    child.terminate()
    child.wait(timeout=10)
