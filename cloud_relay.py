"""
Stable public door for browser AIs.

Runs on Render (or any host with HTTPS). It does NOT talk to Blender.
Your PC runs local_agent.py, which dials OUT to this relay and forwards
/mcp traffic to the local server on 127.0.0.1:8000.

Browser AIs only ever see:
  https://YOUR-SERVICE.onrender.com/mcp
  https://YOUR-SERVICE.onrender.com/talk
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cloud_features

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "10000"))
TOKEN = os.environ.get("SITE_TOKEN", "").strip()
PUBLIC = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
ROBLOX_API_KEY = os.environ.get("ROBLOX_API_KEY", "").strip()
ROBLOX_UNIVERSE_ID = os.environ.get("ROBLOX_UNIVERSE_ID", "").strip()
ROBLOX_PLACE_ID = os.environ.get("ROBLOX_PLACE_ID", "").strip()
ROBLOX_BASE = "https://apis.roblox.com/cloud/v2"

_lock = threading.Lock()
_messages = []
_seen = {}
_queue = []
_waiters = []
_results = {}
_agent_seen = 0.0


def _ok_token(header: str) -> bool:
    if not TOKEN:
        return True
    header = (header or "").strip()
    return header in {TOKEN, f"Bearer {TOKEN}"}


def _agent_online() -> bool:
    return time.time() - _agent_seen < 20


def _add_message(sender: str, text: str, to: str = "all") -> dict:
    item = {
        "id": (_messages[-1]["id"] + 1) if _messages else 1,
        "ts": time.time(),
        "from": (sender or "ai")[:40],
        "to": (to or "all")[:40],
        "text": (text or "")[:2000],
    }
    with _lock:
        _messages.append(item)
        del _messages[:-400]
        _seen[item["from"]] = time.time()
    return item


def _http_json(url, method="GET", body=None, headers=None, timeout=45):
    data = None if body is None else json.dumps(body).encode("utf-8")
    merged = {"Accept": "application/json", "Content-Type": "application/json"}
    if headers:
        merged.update(headers)
    req = urllib.request.Request(url, data=data, headers=merged, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            try:
                value = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                value = {"raw": raw}
            return {"status": "success", "http_status": resp.status, "result": value}
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8") or "{}")
        except Exception:
            detail = {"raw": str(exc)}
        return {"status": "error", "http_status": exc.code, "result": detail}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


def roblox_call(path, method="GET", body=None):
    if not ROBLOX_API_KEY or not ROBLOX_UNIVERSE_ID:
        return {"status": "error", "message": "Set ROBLOX_API_KEY and ROBLOX_UNIVERSE_ID on Render"}
    return _http_json(ROBLOX_BASE.rstrip("/") + "/" + path.lstrip("/"), method, body, {"x-api-key": ROBLOX_API_KEY})


def turbowarp_js():
    server = PUBLIC or ""
    return """(function(Scratch) {
  const SERVER = "__SERVER__";
  const ext = {
    _last: "",
    _request(path, payload) {
      return Scratch.fetch(SERVER + path, {
        method: "POST",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify(payload || {})
      }).then(r => r.json()).catch(e => ({status:"error", message:String(e)}));
    },
    getInfo() {
      return {
        id: "feranmimcp",
        name: "Feranmi MCP",
        blocks: [
          {opcode:"connect", blockType:Scratch.BlockType.COMMAND, text:"MCP connect"},
          {opcode:"status", blockType:Scratch.BlockType.REPORTER, text:"MCP status"},
          {opcode:"ask", blockType:Scratch.BlockType.REPORTER, text:"MCP ask [TASK]", arguments:{TASK:{type:Scratch.ArgumentType.STRING, defaultValue:"hello"}}}
        ]
      };
    },
    connect() { return this._request("/api/turbowarp", {action:"status"}).then(x => { this._last = JSON.stringify(x); }); },
    status() { return this._last || "not connected"; },
    ask(args) { return this._request("/api/turbowarp", {action:"ask", task:String(args.TASK||"")}).then(x => JSON.stringify(x)); }
  };
  Scratch.extensions.register(ext);
})(Scratch);""".replace("__SERVER__", server)


def turbowarp_action(data):
    action = str(data.get("action") or "").strip().lower()
    if action == "status":
        return {"status": "success", "turbowarp": "extension endpoint live", "roblox_configured": bool(ROBLOX_API_KEY and ROBLOX_UNIVERSE_ID), "agent_online": _agent_online()}
    if action == "ask":
        text = str(data.get("task") or "").strip()
        if not text:
            return {"status": "error", "message": "task is required"}
        return _add_message("turbowarp", text[:500])
    return {"status": "error", "message": "Unknown TurboWarp action: " + action}


PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Feranmi bridge</title>
  <style>
    body { font-family: sans-serif; max-width: 42rem; margin: 2rem auto; padding: 0 1rem; line-height: 1.45; }
    code, pre { background: #111; color: #eee; padding: 0.6rem; display: block; overflow: auto; }
    .ok { color: #0a0; } .bad { color: #a00; }
  </style>
</head>
<body>
  <h1>Feranmi bridge</h1>
  <p>Stable URL for browser AIs. Blender and Godot stay on the home PC.</p>
  <p>Agent: <strong id="agent">checking</strong></p>
  <h2>Paste this in the browser AI connector</h2>
  <pre id="mcp">/mcp</pre>
  <h2>Team chat</h2>
  <pre id="talk"></pre>
  <script>
    async function tick() {
      const h = await fetch("/health").then(r => r.json());
      const el = document.getElementById("agent");
      el.textContent = h.agent_online ? "PC connected" : "PC offline — start local_agent.py";
      el.className = h.agent_online ? "ok" : "bad";
      document.getElementById("mcp").textContent = location.origin + "/mcp";
      const t = await fetch("/talk").then(r => r.json());
      document.getElementById("talk").textContent = (t.messages || []).map(m => "#" + m.id + " " + m.from + ": " + m.text).join("\\n") || "(no messages)";
    }
    tick(); setInterval(tick, 3000);
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body, content_type="application/json", extra=None):
        raw = body if isinstance(body, bytes) else (
            body.encode("utf-8") if str(content_type).startswith("text/") else json.dumps(body).encode("utf-8")
        )
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(raw)))
        if extra:
            for key, value in extra.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def _auth(self):
        return self.headers.get("Authorization") or self.headers.get("X-Site-Token") or ""

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Site-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if path == "/health":
            return self._send(200, {
                "ok": True,
                "agent_online": _agent_online(),
                "talk": len(_messages),
                "mcp": (PUBLIC or "") + "/mcp",
            })
        if path == "/mcp-info":
            return self._send(200, {
                "mcp": (PUBLIC or "") + "/mcp",
                "talk": (PUBLIC or "") + "/talk",
                "token_required": bool(TOKEN),
                "agent_online": _agent_online(),
            })
        if path == "/talk":
            return self._send(200, {"messages": _messages[-50:], "online": list(_seen)})
        if path == "/turbowarp-extension.js":
            return self._send(200, cloud_features.turbowarp_js(PUBLIC or ""), "application/javascript; charset=utf-8")
        if path == "/api/roblox/status":
            return self._send(200, cloud_features.roblox_status())
        return self._proxy()

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if path == "/talk":
            if TOKEN and not _ok_token(self._auth()):
                return self._send(401, {"error": "bad token"})
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                return self._send(400, {"error": "invalid json"})
            text = str(data.get("text") or "").strip()
            if not text:
                return self._send(400, {"error": "text required"})
            return self._send(200, _add_message(str(data.get("from") or "ai"), text, str(data.get("to") or "all")))
        if path == "/api/turbowarp":
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                return self._send(400, {"error": "invalid json"})
            return self._send(200, {"status": "success", "note": "Use the extension blocks. They read the Scratch VM in the browser."})
        if path == "/api/roblox":
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                return self._send(400, {"error": "invalid json"})
            return self._send(200, cloud_features.roblox_action(data))
        if path == "/tunnel/poll":
            return self._poll(raw)
        if path == "/tunnel/result":
            return self._result(raw)
        return self._proxy(raw)

    def do_DELETE(self):
        self._proxy()

    def _poll(self, raw):
        global _agent_seen
        if TOKEN and not _ok_token(self._auth()):
            return self._send(401, {"error": "bad token"})
        _agent_seen = time.time()
        event = threading.Event()
        with _lock:
            if _queue:
                job = _queue.pop(0)
                return self._send(200, job)
            _waiters.append(event)
        event.wait(20)
        with _lock:
            if event in _waiters:
                _waiters.remove(event)
            if _queue:
                return self._send(200, _queue.pop(0))
        _agent_seen = time.time()
        return self._send(200, {"idle": True})

    def _result(self, raw):
        global _agent_seen
        if TOKEN and not _ok_token(self._auth()):
            return self._send(401, {"error": "bad token"})
        _agent_seen = time.time()
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "invalid json"})
        job_id = data.get("id")
        box = _results.get(job_id)
        if not box:
            return self._send(404, {"error": "unknown job"})
        box["payload"] = data
        box["event"].set()
        return self._send(200, {"ok": True})

    def _proxy(self, raw=None):
        if not _agent_online():
            return self._send(503, {
                "error": "PC bridge offline",
                "fix": "On the PC run: python local_agent.py",
            })
        if raw is None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
        job_id = uuid.uuid4().hex
        event = threading.Event()
        job = {
            "id": job_id,
            "method": self.command,
            "path": self.path,
            "headers": {k: v for k, v in self.headers.items() if k.lower() not in {"host", "content-length", "connection"}},
            "body_b64": base64.b64encode(raw).decode("ascii"),
        }
        with _lock:
            _queue.append(job)
            _results[job_id] = {"event": event, "payload": None}
            if _waiters:
                _waiters.pop(0).set()
        if not event.wait(55):
            with _lock:
                _results.pop(job_id, None)
            return self._send(504, {"error": "PC bridge timed out"})
        with _lock:
            payload = _results.pop(job_id, {}).get("payload") or {}
        status = int(payload.get("status") or 502)
        body = base64.b64decode(payload.get("body_b64") or "")
        ctype = (payload.get("headers") or {}).get("Content-Type") or "application/json"
        return self._send(status, body, ctype)

    def log_message(self, fmt, *args):
        print("[relay]", fmt % args)


if __name__ == "__main__":
    print(f"Relay on :{PORT} public={PUBLIC or 'local'}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
