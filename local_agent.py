"""
Runs on the PC. Dials out to the stable Render URL and forwards MCP
traffic to the local server.

Start order:
  1. Blender server on 9876
  2. Godot Play (9877)
  3. python server.py --transport http --host 127.0.0.1 --port 8000
  4. python local_agent.py

Env:
  RELAY_URL   https://your-service.onrender.com
  SITE_TOKEN  same token as Render
  LOCAL_MCP   default http://127.0.0.1:8000
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request

RELAY = os.environ.get("RELAY_URL", "").rstrip("/")
TOKEN = os.environ.get("SITE_TOKEN", "").strip()
LOCAL = os.environ.get("LOCAL_MCP", "http://127.0.0.1:8000").rstrip("/")


def _headers():
    headers = {"Content-Type": "application/json"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    return headers


def _post(path, payload, timeout=30):
    req = urllib.request.Request(
        RELAY + path,
        data=json.dumps(payload).encode("utf-8"),
        headers=_headers(),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def _forward(job):
    body = base64.b64decode(job.get("body_b64") or "")
    headers = dict(job.get("headers") or {})
    headers["Host"] = "127.0.0.1"
    req = urllib.request.Request(LOCAL + job.get("path", "/mcp"), data=body or None, headers=headers, method=job.get("method", "POST"))
    try:
        with urllib.request.urlopen(req, timeout=50) as resp:
            raw = resp.read()
            return resp.status, dict(resp.headers), raw
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def main():
    if not RELAY:
        raise SystemExit("Set RELAY_URL to your Render URL, e.g. https://feranmi-mcp.onrender.com")
    print(f"Agent linking {RELAY} -> {LOCAL}")
    while True:
        try:
            job = _post("/tunnel/poll", {}, timeout=30)
            if not job or job.get("idle") or not job.get("id"):
                continue
            status, headers, raw = _forward(job)
            _post("/tunnel/result", {
                "id": job["id"],
                "status": status,
                "headers": {"Content-Type": headers.get("Content-Type", "application/json")},
                "body_b64": base64.b64encode(raw).decode("ascii"),
            })
        except Exception as exc:
            print("agent retry:", exc)
            time.sleep(3)


if __name__ == "__main__":
    main()
