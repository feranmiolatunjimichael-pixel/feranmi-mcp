"""Cloud-side Roblox Open Cloud and TurboWarp extension.

These do not need the PC. Blender and Godot stay on the local bridge.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

ROBLOX_API_KEY = os.environ.get("ROBLOX_API_KEY", "").strip()
ROBLOX_UNIVERSE_ID = os.environ.get("ROBLOX_UNIVERSE_ID", "").strip()
ROBLOX_PLACE_ID = os.environ.get("ROBLOX_PLACE_ID", "").strip()
ROBLOX_BASE = "https://apis.roblox.com/cloud/v2"


def _http(url, method="GET", body=None, headers=None, timeout=45):
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


def roblox_ready():
    missing = []
    if not ROBLOX_API_KEY:
        missing.append("ROBLOX_API_KEY")
    if not ROBLOX_UNIVERSE_ID:
        missing.append("ROBLOX_UNIVERSE_ID")
    if missing:
        return {"status": "error", "message": "Missing " + ", ".join(missing)}
    return None


def roblox_call(path, method="GET", body=None):
    problem = roblox_ready()
    if problem:
        return problem
    return _http(ROBLOX_BASE.rstrip("/") + "/" + path.lstrip("/"), method, body, {"x-api-key": ROBLOX_API_KEY})


def _u():
    return urllib.parse.quote(ROBLOX_UNIVERSE_ID, safe="")


def _place(place_id):
    return urllib.parse.quote((place_id or ROBLOX_PLACE_ID or "").strip(), safe="")


def roblox_status():
    return {
        "configured": bool(ROBLOX_API_KEY and ROBLOX_UNIVERSE_ID),
        "universe_id": ROBLOX_UNIVERSE_ID or None,
        "place_id": ROBLOX_PLACE_ID or None,
        "api_key_present": bool(ROBLOX_API_KEY),
        "api": "Roblox Open Cloud v2",
    }


def roblox_action(data):
    action = str(data.get("action") or "status").strip()
    if action == "status":
        return roblox_status()
    if action == "list_data_stores":
        size = max(1, min(int(data.get("max_page_size") or 25), 100))
        return roblox_call(f"universes/{_u()}/data-stores?maxPageSize={size}")
    if action == "get_entry":
        ds = urllib.parse.quote(str(data.get("data_store_id") or ""), safe="")
        eid = urllib.parse.quote(str(data.get("entry_id") or ""), safe="")
        return roblox_call(f"universes/{_u()}/data-stores/{ds}/entries/{eid}")
    if action == "set_entry":
        ds = urllib.parse.quote(str(data.get("data_store_id") or ""), safe="")
        eid = urllib.parse.quote(str(data.get("entry_id") or ""), safe="")
        value = data.get("value")
        return roblox_call(
            f"universes/{_u()}/data-stores/{ds}/entries/{eid}",
            "PATCH",
            {"value": json.dumps(value, separators=(",", ":"))},
        )
    if action == "delete_entry":
        ds = urllib.parse.quote(str(data.get("data_store_id") or ""), safe="")
        eid = urllib.parse.quote(str(data.get("entry_id") or ""), safe="")
        return roblox_call(f"universes/{_u()}/data-stores/{ds}/entries/{eid}", "DELETE")
    if action == "increment_entry":
        ds = urllib.parse.quote(str(data.get("data_store_id") or ""), safe="")
        eid = urllib.parse.quote(str(data.get("entry_id") or ""), safe="")
        return roblox_call(
            f"universes/{_u()}/data-stores/{ds}/entries/{eid}:increment",
            "POST",
            {"increment": float(data.get("increment") or 1)},
        )
    if action == "publish_message":
        topic = urllib.parse.quote(str(data.get("topic") or "mcp"), safe="")
        return roblox_call(
            f"universes/{_u()}/messaging-service/topics/{topic}/messages",
            "POST",
            {"message": str(data.get("message") or "")[:1000]},
        )
    if action == "list_instances":
        place = _place(data.get("place_id"))
        if not place:
            return {"status": "error", "message": "ROBLOX_PLACE_ID missing"}
        iid = urllib.parse.quote(str(data.get("instance_id") or "root"), safe="")
        return roblox_call(f"universes/{_u()}/places/{place}/instances/{iid}/list-children")
    if action == "get_instance":
        place = _place(data.get("place_id"))
        iid = urllib.parse.quote(str(data.get("instance_id") or ""), safe="")
        if not place or not iid:
            return {"status": "error", "message": "place_id and instance_id required"}
        return roblox_call(f"universes/{_u()}/places/{place}/instances/{iid}")
    if action == "update_script":
        place = _place(data.get("place_id"))
        iid = urllib.parse.quote(str(data.get("instance_id") or ""), safe="")
        source = str(data.get("source") or "")
        if len(source.encode("utf-8")) > 200 * 1024:
            return {"status": "error", "message": "Script source over 200 KB"}
        return roblox_call(
            f"universes/{_u()}/places/{place}/instances/{iid}",
            "PATCH",
            {"engineInstance": {"Details": {"Source": source}}},
        )
    return {"status": "error", "message": "Unknown Roblox action: " + action}


def turbowarp_js(server: str) -> str:
    return """(function(Scratch) {
  "use strict";
  const SERVER = "__SERVER__";
  function vm() {
    return Scratch.vm || window.vm || null;
  }
  function sprites() {
    const v = vm();
    if (!v || !v.runtime) return [];
    return v.runtime.targets.filter(function(t) { return t && !t.isStage; });
  }
  function findSprite(name) {
    name = String(name || "");
    return sprites().filter(function(t) { return t.sprite && t.sprite.name === name; })[0] || null;
  }
  const ext = {
    _last: "",
    getInfo() {
      return {
        id: "feranmimcp",
        name: "Feranmi MCP",
        color1: "#5b4bff",
        blocks: [
          {opcode:"connect", blockType:Scratch.BlockType.COMMAND, text:"MCP connect"},
          {opcode:"spriteCount", blockType:Scratch.BlockType.REPORTER, text:"sprite count"},
          {opcode:"listSprites", blockType:Scratch.BlockType.REPORTER, text:"list sprites"},
          {opcode:"spriteX", blockType:Scratch.BlockType.REPORTER, text:"x of [NAME]", arguments:{NAME:{type:Scratch.ArgumentType.STRING, defaultValue:"Sprite1"}}},
          {opcode:"spriteY", blockType:Scratch.BlockType.REPORTER, text:"y of [NAME]", arguments:{NAME:{type:Scratch.ArgumentType.STRING, defaultValue:"Sprite1"}}},
          {opcode:"moveSprite", blockType:Scratch.BlockType.COMMAND, text:"move [NAME] to x [X] y [Y]", arguments:{NAME:{type:Scratch.ArgumentType.STRING, defaultValue:"Sprite1"}, X:{type:Scratch.ArgumentType.NUMBER, defaultValue:0}, Y:{type:Scratch.ArgumentType.NUMBER, defaultValue:0}}},
          {opcode:"broadcast", blockType:Scratch.BlockType.COMMAND, text:"broadcast [MSG]", arguments:{MSG:{type:Scratch.ArgumentType.STRING, defaultValue:"go"}}},
          {opcode:"postChat", blockType:Scratch.BlockType.COMMAND, text:"post to team chat [MSG]", arguments:{MSG:{type:Scratch.ArgumentType.STRING, defaultValue:"hello from turbowarp"}}}
        ]
      };
    },
    connect() {
      return Scratch.fetch(SERVER + "/api/turbowarp", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({action:"status"})})
        .then(r => r.json()).then(x => { this._last = JSON.stringify(x); });
    },
    spriteCount() { return sprites().length; },
    listSprites() { return sprites().map(function(t) { return t.sprite.name; }).join(", "); },
    spriteX(args) { const t = findSprite(args.NAME); return t ? t.x : 0; },
    spriteY(args) { const t = findSprite(args.NAME); return t ? t.y : 0; },
    moveSprite(args) {
      const t = findSprite(args.NAME);
      if (!t) return;
      t.setXY(Number(args.X) || 0, Number(args.Y) || 0);
    },
    broadcast(args) {
      const v = vm();
      if (v && v.runtime) v.runtime.startHats("event_whenbroadcastreceived", {BROADCAST_OPTION: String(args.MSG || "")});
    },
    postChat(args) {
      return Scratch.fetch(SERVER + "/talk", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({from:"turbowarp", to:"all", text:String(args.MSG || "")})});
    }
  };
  Scratch.extensions.register(ext);
})(Scratch);""".replace("__SERVER__", server)
