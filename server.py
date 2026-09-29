"""
Blender MCP Server (Peak modeling upgrade)
==========================================

Same architecture as before:

  AI  --MCP stdio/http-->  this server  --TCP 9876-->  blender_mcp_addon.py  --main thread-->  Blender

Do not replace the addon. New tools run through existing commands
(`execute_bpy_script`, `inspect_object`, `get_scene_summary`, etc.).

Env:
  BLENDER_HOST          default localhost
  BLENDER_PORT          default 9876
  BLENDER_MCP_TIMEOUT   default 45
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from mcp.server.mcpserver import MCPServer

BLENDER_HOST = os.environ.get("BLENDER_HOST", "localhost")
BLENDER_PORT = int(os.environ.get("BLENDER_PORT", "9876"))
SOCKET_TIMEOUT = float(os.environ.get("BLENDER_MCP_TIMEOUT", "45"))
GODOT_HOST = os.environ.get("GODOT_HOST", "127.0.0.1")
GODOT_PORT = int(os.environ.get("GODOT_PORT", "9877"))
SITE_HOST = os.environ.get("SITE_HOST", "0.0.0.0")
SITE_PORT = int(os.environ.get("SITE_PORT", "8001"))
SITE_TOKEN = os.environ.get("SITE_TOKEN", "").strip()
PUBLIC_MCP_URL = (os.environ.get("PUBLIC_MCP_URL", "").strip() or os.environ.get("RENDER_EXTERNAL_URL", "").strip())
TALK_FILE = os.environ.get(
    "TALK_FILE",
    os.path.join(tempfile.gettempdir(), "feranmi_mcp_talk.json"),
).replace("\\", "/")
INTERNAL_MCP_PORT = int(os.environ.get("INTERNAL_MCP_PORT", "8020"))

mcp = MCPServer("blender-mcp")
_blender_io = threading.Lock()


class BlenderConnectionError(Exception):
    pass


def send_command(command_type: str, params: Optional[dict] = None) -> dict:
    payload = json.dumps({"type": command_type, "params": params or {}}).encode("utf-8")
    try:
        with _blender_io:
            with socket.create_connection((BLENDER_HOST, BLENDER_PORT), timeout=SOCKET_TIMEOUT) as sock:
                sock.settimeout(SOCKET_TIMEOUT)
                sock.sendall(payload)
                try:
                    sock.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
                chunks = []
                while True:
                    try:
                        data = sock.recv(65536)
                    except socket.timeout:
                        raise BlenderConnectionError(
                            f"Timed out waiting for Blender to respond to '{command_type}' "
                            f"after {SOCKET_TIMEOUT}s."
                        )
                    if not data:
                        break
                    chunks.append(data)
                raw = b"".join(chunks)
                if not raw:
                    raise BlenderConnectionError(
                        "Blender addon closed the connection without sending a response."
                    )
                return json.loads(raw.decode("utf-8"))
    except ConnectionRefusedError as exc:
        raise BlenderConnectionError(
            f"Could not connect to Blender at {BLENDER_HOST}:{BLENDER_PORT}. "
            "Start Blender, enable the MCP Bridge addon, and click Start Server "
            "in View3D > Sidebar (N) > MCP."
        ) from exc
    except socket.gaierror as exc:
        raise BlenderConnectionError(f"Could not resolve host '{BLENDER_HOST}': {exc}") from exc


def call_blender(command_type: str, params: Optional[dict] = None) -> dict:
    try:
        response = send_command(command_type, params)
    except BlenderConnectionError as exc:
        return {"status": "error", "message": str(exc)}
    except Exception as exc:
        return {"status": "error", "message": f"Unexpected client-side error: {exc}"}
    if not isinstance(response, dict) or "status" not in response:
        return {"status": "error", "message": f"Malformed response from Blender: {response!r}"}
    return response


def run_bpy(script_code: str) -> dict:
    return call_blender("execute_bpy_script", {"script_code": script_code})


def _ok(data: Any) -> dict:
    return {"status": "success", "result": data}


WATCH_DIR = os.path.join(tempfile.gettempdir(), "blender_mcp_watch").replace("\\", "/")
_watch_lock = threading.Lock()
_watch_state = {
    "running": False,
    "stop": threading.Event(),
    "thread": None,
    "interval": 1.0,
    "camera_name": None,
    "max_frames": 60,
    "frames": [],
    "latest": None,
    "error": None,
    "use_camera_render": True,
}


def _encode_png(path: str) -> dict:
    info = {"filepath": path, "exists": os.path.exists(path)}
    if not info["exists"]:
        return info
    info["file_size_bytes"] = os.path.getsize(path)
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
        info["image_base64"] = base64.b64encode(raw).decode("ascii")
        info["data_url"] = f"data:image/png;base64,{info['image_base64']}"
    except Exception as exc:
        info["base64_error"] = str(exc)
    return info


def _capture_scene_frame(filepath: str, camera_name: Optional[str], use_camera_render: bool) -> dict:
    """Capture the 3D scene only — camera/GL render, not the Blender app window."""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    if use_camera_render:
        script = f"""
import bpy
scene = bpy.context.scene
cam_name = {camera_name!r}
if cam_name:
    cam = bpy.data.objects.get(cam_name)
    if cam and cam.type == "CAMERA":
        scene.camera = cam
if not scene.camera:
    raise ValueError("No camera in the scene. Add a camera or pass camera_name.")
scene.render.filepath = {filepath!r}
scene.render.image_settings.file_format = "PNG"
scene.render.image_settings.color_mode = "RGB"
bpy.ops.render.render(write_still=True)
print(scene.render.filepath)
"""
        result = run_bpy(script)
        if result.get("status") not in {"success", "ok"}:
            return result
        return {"status": "success", "result": {"filepath": filepath, "kind": "camera_render"}}
    return call_blender("capture_viewport_screenshot", {
        "filepath": filepath,
        "camera_name": camera_name,
    })


def _watch_loop():
    frame_index = 0
    while not _watch_state["stop"].wait(_watch_state["interval"]):
        if not _watch_state["running"]:
            break
        if frame_index >= int(_watch_state["max_frames"]):
            _watch_state["running"] = False
            break
        path = os.path.join(WATCH_DIR, f"frame_{frame_index:04d}.png").replace("\\", "/")
        try:
            captured = _capture_scene_frame(
                path,
                _watch_state["camera_name"],
                _watch_state["use_camera_render"],
            )
            if captured.get("status") not in {"success", "ok"}:
                _watch_state["error"] = captured.get("message") or str(captured)
                continue
            actual = path
            result = captured.get("result")
            if isinstance(result, dict) and result.get("filepath"):
                actual = result["filepath"]
            with _watch_lock:
                _watch_state["frames"].append(actual)
                _watch_state["latest"] = actual
                _watch_state["error"] = None
            frame_index += 1
        except Exception as exc:
            _watch_state["error"] = str(exc)


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------

@mcp.tool()
def ping_blender() -> dict:
    """Check whether the Blender MCP Bridge addon is running and reachable."""
    result = call_blender("ping")
    if result.get("status") in {"success", "ok"}:
        result["bridge"] = {"host": BLENDER_HOST, "port": BLENDER_PORT, "timeout": SOCKET_TIMEOUT}
    return result


@mcp.tool()
def get_scene_summary() -> dict:
    """Overview of collections, objects, transforms, lights, camera, and selection."""
    return call_blender("get_scene_summary")


@mcp.tool()
def inspect_object(object_name: str) -> dict:
    """Detailed transform, bounds, modifiers, materials, and mesh counts for one object."""
    return call_blender("inspect_object", {"object_name": object_name})


@mcp.tool()
def list_objects(mesh_only: bool = False, collection_name: Optional[str] = None) -> dict:
    """List objects with type, location, dimensions, and collection names."""
    return run_bpy(f"""
import bpy, json
from mathutils import Vector
mesh_only = {mesh_only}
col_name = {collection_name!r}
objs = list(bpy.data.collections[col_name].objects) if col_name and col_name in bpy.data.collections else list(bpy.context.scene.objects)
out = []
for obj in objs:
    if mesh_only and obj.type != "MESH":
        continue
    dims = list(obj.dimensions) if obj.type == "MESH" else [0, 0, 0]
    out.append({{
        "name": obj.name,
        "type": obj.type,
        "location": [round(v, 4) for v in obj.location],
        "rotation_mode": obj.rotation_mode,
        "rotation_euler_deg": [round(v * 57.2957795, 3) for v in obj.rotation_euler],
        "scale": [round(v, 4) for v in obj.scale],
        "dimensions": [round(v, 4) for v in dims],
        "collections": [c.name for c in obj.users_collection],
        "parent": obj.parent.name if obj.parent else None,
        "hidden": obj.hide_get(),
    }})
print(json.dumps({{"count": len(out), "objects": out}}))
""")


@mcp.tool()
def scene_health() -> dict:
    """Polycount, loose objects, missing camera, and oversized meshes. Use before heavy edits."""
    return run_bpy("""
import bpy, json
meshes = [o for o in bpy.data.objects if o.type == "MESH"]
verts = edges = faces = 0
heavy = []
for o in meshes:
    v, e, f = len(o.data.vertices), len(o.data.edges), len(o.data.polygons)
    verts += v; edges += e; faces += f
    if v > 200000:
        heavy.append({"name": o.name, "vertices": v, "faces": f})
print(json.dumps({
    "objects": len(bpy.data.objects),
    "meshes": len(meshes),
    "vertices": verts,
    "edges": edges,
    "faces": faces,
    "heavy_meshes": heavy,
    "has_camera": bool(bpy.context.scene.camera),
    "lights": sum(1 for o in bpy.data.objects if o.type == "LIGHT"),
    "materials": len(bpy.data.materials),
}))
""")


# ---------------------------------------------------------------------------
# Visual feedback
# ---------------------------------------------------------------------------

@mcp.tool()
def capture_viewport_screenshot(
    filepath: Optional[str] = None,
    camera_name: Optional[str] = None,
    include_base64: bool = False,
) -> dict:
    """Capture viewport or camera PNG. Base64 is off by default to keep responses small."""
    if not filepath:
        filepath = os.path.join(tempfile.gettempdir(), "blender_mcp_review.png").replace("\\", "/")
    res = call_blender("capture_viewport_screenshot", {
        "filepath": filepath,
        "camera_name": camera_name,
    })
    if res.get("status") not in {"success", "ok"}:
        return res
    result_data = res.get("result", {}) if isinstance(res.get("result"), dict) else {}
    actual_path = result_data.get("filepath", filepath)
    if include_base64 and os.path.exists(actual_path):
        try:
            with open(actual_path, "rb") as handle:
                raw_bytes = handle.read()
            b64_str = base64.b64encode(raw_bytes).decode("ascii")
            result_data["image_base64"] = b64_str
            result_data["data_url"] = f"data:image/png;base64,{b64_str}"
            result_data["file_size_bytes"] = len(raw_bytes)
        except Exception as exc:
            result_data["base64_error"] = f"Failed to encode image: {exc}"
    result_data["filepath"] = actual_path
    return {"status": "success", "result": result_data}


@mcp.tool()
def capture_scene_frame(
    camera_name: Optional[str] = None,
    include_base64: bool = False,
    use_camera_render: bool = True,
) -> dict:
    """Capture the 3D scene through the camera. This is the picture of the model, not the Blender window UI."""
    os.makedirs(WATCH_DIR, exist_ok=True)
    path = os.path.join(WATCH_DIR, f"single_{int(time.time())}.png").replace("\\", "/")
    captured = _capture_scene_frame(path, camera_name, use_camera_render)
    if captured.get("status") not in {"success", "ok"}:
        return captured
    result = captured.get("result") if isinstance(captured.get("result"), dict) else {"filepath": path}
    if include_base64:
        result.update(_encode_png(result.get("filepath", path)))
    with _watch_lock:
        _watch_state["latest"] = result.get("filepath", path)
    return {"status": "success", "result": result}


@mcp.tool()
def start_visual_watch(
    interval_seconds: float = 1.0,
    camera_name: Optional[str] = None,
    max_frames: int = 30,
    use_camera_render: bool = True,
) -> dict:
    """Start capturing the 3D scene every second (or interval_seconds).

    This records the model through the camera, not a video of the Blender app chrome.
    The AI should poll get_latest_review_frame after edits.
    Full Cycles renders every second will freeze Blender; keep max_frames modest.
    """
    if _watch_state["running"]:
        return {"status": "error", "message": "Visual watch is already running. Call stop_visual_watch first."}
    os.makedirs(WATCH_DIR, exist_ok=True)
    _watch_state["stop"].clear()
    _watch_state["interval"] = max(0.25, float(interval_seconds))
    _watch_state["camera_name"] = camera_name
    _watch_state["max_frames"] = max(1, min(int(max_frames), 120))
    _watch_state["use_camera_render"] = bool(use_camera_render)
    _watch_state["frames"] = []
    _watch_state["latest"] = None
    _watch_state["error"] = None
    _watch_state["running"] = True
    thread = threading.Thread(target=_watch_loop, name="blender-mcp-watch", daemon=True)
    _watch_state["thread"] = thread
    thread.start()
    return _ok({
        "running": True,
        "interval_seconds": _watch_state["interval"],
        "max_frames": _watch_state["max_frames"],
        "folder": WATCH_DIR,
        "kind": "camera_render" if use_camera_render else "viewport_gl",
    })


@mcp.tool()
def stop_visual_watch() -> dict:
    """Stop the repeating scene capture."""
    _watch_state["running"] = False
    _watch_state["stop"].set()
    thread = _watch_state.get("thread")
    if thread and thread.is_alive():
        thread.join(timeout=2.0)
    return _ok({
        "running": False,
        "frames": list(_watch_state["frames"]),
        "latest": _watch_state["latest"],
        "error": _watch_state["error"],
    })


@mcp.tool()
def get_latest_review_frame(include_base64: bool = False) -> dict:
    """Return the newest captured scene frame so the AI can check accuracy."""
    path = _watch_state.get("latest")
    if not path:
        return {"status": "error", "message": "No review frame yet. Call capture_scene_frame or start_visual_watch."}
    result = {
        "filepath": path,
        "running": _watch_state["running"],
        "frame_count": len(_watch_state["frames"]),
        "error": _watch_state["error"],
    }
    if include_base64:
        result.update(_encode_png(path))
    return _ok(result)


@mcp.tool()
def list_review_frames() -> dict:
    """List all frames captured by the visual watch."""
    return _ok({
        "folder": WATCH_DIR,
        "frames": list(_watch_state["frames"]),
        "latest": _watch_state["latest"],
        "running": _watch_state["running"],
        "error": _watch_state["error"],
    })


@mcp.tool()
def make_review_video(fps: int = 4, output_path: Optional[str] = None) -> dict:
    """Stitch watched frames into an MP4 of the 3D scene. Needs ffmpeg on PATH."""
    frames = list(_watch_state["frames"])
    if len(frames) < 2:
        return {"status": "error", "message": "Need at least 2 frames. Start visual watch first."}
    out = output_path or os.path.join(WATCH_DIR, "review.mp4").replace("\\", "/")
    concat = os.path.join(WATCH_DIR, "frames.txt").replace("\\", "/")
    with open(concat, "w", encoding="utf-8") as handle:
        for frame in frames:
            handle.write(f"file '{frame.replace(chr(39), chr(39)+chr(39))}'\n")
            handle.write(f"duration {1.0 / max(1, int(fps))}\n")
        handle.write(f"file '{frames[-1].replace(chr(39), chr(39)+chr(39))}'\n")
    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat,
        "-vsync", "vfr", "-pix_fmt", "yuv420p", out,
    ]
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        return {"status": "error", "message": "ffmpeg is not installed. Frames are still in " + WATCH_DIR}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}
    if completed.returncode != 0:
        return {"status": "error", "message": completed.stderr[-500:] or "ffmpeg failed"}
    return _ok({"video": out, "frames": len(frames), "fps": fps})


@mcp.tool()
def frame_and_render(
    target_object: Optional[str] = None,
    view_angle: str = "three_quarters",
    distance_multiplier: float = 1.6,
    resolution: Optional[List[int]] = None,
    include_base64: bool = False,
) -> dict:
    """Frame an object or the scene, render a review camera, and return the image path."""
    res_w = resolution[0] if resolution and len(resolution) >= 2 else 1024
    res_h = resolution[1] if resolution and len(resolution) >= 2 else 1024
    script = f"""
import bpy, mathutils
scene = bpy.context.scene
scene.render.resolution_x = {res_w}
scene.render.resolution_y = {res_h}
cam = scene.camera
if not cam:
    cam_data = bpy.data.cameras.new("ReviewCamera")
    cam = bpy.data.objects.new("ReviewCamera", cam_data)
    scene.collection.objects.link(cam)
    scene.camera = cam
target_name = {target_object!r}
target = bpy.data.objects.get(target_name) if target_name else None
if target:
    bbox_corners = [target.matrix_world @ mathutils.Vector(corner) for corner in target.bound_box]
    center = sum(bbox_corners, mathutils.Vector((0, 0, 0))) / 8.0
    dims = target.dimensions
    max_dim = max(dims.x, dims.y, dims.z, 0.5)
else:
    center = mathutils.Vector((0, 0, 1.0))
    max_dim = 2.0
dist = max_dim * {distance_multiplier}
angle = {view_angle!r}
if angle == "front":
    cam_pos = center + mathutils.Vector((0.0, -dist, max_dim * 0.2))
elif angle == "side":
    cam_pos = center + mathutils.Vector((dist, 0.0, max_dim * 0.2))
elif angle == "top":
    cam_pos = center + mathutils.Vector((0.0, 0.0, dist * 1.2))
elif angle == "close_up":
    cam_pos = center + mathutils.Vector((dist * 0.4, -dist * 0.6, max_dim * 0.3))
else:
    cam_pos = center + mathutils.Vector((dist * 0.7, -dist * 0.7, max_dim * 0.4))
cam.location = cam_pos
direction = center - cam.location
cam.rotation_euler = direction.to_track_quat('-Z', 'Y').to_euler()
print("camera_ready")
"""
    exec_res = run_bpy(script)
    if exec_res.get("status") not in {"success", "ok"}:
        return exec_res
    return capture_viewport_screenshot(camera_name="ReviewCamera", include_base64=include_base64)


# ---------------------------------------------------------------------------
# Import / export
# ---------------------------------------------------------------------------

@mcp.tool()
def import_model(
    source: str,
    format: str = "auto",
    location: Optional[List[float]] = None,
    scale: Optional[List[float]] = None,
    rotation: Optional[List[float]] = None,
    auto_center: bool = True,
) -> dict:
    """Import GLB/GLTF/OBJ/FBX/STL from a local path or URL."""
    local_path = source
    if source.startswith("http://") or source.startswith("https://"):
        try:
            parsed = urllib.parse.urlparse(source)
            ext = os.path.splitext(parsed.path)[1] or ".glb"
            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
            temp_file.close()
            local_path = temp_file.name
            urllib.request.urlretrieve(source, local_path)
        except Exception as exc:
            return {"status": "error", "message": f"Failed to download model from {source}: {exc}"}
    loc = location or [0.0, 0.0, 0.0]
    sc = scale or [1.0, 1.0, 1.0]
    rot = rotation or [0.0, 0.0, 0.0]
    norm_path = local_path.replace("\\", "/")
    return run_bpy(f"""
import bpy, os, math
filepath = {norm_path!r}
fmt = {format!r}.lower()
if fmt == "auto":
    fmt = os.path.splitext(filepath)[1].lower().replace(".", "")
before = set(bpy.data.objects)
if fmt in ("glb", "gltf"):
    bpy.ops.import_scene.gltf(filepath=filepath)
elif fmt == "obj":
    try:
        bpy.ops.wm.obj_import(filepath=filepath)
    except Exception:
        bpy.ops.import_scene.obj(filepath=filepath)
elif fmt == "fbx":
    bpy.ops.import_scene.fbx(filepath=filepath)
elif fmt == "stl":
    try:
        bpy.ops.wm.stl_import(filepath=filepath)
    except Exception:
        bpy.ops.import_mesh.stl(filepath=filepath)
else:
    raise ValueError(f"Unsupported model format: {{fmt}}")
new_objs = list(set(bpy.data.objects) - before)
if new_objs:
    bpy.ops.object.select_all(action="DESELECT")
    root = new_objs[0]
    for obj in new_objs:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = root
    if {auto_center}:
        bpy.ops.object.origin_set(type="ORIGIN_GEOMETRY", center="BOUNDS")
    root.location = {loc}
    root.scale = {sc}
    root.rotation_mode = "XYZ"
    root.rotation_euler = [math.radians(r) for r in {rot}]
print("imported", len(new_objs), "root", new_objs[0].name if new_objs else None)
""")


@mcp.tool()
def export_scene(filepath: str, format: str, selected_only: bool = False) -> dict:
    """Export scene or selection to GLTF, GLB, FBX, or OBJ."""
    return call_blender("export_scene", {
        "filepath": filepath,
        "format": format,
        "selected_only": selected_only,
    })


# ---------------------------------------------------------------------------
# Blockout / primitives
# ---------------------------------------------------------------------------

@mcp.tool()
def create_primitive(
    type: str,
    location: Optional[List[float]] = None,
    scale: Optional[List[float]] = None,
    rotation: Optional[List[float]] = None,
    name: Optional[str] = None,
) -> dict:
    """Create CUBE, UV_SPHERE, CYLINDER, PLANE, TORUS, or CONE."""
    params = {
        "type": type,
        "location": location or [0.0, 0.0, 0.0],
        "scale": scale or [1.0, 1.0, 1.0],
        "rotation": rotation or [0.0, 0.0, 0.0],
    }
    if name:
        params["name"] = name
    return call_blender("create_primitive", params)


@mcp.tool()
def create_box(name: str, x: float, y: float, z: float, width: float, depth: float, height: float) -> dict:
    """Create a construction box with world size. Origin sits on the ground plane of the box."""
    return run_bpy(f"""
import bpy
if bpy.data.objects.get({name!r}):
    raise ValueError("Object already exists")
bpy.ops.mesh.primitive_cube_add(location=({x}, {y}, {z} + {height}/2))
obj = bpy.context.object
obj.name = {name!r}
obj.dimensions = ({width}, {depth}, {height})
bpy.context.view_layer.update()
print(obj.name)
""")


# ---------------------------------------------------------------------------
# Transforms that respect rotation_mode
# ---------------------------------------------------------------------------

@mcp.tool()
def transform_object(
    object_name: str,
    location: Optional[List[float]] = None,
    rotation: Optional[List[float]] = None,
    scale: Optional[List[float]] = None,
    relative: bool = False,
) -> dict:
    """Move, rotate (degrees), or scale an object. Rotation keeps the current rotation_mode."""
    return run_bpy(f"""
import bpy, math
from mathutils import Euler, Vector
obj = bpy.data.objects.get({object_name!r})
if not obj:
    raise ValueError("Object not found")
rel = {relative}
loc = {location}
if loc is not None:
    obj.location = obj.location + Vector(loc) if rel else Vector(loc)
rot = {rotation}
if rot is not None:
    requested = Euler((math.radians(rot[0]), math.radians(rot[1]), math.radians(rot[2])), "XYZ")
    if rel:
        current = Euler(obj.rotation_euler, obj.rotation_mode if obj.rotation_mode not in ("QUATERNION", "AXIS_ANGLE") else "XYZ")
        requested = Euler((current.x + requested.x, current.y + requested.y, current.z + requested.z), "XYZ")
    mode = obj.rotation_mode
    if mode == "QUATERNION":
        obj.rotation_quaternion = requested.to_quaternion()
    elif mode == "AXIS_ANGLE":
        axis, angle = requested.to_quaternion().to_axis_angle()
        obj.rotation_axis_angle = (angle, axis.x, axis.y, axis.z)
    else:
        obj.rotation_euler = requested
sc = {scale}
if sc is not None:
    if rel:
        obj.scale.x *= sc[0]; obj.scale.y *= sc[1]; obj.scale.z *= sc[2]
    else:
        obj.scale = Vector(sc)
bpy.context.view_layer.update()
print("transformed", obj.name, obj.rotation_mode)
""")


@mcp.tool()
def ground_object(object_name: str, z: float = 0.0) -> dict:
    """Sit the object's bounding box on z (default 0)."""
    return run_bpy(f"""
import bpy
from mathutils import Vector
obj = bpy.data.objects.get({object_name!r})
if not obj:
    raise ValueError("Object not found")
corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
lowest = min(v.z for v in corners)
obj.location.z += ({z} - lowest)
print(obj.location[:])
""")


@mcp.tool()
def snap_to_grid(object_name: str, size: float = 1.0, snap_z: bool = False) -> dict:
    """Snap object X/Y (and optional Z) to a grid."""
    return run_bpy(f"""
import bpy
obj = bpy.data.objects.get({object_name!r})
if not obj:
    raise ValueError("Object not found")
size = float({size}) or 1.0
obj.location.x = round(obj.location.x / size) * size
obj.location.y = round(obj.location.y / size) * size
if {snap_z}:
    obj.location.z = round(obj.location.z / size) * size
print(list(obj.location))
""")


@mcp.tool()
def align_objects(object_names: List[str], axis: str = "x", mode: str = "min") -> dict:
    """Align objects on x, y, or z using min, max, or center of their bounding boxes."""
    return run_bpy(f"""
import bpy
from mathutils import Vector
names = {object_names!r}
axis = {axis!r}.lower()
mode = {mode!r}.lower()
idx = {{"x": 0, "y": 1, "z": 2}}[axis]
objs = [bpy.data.objects[n] for n in names if n in bpy.data.objects]
if not objs:
    raise ValueError("No objects")
def world_center(o):
    corners = [o.matrix_world @ Vector(c) for c in o.bound_box]
    return sum(corners, Vector((0,0,0))) / 8.0
values = [world_center(o)[idx] for o in objs]
if mode == "min":
    target = min(values)
elif mode == "max":
    target = max(values)
else:
    target = sum(values) / len(values)
for o in objs:
    delta = target - world_center(o)[idx]
    loc = list(o.location)
    loc[idx] += delta
    o.location = loc
print("aligned", [o.name for o in objs], "to", target)
""")


# ---------------------------------------------------------------------------
# Hierarchy / cleanup
# ---------------------------------------------------------------------------

@mcp.tool()
def duplicate_object(object_name: str, new_name: Optional[str] = None) -> dict:
    """Duplicate an object and its mesh data."""
    return run_bpy(f"""
import bpy
obj = bpy.data.objects.get({object_name!r})
if not obj:
    raise ValueError("Object not found")
copy = obj.copy()
if obj.data:
    copy.data = obj.data.copy()
bpy.context.collection.objects.link(copy)
if {new_name!r}:
    copy.name = {new_name!r}
print(copy.name)
""")


@mcp.tool()
def rename_object(object_name: str, new_name: str) -> dict:
    """Rename an object."""
    return run_bpy(f"""
import bpy
obj = bpy.data.objects.get({object_name!r})
if not obj:
    raise ValueError("Object not found")
obj.name = {new_name!r}
print(obj.name)
""")


@mcp.tool()
def delete_objects(object_names: List[str]) -> dict:
    """Delete named objects."""
    return run_bpy(f"""
import bpy
deleted = []
for name in {object_names!r}:
    obj = bpy.data.objects.get(name)
    if obj:
        bpy.data.objects.remove(obj, do_unlink=True)
        deleted.append(name)
print("deleted", deleted)
""")


@mcp.tool()
def parent_objects(child_names: List[str], parent_name: str, keep_transform: bool = True) -> dict:
    """Parent children to a parent object."""
    return run_bpy(f"""
import bpy
parent = bpy.data.objects.get({parent_name!r})
if not parent:
    raise ValueError("Parent not found")
for name in {child_names!r}:
    child = bpy.data.objects.get(name)
    if not child:
        continue
    if {keep_transform}:
        child.parent = parent
        child.matrix_parent_inverse = parent.matrix_world.inverted()
    else:
        child.parent = parent
print("parented to", parent.name)
""")


@mcp.tool()
def move_to_collection(object_names: List[str], collection_name: str) -> dict:
    """Move objects into a collection, creating it if needed."""
    return run_bpy(f"""
import bpy
col = bpy.data.collections.get({collection_name!r}) or bpy.data.collections.new({collection_name!r})
if col.name not in bpy.context.scene.collection.children:
    try:
        bpy.context.scene.collection.children.link(col)
    except RuntimeError:
        pass
for name in {object_names!r}:
    obj = bpy.data.objects.get(name)
    if not obj:
        continue
    for existing in list(obj.users_collection):
        existing.objects.unlink(obj)
    col.objects.link(obj)
print("moved", {object_names!r}, "->", col.name)
""")


@mcp.tool()
def clear_scene(keep_camera_and_lights: bool = True, collection_name: Optional[str] = None) -> dict:
    """Delete objects in the scene or one collection."""
    return run_bpy(f"""
import bpy
col_name = {collection_name!r}
keep = {keep_camera_and_lights}
objs = list(bpy.data.collections[col_name].objects) if col_name and col_name in bpy.data.collections else list(bpy.data.objects)
deleted = 0
for obj in objs:
    if keep and obj.type in ("CAMERA", "LIGHT"):
        continue
    bpy.data.objects.remove(obj, do_unlink=True)
    deleted += 1
print("deleted", deleted)
""")


# ---------------------------------------------------------------------------
# Mesh modeling
# ---------------------------------------------------------------------------

@mcp.tool()
def remesh_object(object_name: str, voxel_size: float = 0.03, adaptivity: float = 0.0, smooth_shading: bool = True) -> dict:
    """Voxel-remesh a mesh into one organic manifold surface."""
    return run_bpy(f"""
import bpy
obj = bpy.data.objects.get({object_name!r})
if not obj or obj.type != "MESH":
    raise ValueError("Mesh not found")
bpy.context.view_layer.objects.active = obj
obj.select_set(True)
bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
obj.data.remesh_voxel_size = {voxel_size}
obj.data.remesh_voxel_adaptivity = {adaptivity}
bpy.ops.object.voxel_remesh()
if {smooth_shading}:
    for poly in obj.data.polygons:
        poly.use_smooth = True
print("remeshed", obj.name, "verts", len(obj.data.vertices))
""")


@mcp.tool()
def join_objects(object_names: List[str], result_name: Optional[str] = None) -> dict:
    """Join meshes into one object."""
    return run_bpy(f"""
import bpy
objs = [bpy.data.objects.get(n) for n in {object_names!r}]
objs = [o for o in objs if o]
if not objs:
    raise ValueError("No matching objects")
bpy.ops.object.select_all(action="DESELECT")
active = objs[0]
bpy.context.view_layer.objects.active = active
for obj in objs:
    obj.select_set(True)
bpy.ops.object.join()
if {result_name!r}:
    active.name = {result_name!r}
print(active.name)
""")


@mcp.tool()
def boolean_operation(target_object: str, cutter_object: str, operation: str = "UNION", apply: bool = True) -> dict:
    """BOOLEAN UNION, DIFFERENCE, or INTERSECT."""
    return call_blender("apply_modifiers", {
        "object_name": target_object,
        "modifier_type": "BOOLEAN",
        "params": {"operation": operation, "object": cutter_object},
        "apply": apply,
    })


@mcp.tool()
def apply_modifiers(object_name: str, modifier_type: str, params: Optional[dict] = None, apply: bool = True) -> dict:
    """Add SUBSURF, BEVEL, BOOLEAN, MIRROR, ARRAY, or SOLIDIFY."""
    return call_blender("apply_modifiers", {
        "object_name": object_name,
        "modifier_type": modifier_type,
        "params": params or {},
        "apply": apply,
    })


@mcp.tool()
def extrude_faces(object_name: str, distance: float = 0.2, individual: bool = False) -> dict:
    """Extrude selected faces, or all faces if none are selected."""
    return run_bpy(f"""
import bpy, bmesh
obj = bpy.data.objects.get({object_name!r})
if not obj or obj.type != "MESH":
    raise ValueError("Mesh not found")
bpy.context.view_layer.objects.active = obj
obj.select_set(True)
if obj.mode != "OBJECT":
    bpy.ops.object.mode_set(mode="OBJECT")
bm = bmesh.new()
bm.from_mesh(obj.data)
faces = [f for f in bm.faces if f.select] or list(bm.faces)
ret = bmesh.ops.extrude_discrete_faces(bm, faces=faces) if {individual} else bmesh.ops.extrude_face_region(bm, geom=faces)
geom = ret.get("faces") or ret.get("geom") or []
verts = [g for g in geom if isinstance(g, bmesh.types.BMVert)]
if not verts:
    verts = [v for f in geom if hasattr(f, "verts") for v in f.verts]
normal = faces[0].normal.copy() if faces else obj.matrix_world.col[2].xyz
bmesh.ops.translate(bm, verts=verts, vec=normal.normalized() * {distance})
bm.to_mesh(obj.data)
bm.free()
obj.data.update()
print("extruded", obj.name)
""")


@mcp.tool()
def inset_faces(object_name: str, thickness: float = 0.05, depth: float = 0.0) -> dict:
    """Inset selected faces (or all faces)."""
    return run_bpy(f"""
import bpy, bmesh
obj = bpy.data.objects.get({object_name!r})
if not obj or obj.type != "MESH":
    raise ValueError("Mesh not found")
bm = bmesh.new()
bm.from_mesh(obj.data)
faces = [f for f in bm.faces if f.select] or list(bm.faces)
bmesh.ops.inset_region(bm, faces=faces, thickness={thickness}, depth={depth}, use_even_offset=True)
bm.to_mesh(obj.data)
bm.free()
obj.data.update()
print("inset", obj.name)
""")


@mcp.tool()
def bevel_object(object_name: str, width: float = 0.03, segments: int = 3, apply: bool = True) -> dict:
    """Bevel edges with a modifier."""
    return apply_modifiers(object_name, "BEVEL", {"width": width, "segments": segments}, apply)


@mcp.tool()
def subdivide_mesh(object_name: str, cuts: int = 1) -> dict:
    """Subdivide all faces."""
    return run_bpy(f"""
import bpy, bmesh
obj = bpy.data.objects.get({object_name!r})
if not obj or obj.type != "MESH":
    raise ValueError("Mesh not found")
bm = bmesh.new()
bm.from_mesh(obj.data)
bmesh.ops.subdivide_edges(bm, edges=bm.edges, cuts={int(cuts)})
bm.to_mesh(obj.data)
bm.free()
obj.data.update()
print("subdivided", obj.name, "verts", len(obj.data.vertices))
""")


@mcp.tool()
def shade_smooth(object_name: str, auto_smooth: bool = True, angle_degrees: float = 60.0) -> dict:
    """Enable smooth shading, optional auto-smooth angle."""
    return run_bpy(f"""
import bpy, math
obj = bpy.data.objects.get({object_name!r})
if not obj or obj.type != "MESH":
    raise ValueError("Mesh not found")
for poly in obj.data.polygons:
    poly.use_smooth = True
if hasattr(obj.data, "use_auto_smooth"):
    obj.data.use_auto_smooth = {auto_smooth}
    obj.data.auto_smooth_angle = math.radians({angle_degrees})
print("shade_smooth", obj.name)
""")


@mcp.tool()
def set_origin(object_name: str, mode: str = "GEOMETRY") -> dict:
    """Set origin: GEOMETRY, CURSOR, or BOTTOM."""
    return run_bpy(f"""
import bpy
from mathutils import Vector
obj = bpy.data.objects.get({object_name!r})
if not obj:
    raise ValueError("Object not found")
bpy.context.view_layer.objects.active = obj
obj.select_set(True)
mode = {mode!r}.upper()
if mode == "BOTTOM":
    bpy.ops.object.origin_set(type="ORIGIN_GEOMETRY", center="BOUNDS")
    corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    lowest = min(v.z for v in corners)
    world = obj.matrix_world.translation.copy()
    world.z = lowest
    local = obj.matrix_world.inverted() @ world
    obj.data.transform(obj.matrix_world.inverted() @ obj.matrix_world) if False else None
    obj.location = obj.location
    # shift mesh so origin sits at lowest world Z
    diff = obj.matrix_world.inverted() @ Vector((obj.matrix_world.translation.x, obj.matrix_world.translation.y, lowest))
    for v in obj.data.vertices:
        v.co.z -= (lowest - obj.matrix_world.translation.z) / max(obj.scale.z, 1e-6)
    obj.location.z = lowest
elif mode == "CURSOR":
    bpy.ops.object.origin_set(type="ORIGIN_CURSOR")
else:
    bpy.ops.object.origin_set(type="ORIGIN_GEOMETRY", center="BOUNDS")
obj.data.update()
print(list(obj.location))
""")


@mcp.tool()
def execute_bmesh_script(object_name: str, script: str) -> dict:
    """Run bmesh Python against one mesh."""
    return call_blender("execute_bmesh_script", {"object_name": object_name, "script": script})


@mcp.tool()
def execute_bpy_script(script_code: str) -> dict:
    """Run any Python on Blender's main thread. This is full Blender control."""
    return call_blender("execute_bpy_script", {"script_code": script_code})


@mcp.tool()
def blender_do_anything(python_code: str, active_object: Optional[str] = None, mode: Optional[str] = None) -> dict:
    """Do anything Blender's Python API can do.

    This is the full-control tool. Write normal Blender Python:
      import bpy
      bpy.ops.mesh.extrude_region_move(...)
      bpy.data.objects['Cube'].location.z = 2

    Optional:
      active_object: make this object active first
      mode: OBJECT, EDIT, SCULPT, POSE, WEIGHT_PAINT, TEXTURE_PAINT, VERTEX_PAINT
    """
    return run_bpy(f"""
import bpy
active_name = {active_object!r}
mode = {mode!r}
if active_name:
    obj = bpy.data.objects.get(active_name)
    if not obj:
        raise ValueError("Active object not found: " + active_name)
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    if mode:
        bpy.ops.object.mode_set(mode=mode)
elif mode:
    bpy.ops.object.mode_set(mode=mode)
# --- user code ---
{python_code}
""")


@mcp.tool()
def execute_operator(
    operator_id: str,
    params: Optional[dict] = None,
    active_object: Optional[str] = None,
    mode: Optional[str] = None,
    use_view3d: bool = True,
) -> dict:
    """Run any bpy.ops operator by id, e.g. 'mesh.extrude_region_move', 'object.shade_smooth', 'sculpt.dynamic_topology_toggle'.

    params is a JSON object of operator properties.
    use_view3d tries a 3D View context override so UI operators work without a click.
    """
    params_literal = json.dumps(params or {})
    return run_bpy(f"""
import bpy, json
op_id = {operator_id!r}
params = json.loads({params_literal!r})
active_name = {active_object!r}
mode = {mode!r}
if active_name:
    obj = bpy.data.objects.get(active_name)
    if not obj:
        raise ValueError("Active object not found")
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
if mode:
    bpy.ops.object.mode_set(mode=mode)
parts = op_id.split(".")
if len(parts) == 3 and parts[0] == "bpy" and parts[1] == "ops":
    parts = parts[2:]
if len(parts) != 2:
    raise ValueError("operator_id must look like 'object.modifier_add' or 'mesh.bisect'")
mod, name = parts
op = getattr(getattr(bpy.ops, mod), name)
if {use_view3d}:
    win = bpy.context.window
    screen = win.screen if win else None
    area = next((a for a in (screen.areas if screen else []) if a.type == "VIEW_3D"), None)
    region = next((r for r in (area.regions if area else []) if r.type == "WINDOW"), None)
    if area and region:
        with bpy.context.temp_override(window=win, area=area, region=region):
            result = op(**params)
            print(result)
    else:
        result = op(**params)
        print(result)
else:
    result = op(**params)
    print(result)
""")


@mcp.tool()
def set_mode(mode: str, object_name: Optional[str] = None) -> dict:
    """Switch Blender mode: OBJECT, EDIT, SCULPT, POSE, WEIGHT_PAINT, TEXTURE_PAINT, VERTEX_PAINT."""
    return run_bpy(f"""
import bpy
name = {object_name!r}
if name:
    obj = bpy.data.objects.get(name)
    if not obj:
        raise ValueError("Object not found")
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
bpy.ops.object.mode_set(mode={mode!r})
print(bpy.context.mode)
""")


@mcp.tool()
def select_objects(object_names: List[str], active: Optional[str] = None, exclusive: bool = True) -> dict:
    """Select objects. Optionally set the active object."""
    return run_bpy(f"""
import bpy
if {exclusive}:
    bpy.ops.object.select_all(action="DESELECT")
selected = []
for name in {object_names!r}:
    obj = bpy.data.objects.get(name)
    if not obj:
        continue
    obj.select_set(True)
    selected.append(obj.name)
active_name = {active!r} or (selected[0] if selected else None)
if active_name and active_name in bpy.data.objects:
    bpy.context.view_layer.objects.active = bpy.data.objects[active_name]
print("selected", selected, "active", active_name)
""")


@mcp.tool()
def get_data_path(path: str) -> dict:
    """Read any Blender RNA path, e.g. "bpy.data.objects['Cube'].location" or "bpy.context.scene.frame_current"."""
    return run_bpy(f"""
import bpy, json
value = eval({path!r}, {{"bpy": bpy}})
if hasattr(value, "to_list"):
    value = list(value)
elif hasattr(value, "__iter__") and not isinstance(value, (str, bytes, dict)):
    try:
        value = list(value)
    except Exception:
        value = str(value)
print(json.dumps({{"path": {path!r}, "value": value}}, default=str))
""")


@mcp.tool()
def set_data_path(path: str, value_python: str) -> dict:
    """Write any Blender RNA path. value_python is Python, e.g. "(0,0,2)" or "'Red'" or "True"."""
    return run_bpy(f"""
import bpy
path = {path!r}
value = eval({value_python!r}, {{"bpy": bpy}})
target = path
exec(target + " = value", {{"bpy": bpy, "value": value}})
print("set", path)
""")


@mcp.tool()
def search_operators(keyword: str, limit: int = 40) -> dict:
    """Search bpy.ops ids that contain a keyword, e.g. 'extrude', 'bevel', 'sculpt', 'cloth'."""
    return run_bpy(f"""
import bpy, json
keyword = {keyword!r}.lower()
limit = {int(limit)}
found = []
for mod_name in dir(bpy.ops):
    if mod_name.startswith("_"):
        continue
    mod = getattr(bpy.ops, mod_name)
    for op_name in dir(mod):
        if op_name.startswith("_"):
            continue
        op_id = f"{{mod_name}}.{{op_name}}"
        if keyword in op_id.lower():
            found.append(op_id)
            if len(found) >= limit:
                break
    if len(found) >= limit:
        break
print(json.dumps({{"matches": found}}))
""")


# ---------------------------------------------------------------------------
# Materials / lights
# ---------------------------------------------------------------------------

@mcp.tool()
def create_principled_material(
    name: str,
    color_hex: str = "#CCCCCC",
    metallic: float = 0.0,
    roughness: float = 0.5,
    emission_hex: Optional[str] = None,
    emission_strength: float = 0.0,
    object_name: Optional[str] = None,
) -> dict:
    """Create a Principled BSDF material and optionally assign it."""
    return call_blender("create_principled_material", {
        "name": name,
        "color_hex": color_hex,
        "metallic": metallic,
        "roughness": roughness,
        "emission_hex": emission_hex,
        "emission_strength": emission_strength,
        "object_name": object_name,
    })


@mcp.tool()
def create_toon_material(
    name: str,
    base_color_hex: str = "#E08544",
    shadow_color_hex: str = "#8B4513",
    highlight_color_hex: str = "#FFAA66",
    object_name: Optional[str] = None,
    outline: bool = True,
) -> dict:
    """Cel-shaded material with optional inverted-hull outline."""
    return run_bpy(f"""
import bpy
def hex_to_rgb(h):
    h = h.lstrip("#")
    return [int(h[i:i+2], 16)/255.0 for i in (0, 2, 4)] + [1.0]
mat = bpy.data.materials.new(name={name!r})
mat.use_nodes = True
nodes = mat.node_tree.nodes
links = mat.node_tree.links
nodes.clear()
diff = nodes.new("ShaderNodeDiffuseBSDF"); diff.location = (-400, 0)
s2rgb = nodes.new("ShaderNodeShaderToRGB"); s2rgb.location = (-200, 0)
ramp = nodes.new("ShaderNodeValToRGB"); ramp.location = (50, 0)
ramp.color_ramp.interpolation = "CONSTANT"
elements = ramp.color_ramp.elements
elements[0].position = 0.0
elements[0].color = hex_to_rgb({shadow_color_hex!r})
elements[1].position = 0.45
elements[1].color = hex_to_rgb({base_color_hex!r})
high = ramp.color_ramp.elements.new(0.85)
high.color = hex_to_rgb({highlight_color_hex!r})
out = nodes.new("ShaderNodeOutputMaterial"); out.location = (350, 0)
links.new(diff.outputs["BSDF"], s2rgb.inputs["Shader"])
links.new(s2rgb.outputs["Color"], ramp.inputs["Fac"])
links.new(ramp.outputs["Color"], out.inputs["Surface"])
target_name = {object_name!r}
if target_name:
    obj = bpy.data.objects.get(target_name)
    if obj:
        if obj.data.materials:
            obj.data.materials[0] = mat
        else:
            obj.data.materials.append(mat)
        if {outline}:
            outline_mat = bpy.data.materials.get("Anime_Outline")
            if not outline_mat:
                outline_mat = bpy.data.materials.new(name="Anime_Outline")
                outline_mat.use_nodes = True
                outline_mat.use_backface_culling = True
                onodes = outline_mat.node_tree.nodes
                onodes.clear()
                emission = onodes.new("ShaderNodeEmission")
                emission.inputs["Color"].default_value = (0.05, 0.05, 0.05, 1.0)
                o_out = onodes.new("ShaderNodeOutputMaterial")
                outline_mat.node_tree.links.new(emission.outputs["Emission"], o_out.inputs["Surface"])
            if outline_mat.name not in [m.name for m in obj.data.materials if m]:
                obj.data.materials.append(outline_mat)
            mat_idx = len(obj.data.materials) - 1
            mod = obj.modifiers.new(name="AnimeOutline", type="SOLIDIFY")
            mod.thickness = -0.015
            mod.use_flip_normals = True
            mod.material_offset = mat_idx
print("toon", mat.name)
""")


@mcp.tool()
def setup_lighting_preset(preset: str = "three_point", energy_scale: float = 1.0, collection_name: str = "Studio_Lighting") -> dict:
    """Presets: three_point, studio_soft, anime_toon, dramatic_cinematic, cyberpunk."""
    return run_bpy(f"""
import bpy
col = bpy.data.collections.get({collection_name!r}) or bpy.data.collections.new({collection_name!r})
if col.name not in bpy.context.scene.collection.children:
    bpy.context.scene.collection.children.link(col)
for obj in list(col.objects):
    if obj.type == "LIGHT":
        bpy.data.objects.remove(obj, do_unlink=True)
preset = {preset!r}.lower()
scale = {energy_scale}
def make_light(name, ltype, loc, energy, color=(1,1,1)):
    ldata = bpy.data.lights.new(name=name, type=ltype)
    ldata.energy = energy * scale
    ldata.color = color
    lobj = bpy.data.objects.new(name=name, object_data=ldata)
    lobj.location = loc
    col.objects.link(lobj)
    return lobj
if preset == "three_point":
    make_light("Key_Light", "AREA", (4.0, -4.0, 5.0), 1200, (1.0, 0.98, 0.95))
    make_light("Fill_Light", "AREA", (-4.0, -2.5, 3.0), 450, (0.85, 0.92, 1.0))
    make_light("Rim_Light", "SPOT", (-2.0, 4.0, 6.0), 1800, (1.0, 0.95, 0.85))
elif preset == "anime_toon":
    make_light("Toon_Sun", "SUN", (3.0, -3.0, 6.0), 5.0 * scale, (1.0, 0.98, 0.9))
    make_light("Fill_Bounce", "AREA", (-3.0, -3.0, 2.0), 300, (0.9, 0.95, 1.0))
    make_light("Anime_Rim", "SPOT", (0.0, 4.0, 4.0), 1200, (1.0, 1.0, 1.0))
elif preset == "dramatic_cinematic":
    make_light("Key_Rim", "SPOT", (3.5, 3.5, 4.0), 2500, (1.0, 0.9, 0.8))
    make_light("Soft_Fill", "AREA", (-3.0, -3.0, 1.5), 200, (0.6, 0.7, 1.0))
elif preset == "cyberpunk":
    make_light("Neon_Cyan", "AREA", (4.0, -2.0, 3.0), 1500, (0.1, 0.8, 1.0))
    make_light("Neon_Magenta", "AREA", (-4.0, 2.0, 3.0), 1500, (1.0, 0.1, 0.7))
    make_light("Top_Rim", "SPOT", (0.0, 0.0, 6.0), 1000, (0.9, 0.9, 1.0))
else:
    make_light("Studio_Left", "AREA", (-4.0, -4.0, 4.0), 800, (1.0, 1.0, 1.0))
    make_light("Studio_Right", "AREA", (4.0, -4.0, 4.0), 800, (1.0, 1.0, 1.0))
    make_light("Overhead", "POINT", (0.0, 0.0, 5.0), 300, (1.0, 0.98, 0.95))
print("lighting", preset)
""")


@mcp.tool()
def add_light(type: str, location: Optional[List[float]] = None, energy: float = 1000.0, color_hex: str = "#FFFFFF", name: Optional[str] = None) -> dict:
    """Add POINT, SUN, SPOT, or AREA light."""
    return call_blender("add_light", {
        "type": type,
        "location": location or [0.0, 0.0, 3.0],
        "energy": energy,
        "color_hex": color_hex,
        "name": name,
    })


# ---------------------------------------------------------------------------
# Modeling workflow helper for the AI
# ---------------------------------------------------------------------------

@mcp.tool()
def apply_object_transforms(object_name: str, location: bool = False, rotation: bool = True, scale: bool = True) -> dict:
    """Apply location/rotation/scale so the mesh matches the object transform."""
    return run_bpy(f"""
import bpy
obj = bpy.data.objects.get({object_name!r})
if not obj:
    raise ValueError("Object not found")
bpy.context.view_layer.objects.active = obj
obj.select_set(True)
bpy.ops.object.transform_apply(location={location}, rotation={rotation}, scale={scale})
print("applied", obj.name)
""")


@mcp.tool()
def set_exact_dimensions(object_name: str, width: float, depth: float, height: float) -> dict:
    """Set world dimensions X/Y/Z in Blender units."""
    return run_bpy(f"""
import bpy
obj = bpy.data.objects.get({object_name!r})
if not obj:
    raise ValueError("Object not found")
obj.dimensions = ({width}, {depth}, {height})
bpy.context.view_layer.update()
print(list(obj.dimensions))
""")


@mcp.tool()
def measure_distance(object_a: str, object_b: str) -> dict:
    """World-space distance between two object origins."""
    return run_bpy(f"""
import bpy, json
a = bpy.data.objects.get({object_a!r})
b = bpy.data.objects.get({object_b!r})
if not a or not b:
    raise ValueError("Object not found")
delta = a.matrix_world.translation - b.matrix_world.translation
print(json.dumps({{"distance": delta.length, "delta": list(delta)}}))
""")


@mcp.tool()
def hide_objects(object_names: List[str], hide: bool = True) -> dict:
    """Hide or unhide objects in the viewport and render."""
    return run_bpy(f"""
import bpy
for name in {object_names!r}:
    obj = bpy.data.objects.get(name)
    if not obj:
        continue
    obj.hide_set({hide})
    obj.hide_render = {hide}
print("hide", {hide}, {object_names!r})
""")


@mcp.tool()
def isolate_object(object_name: str) -> dict:
    """Hide every other object so one mesh is easy to edit."""
    return run_bpy(f"""
import bpy
keep = bpy.data.objects.get({object_name!r})
if not keep:
    raise ValueError("Object not found")
for obj in bpy.context.scene.objects:
    obj.hide_set(obj.name != keep.name)
print("isolated", keep.name)
""")


@mcp.tool()
def reveal_all_objects() -> dict:
    """Unhide every object in the current scene."""
    return run_bpy("""
import bpy
for obj in bpy.context.scene.objects:
    obj.hide_set(False)
    obj.hide_render = False
print("revealed", len(bpy.context.scene.objects))
""")


@mcp.tool()
def mirror_object(object_name: str, axis: str = "x", apply: bool = False) -> dict:
    """Add a Mirror modifier on X, Y, or Z. Optionally apply it."""
    return run_bpy(f"""
import bpy
obj = bpy.data.objects.get({object_name!r})
if not obj:
    raise ValueError("Object not found")
axis = {axis!r}.lower()
mod = obj.modifiers.new("MCP_Mirror", "MIRROR")
mod.use_axis[0] = axis == "x"
mod.use_axis[1] = axis == "y"
mod.use_axis[2] = axis == "z"
mod.use_clip = True
if {apply}:
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.modifier_apply(modifier=mod.name)
print("mirrored", obj.name, axis)
""")


@mcp.tool()
def array_object(object_name: str, count: int = 3, offset: Optional[List[float]] = None, apply: bool = False) -> dict:
    """Repeat an object with an Array modifier."""
    off = offset or [2.0, 0.0, 0.0]
    return run_bpy(f"""
import bpy
obj = bpy.data.objects.get({object_name!r})
if not obj:
    raise ValueError("Object not found")
mod = obj.modifiers.new("MCP_Array", "ARRAY")
mod.count = {int(count)}
mod.use_relative_offset = False
mod.use_constant_offset = True
mod.constant_offset_displace = {off}
if {apply}:
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.modifier_apply(modifier=mod.name)
print("array", obj.name, mod.count)
""")


@mcp.tool()
def solidify_object(object_name: str, thickness: float = 0.05, apply: bool = False) -> dict:
    """Give a thin mesh real thickness."""
    return apply_modifiers(object_name, "SOLIDIFY", {"thickness": thickness}, apply)


@mcp.tool()
def decimate_object(object_name: str, ratio: float = 0.5, apply: bool = True) -> dict:
    """Reduce polygon count. ratio 0.5 keeps half the faces."""
    return run_bpy(f"""
import bpy
obj = bpy.data.objects.get({object_name!r})
if not obj or obj.type != "MESH":
    raise ValueError("Mesh not found")
mod = obj.modifiers.new("MCP_Decimate", "DECIMATE")
mod.ratio = max(0.01, min(1.0, {ratio}))
if {apply}:
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.modifier_apply(modifier=mod.name)
print("decimate", obj.name, len(obj.data.polygons))
""")


@mcp.tool()
def merge_by_distance(object_name: str, distance: float = 0.0001) -> dict:
    """Weld overlapping vertices."""
    return run_bpy(f"""
import bpy, bmesh
obj = bpy.data.objects.get({object_name!r})
if not obj or obj.type != "MESH":
    raise ValueError("Mesh not found")
bm = bmesh.new()
bm.from_mesh(obj.data)
bmesh.ops.remove_doubles(bm, verts=bm.verts, dist={distance})
bm.to_mesh(obj.data)
bm.free()
obj.data.update()
print("welded", obj.name, len(obj.data.vertices))
""")


@mcp.tool()
def recalc_normals(object_name: str, inside: bool = False) -> dict:
    """Recalculate face normals. inside=True flips them inward."""
    return run_bpy(f"""
import bpy, bmesh
obj = bpy.data.objects.get({object_name!r})
if not obj or obj.type != "MESH":
    raise ValueError("Mesh not found")
bm = bmesh.new()
bm.from_mesh(obj.data)
bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
if {inside}:
    bmesh.ops.reverse_faces(bm, faces=bm.faces)
bm.to_mesh(obj.data)
bm.free()
obj.data.update()
print("normals", obj.name)
""")


@mcp.tool()
def separate_loose_parts(object_name: str) -> dict:
    """Split one mesh into objects by loose parts."""
    return run_bpy(f"""
import bpy
obj = bpy.data.objects.get({object_name!r})
if not obj or obj.type != "MESH":
    raise ValueError("Mesh not found")
before = set(bpy.data.objects)
bpy.ops.object.select_all(action="DESELECT")
obj.select_set(True)
bpy.context.view_layer.objects.active = obj
bpy.ops.object.mode_set(mode="EDIT")
bpy.ops.mesh.separate(type="LOOSE")
bpy.ops.object.mode_set(mode="OBJECT")
created = [o.name for o in bpy.data.objects if o not in before]
print("parts", [obj.name] + created)
""")


@mcp.tool()
def bisect_object(object_name: str, axis: str = "x", clear_inner: bool = False, clear_outer: bool = False) -> dict:
    """Cut a mesh through its center on X, Y, or Z."""
    return run_bpy(f"""
import bpy, bmesh
from mathutils import Vector
obj = bpy.data.objects.get({object_name!r})
if not obj or obj.type != "MESH":
    raise ValueError("Mesh not found")
axis = {axis!r}.lower()
normal = {{"x": Vector((1,0,0)), "y": Vector((0,1,0)), "z": Vector((0,0,1))}}[axis]
bm = bmesh.new()
bm.from_mesh(obj.data)
plane = obj.matrix_world.inverted() @ obj.matrix_world.translation
bmesh.ops.bisect_plane(
    bm,
    geom=list(bm.verts) + list(bm.edges) + list(bm.faces),
    plane_co=plane,
    plane_no=obj.matrix_world.to_3x3().inverted() @ normal,
    clear_inner={clear_inner},
    clear_outer={clear_outer},
)
bm.to_mesh(obj.data)
bm.free()
obj.data.update()
print("bisect", obj.name, axis)
""")


@mcp.tool()
def smart_uv_unwrap(object_name: str, margin: float = 0.001) -> dict:
    """Smart UV project so textures can sit on the mesh."""
    return run_bpy(f"""
import bpy
obj = bpy.data.objects.get({object_name!r})
if not obj or obj.type != "MESH":
    raise ValueError("Mesh not found")
bpy.ops.object.select_all(action="DESELECT")
obj.select_set(True)
bpy.context.view_layer.objects.active = obj
bpy.ops.object.mode_set(mode="EDIT")
bpy.ops.mesh.select_all(action="SELECT")
bpy.ops.uv.smart_project(island_margin={margin})
bpy.ops.object.mode_set(mode="OBJECT")
print("uv", obj.name)
""")


@mcp.tool()
def create_text_object(name: str, text: str, size: float = 1.0, location: Optional[List[float]] = None) -> dict:
    """Create a 3D text object."""
    loc = location or [0.0, 0.0, 0.0]
    return run_bpy(f"""
import bpy
curve = bpy.data.curves.new({name!r}, type="FONT")
curve.body = {text!r}
curve.size = {size}
obj = bpy.data.objects.new({name!r}, curve)
obj.location = {loc}
bpy.context.collection.objects.link(obj)
print(obj.name)
""")


@mcp.tool()
def create_empty(name: str, location: Optional[List[float]] = None) -> dict:
    """Create an empty locator for parenting and alignment."""
    loc = location or [0.0, 0.0, 0.0]
    return run_bpy(f"""
import bpy
obj = bpy.data.objects.new({name!r}, None)
obj.empty_display_type = "ARROWS"
obj.location = {loc}
bpy.context.collection.objects.link(obj)
print(obj.name)
""")


@mcp.tool()
def look_at(object_name: str, target_name: str) -> dict:
    """Point an object so its -Z axis faces a target (cameras and lights included)."""
    return run_bpy(f"""
import bpy
obj = bpy.data.objects.get({object_name!r})
target = bpy.data.objects.get({target_name!r})
if not obj or not target:
    raise ValueError("Object not found")
direction = target.matrix_world.translation - obj.matrix_world.translation
obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
print(obj.name, "looks at", target.name)
""")


@mcp.tool()
def set_world_color(color_hex: str = "#202020", strength: float = 1.0) -> dict:
    """Set a flat world background color."""
    return run_bpy(f"""
import bpy
def hex_to_rgb(h):
    h = h.lstrip("#")
    return [int(h[i:i+2], 16)/255.0 for i in (0, 2, 4)]
world = bpy.context.scene.world or bpy.data.worlds.new("World")
bpy.context.scene.world = world
world.use_nodes = True
bg = world.node_tree.nodes.get("Background")
if bg:
    rgb = hex_to_rgb({color_hex!r})
    bg.inputs[0].default_value = (*rgb, 1.0)
    bg.inputs[1].default_value = {strength}
print("world", {color_hex!r})
""")


@mcp.tool()
def add_keyframe(object_name: str, frame: int, location: bool = True, rotation: bool = True, scale: bool = False) -> dict:
    """Insert a transform keyframe on an object."""
    return run_bpy(f"""
import bpy
obj = bpy.data.objects.get({object_name!r})
if not obj:
    raise ValueError("Object not found")
if {location}:
    obj.keyframe_insert(data_path="location", frame={int(frame)})
if {rotation}:
    path = "rotation_quaternion" if obj.rotation_mode == "QUATERNION" else "rotation_euler"
    obj.keyframe_insert(data_path=path, frame={int(frame)})
if {scale}:
    obj.keyframe_insert(data_path="scale", frame={int(frame)})
print("key", obj.name, {int(frame)})
""")


@mcp.tool()
def set_frame(frame: int) -> dict:
    """Jump the timeline to a frame."""
    return run_bpy(f"""
import bpy
bpy.context.scene.frame_set({int(frame)})
print(bpy.context.scene.frame_current)
""")


@mcp.tool()
def save_blend(filepath: Optional[str] = None) -> dict:
    """Save the current .blend. Uses the open file if filepath is omitted."""
    return run_bpy(f"""
import bpy
path = {filepath!r}
if path:
    bpy.ops.wm.save_as_mainfile(filepath=path.replace("\\\\", "/"))
else:
    bpy.ops.wm.save_mainfile()
print(bpy.data.filepath)
""")


@mcp.tool()
def undo_last() -> dict:
    """Undo the last Blender operator."""
    return run_bpy("""
import bpy
ok = bpy.ops.ed.undo()
print(ok)
""")


@mcp.tool()
def smooth_mesh(object_name: str, iterations: int = 5, factor: float = 0.5) -> dict:
    """Sculpt-like relax. Softens lumps without voxel remesh."""
    return run_bpy(f"""
import bpy, bmesh
obj = bpy.data.objects.get({object_name!r})
if not obj or obj.type != "MESH":
    raise ValueError("Mesh not found")
bm = bmesh.new()
bm.from_mesh(obj.data)
for _ in range({int(iterations)}):
    bmesh.ops.smooth_vert(bm, verts=bm.verts, factor={factor}, use_axis_x=True, use_axis_y=True, use_axis_z=True)
bm.to_mesh(obj.data)
bm.free()
obj.data.update()
print("smooth", obj.name)
""")


@mcp.tool()
def inflate_mesh(object_name: str, distance: float = 0.05) -> dict:
    """Push vertices along their normals (inflate / deflate)."""
    return run_bpy(f"""
import bpy, bmesh
obj = bpy.data.objects.get({object_name!r})
if not obj or obj.type != "MESH":
    raise ValueError("Mesh not found")
bm = bmesh.new()
bm.from_mesh(obj.data)
bm.normal_update()
for v in bm.verts:
    v.co += v.normal.normalized() * {distance}
bm.to_mesh(obj.data)
bm.free()
obj.data.update()
print("inflate", obj.name)
""")


@mcp.tool()
def sculpt_symmetrize(object_name: str, direction: str = "POSITIVE_X") -> dict:
    """Copy mesh across an axis. direction like POSITIVE_X or NEGATIVE_X."""
    return run_bpy(f"""
import bpy
obj = bpy.data.objects.get({object_name!r})
if not obj or obj.type != "MESH":
    raise ValueError("Mesh not found")
bpy.ops.object.select_all(action="DESELECT")
obj.select_set(True)
bpy.context.view_layer.objects.active = obj
bpy.ops.object.mode_set(mode="EDIT")
bpy.ops.mesh.select_all(action="SELECT")
bpy.ops.mesh.symmetrize(direction={direction!r})
bpy.ops.object.mode_set(mode="OBJECT")
print("symmetrize", obj.name, {direction!r})
""")


@mcp.tool()
def create_armature(name: str = "Armature", location: Optional[List[float]] = None) -> dict:
    """Create a new armature object with one root bone."""
    loc = location or [0.0, 0.0, 0.0]
    return run_bpy(f"""
import bpy
arm = bpy.data.armatures.new({name!r})
obj = bpy.data.objects.new({name!r}, arm)
obj.location = {loc}
bpy.context.collection.objects.link(obj)
bpy.context.view_layer.objects.active = obj
bpy.ops.object.mode_set(mode="EDIT")
bone = arm.edit_bones.new("Root")
bone.head = (0.0, 0.0, 0.0)
bone.tail = (0.0, 0.0, 1.0)
bpy.ops.object.mode_set(mode="OBJECT")
print(obj.name)
""")


@mcp.tool()
def add_bone(armature_name: str, bone_name: str, parent_bone: Optional[str] = None, head: Optional[List[float]] = None, tail: Optional[List[float]] = None) -> dict:
    """Add a bone to an armature. head/tail are in armature local space."""
    h = head or [0.0, 0.0, 1.0]
    t = tail or [0.0, 0.0, 2.0]
    return run_bpy(f"""
import bpy
obj = bpy.data.objects.get({armature_name!r})
if not obj or obj.type != "ARMATURE":
    raise ValueError("Armature not found")
bpy.context.view_layer.objects.active = obj
bpy.ops.object.mode_set(mode="EDIT")
bone = obj.data.edit_bones.new({bone_name!r})
bone.head = {h}
bone.tail = {t}
parent_name = {parent_bone!r}
if parent_name and parent_name in obj.data.edit_bones:
    bone.parent = obj.data.edit_bones[parent_name]
    bone.use_connect = False
bpy.ops.object.mode_set(mode="OBJECT")
print({bone_name!r})
""")


@mcp.tool()
def bind_mesh_to_armature(mesh_name: str, armature_name: str, automatic_weights: bool = True) -> dict:
    """Parent a mesh to an armature. automatic_weights paints a first skin."""
    return run_bpy(f"""
import bpy
mesh = bpy.data.objects.get({mesh_name!r})
arm = bpy.data.objects.get({armature_name!r})
if not mesh or mesh.type != "MESH":
    raise ValueError("Mesh not found")
if not arm or arm.type != "ARMATURE":
    raise ValueError("Armature not found")
bpy.ops.object.select_all(action="DESELECT")
mesh.select_set(True)
arm.select_set(True)
bpy.context.view_layer.objects.active = arm
if {automatic_weights}:
    bpy.ops.object.parent_set(type="ARMATURE_AUTO")
else:
    bpy.ops.object.parent_set(type="ARMATURE")
print("bound", mesh.name, "->", arm.name)
""")


@mcp.tool()
def pose_bone(armature_name: str, bone_name: str, rotation_degrees: Optional[List[float]] = None, location: Optional[List[float]] = None) -> dict:
    """Rotate or move a bone in Pose mode. rotation is XYZ degrees."""
    return run_bpy(f"""
import bpy, math
from mathutils import Euler
obj = bpy.data.objects.get({armature_name!r})
if not obj or obj.type != "ARMATURE":
    raise ValueError("Armature not found")
bpy.context.view_layer.objects.active = obj
bpy.ops.object.mode_set(mode="POSE")
bone = obj.pose.bones.get({bone_name!r})
if not bone:
    bpy.ops.object.mode_set(mode="OBJECT")
    raise ValueError("Bone not found")
rot = {rotation_degrees}
if rot is not None:
    bone.rotation_mode = "XYZ"
    bone.rotation_euler = Euler((math.radians(rot[0]), math.radians(rot[1]), math.radians(rot[2])), "XYZ")
loc = {location}
if loc is not None:
    bone.location = loc
bpy.ops.object.mode_set(mode="OBJECT")
print("posed", {bone_name!r})
""")


@mcp.tool()
def add_cloth(object_name: str, quality: int = 5) -> dict:
    """Add a Cloth modifier. Use add_collision on the ground or body it should sit on."""
    return run_bpy(f"""
import bpy
obj = bpy.data.objects.get({object_name!r})
if not obj or obj.type != "MESH":
    raise ValueError("Mesh not found")
mod = obj.modifiers.new("MCP_Cloth", "CLOTH")
mod.settings.quality = {int(quality)}
mod.collision_settings.collision_quality = max(2, {int(quality)})
print("cloth", obj.name)
""")


@mcp.tool()
def add_collision(object_name: str) -> dict:
    """Make a mesh collide with cloth or soft body."""
    return run_bpy(f"""
import bpy
obj = bpy.data.objects.get({object_name!r})
if not obj or obj.type != "MESH":
    raise ValueError("Mesh not found")
obj.modifiers.new("MCP_Collision", "COLLISION")
print("collision", obj.name)
""")


@mcp.tool()
def pin_cloth_vertices(object_name: str, group_name: str = "Pin", vertex_indices: Optional[List[int]] = None) -> dict:
    """Pin cloth verts. If indices omitted, pins selected verts, or the top row."""
    return run_bpy(f"""
import bpy
obj = bpy.data.objects.get({object_name!r})
if not obj or obj.type != "MESH":
    raise ValueError("Mesh not found")
group = obj.vertex_groups.get({group_name!r}) or obj.vertex_groups.new(name={group_name!r})
ids = {vertex_indices}
if not ids:
    ids = [v.index for v in obj.data.vertices if v.select]
if not ids:
    max_z = max(v.co.z for v in obj.data.vertices)
    ids = [v.index for v in obj.data.vertices if v.co.z >= max_z - 0.01]
group.add(ids, 1.0, "REPLACE")
cloth = next((m for m in obj.modifiers if m.type == "CLOTH"), None)
if cloth:
    cloth.settings.vertex_group_mass = group.name
print("pinned", len(ids), "on", obj.name)
""")


@mcp.tool()
def run_simulation(frames: int = 40, start: int = 1) -> dict:
    """Step the scene so cloth / physics can settle. Keep frames modest."""
    return run_bpy(f"""
import bpy
scene = bpy.context.scene
scene.frame_set({int(start)})
for frame in range({int(start)}, {int(start)} + {int(frames)}):
    scene.frame_set(frame)
print("simulated", scene.frame_current)
""")


@mcp.tool()
def add_geometry_nodes(object_name: str, group_name: str = "MCP_Geo") -> dict:
    """Add an empty Geometry Nodes modifier and group so nodes can be filled next."""
    return run_bpy(f"""
import bpy
obj = bpy.data.objects.get({object_name!r})
if not obj or obj.type != "MESH":
    raise ValueError("Mesh not found")
group = bpy.data.node_groups.get({group_name!r})
if not group:
    group = bpy.data.node_groups.new({group_name!r}, "GeometryNodeTree")
    group.interface.new_socket(name="Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    group.interface.new_socket(name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    n_in = group.nodes.new("NodeGroupInput"); n_in.location = (-200, 0)
    n_out = group.nodes.new("NodeGroupOutput"); n_out.location = (200, 0)
    group.links.new(n_in.outputs[0], n_out.inputs[0])
mod = next((m for m in obj.modifiers if m.type == "NODES"), None)
if not mod:
    mod = obj.modifiers.new("MCP_GeometryNodes", "NODES")
mod.node_group = group
print(group.name)
""")


@mcp.tool()
def scatter_on_mesh(source_name: str, instance_name: str, count: int = 25, seed: int = 1) -> dict:
    """Geometry Nodes: scatter copies of instance_name across source_name."""
    return run_bpy(f"""
import bpy
source = bpy.data.objects.get({source_name!r})
inst = bpy.data.objects.get({instance_name!r})
if not source or source.type != "MESH":
    raise ValueError("Source mesh not found")
if not inst:
    raise ValueError("Instance object not found")
group_name = source.name + "_Scatter"
group = bpy.data.node_groups.get(group_name) or bpy.data.node_groups.new(group_name, "GeometryNodeTree")
group.nodes.clear()
if hasattr(group, "interface"):
    try:
        group.interface.clear()
    except Exception:
        pass
    group.interface.new_socket(name="Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    group.interface.new_socket(name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
n_in = group.nodes.new("NodeGroupInput"); n_in.location = (-600, 0)
n_out = group.nodes.new("NodeGroupOutput"); n_out.location = (600, 0)
points = group.nodes.new("GeometryNodeDistributePointsOnFaces"); points.location = (-300, 0)
try:
    points.inputs["Density"].default_value = max(0.01, {int(count)} / 10.0)
except Exception:
    pass
info = group.nodes.new("GeometryNodeObjectInfo"); info.location = (-300, -250)
try:
    info.inputs[0].default_value = inst
except Exception:
    pass
inst_on = group.nodes.new("GeometryNodeInstanceOnPoints"); inst_on.location = (50, 0)
realize = group.nodes.new("GeometryNodeRealizeInstances"); realize.location = (300, 0)
group.links.new(n_in.outputs[0], points.inputs[0])
group.links.new(points.outputs[0], inst_on.inputs[0])
try:
    group.links.new(info.outputs["Geometry"], inst_on.inputs["Instance"])
except Exception:
    group.links.new(info.outputs[0], inst_on.inputs[1])
group.links.new(inst_on.outputs[0], realize.inputs[0])
group.links.new(realize.outputs[0], n_out.inputs[0])
mod = next((m for m in source.modifiers if m.type == "NODES" and m.name == "MCP_Scatter"), None)
if not mod:
    mod = source.modifiers.new("MCP_Scatter", "NODES")
mod.node_group = group
print("scatter", source.name, inst.name)
""")


@mcp.tool()
def create_camera(name: str = "MCP_Camera", location: Optional[List[float]] = None, make_active: bool = True) -> dict:
    """Create a camera and optionally make it the scene camera."""
    loc = location or [6.0, -6.0, 4.0]
    return run_bpy(f"""
import bpy
from mathutils import Vector
cam_data = bpy.data.cameras.new({name!r})
cam = bpy.data.objects.new({name!r}, cam_data)
cam.location = {loc}
bpy.context.collection.objects.link(cam)
target = Vector((0.0, 0.0, 1.0))
cam.rotation_euler = (target - cam.location).to_track_quat("-Z", "Y").to_euler()
if {make_active}:
    bpy.context.scene.camera = cam
print(cam.name)
""")


@mcp.tool()
def set_render_settings(engine: str = "BLENDER_EEVEE", x: int = 1280, y: int = 720, samples: int = 32) -> dict:
    """Set render engine, resolution, and samples."""
    return run_bpy(f"""
import bpy
scene = bpy.context.scene
engine = {engine!r}
try:
    scene.render.engine = engine
except TypeError:
    scene.render.engine = "BLENDER_EEVEE"
scene.render.resolution_x = {int(x)}
scene.render.resolution_y = {int(y)}
if hasattr(scene.cycles, "samples"):
    scene.cycles.samples = {int(samples)}
if hasattr(scene.eevee, "taa_render_samples"):
    scene.eevee.taa_render_samples = {int(samples)}
print(scene.render.engine, scene.render.resolution_x, scene.render.resolution_y)
""")


@mcp.tool()
def add_hdri_world(filepath: str, strength: float = 1.0) -> dict:
    """Use an HDRI image as world lighting."""
    return run_bpy(f"""
import bpy
world = bpy.context.scene.world or bpy.data.worlds.new("World")
bpy.context.scene.world = world
world.use_nodes = True
nt = world.node_tree
nt.nodes.clear()
out = nt.nodes.new("ShaderNodeOutputWorld"); out.location = (300, 0)
bg = nt.nodes.new("ShaderNodeBackground"); bg.location = (50, 0)
env = nt.nodes.new("ShaderNodeTexEnvironment"); env.location = (-250, 0)
env.image = bpy.data.images.load({filepath!r})
bg.inputs["Strength"].default_value = {strength}
nt.links.new(env.outputs["Color"], bg.inputs["Color"])
nt.links.new(bg.outputs["Background"], out.inputs["Surface"])
print("hdri", env.image.name)
""")


@mcp.tool()
def add_rigid_body(object_name: str, body_type: str = "ACTIVE", mass: float = 1.0) -> dict:
    """Add rigid body physics. body_type ACTIVE or PASSIVE."""
    return run_bpy(f"""
import bpy
obj = bpy.data.objects.get({object_name!r})
if not obj or obj.type != "MESH":
    raise ValueError("Mesh not found")
bpy.context.view_layer.objects.active = obj
obj.select_set(True)
if not obj.rigid_body:
    bpy.ops.rigidbody.object_add()
obj.rigid_body.type = {body_type!r}
obj.rigid_body.mass = {mass}
print("rigid", obj.name, obj.rigid_body.type)
""")


@mcp.tool()
def add_soft_body(object_name: str) -> dict:
    """Add a Soft Body modifier."""
    return run_bpy(f"""
import bpy
obj = bpy.data.objects.get({object_name!r})
if not obj or obj.type != "MESH":
    raise ValueError("Mesh not found")
obj.modifiers.new("MCP_Softbody", "SOFT_BODY")
print("softbody", obj.name)
""")


@mcp.tool()
def add_particle_hair(object_name: str, count: int = 200) -> dict:
    """Add a simple hair particle system."""
    return run_bpy(f"""
import bpy
obj = bpy.data.objects.get({object_name!r})
if not obj or obj.type != "MESH":
    raise ValueError("Mesh not found")
obj.modifiers.new("MCP_Hair", "PARTICLE_SYSTEM")
settings = obj.particle_systems[-1].settings
settings.type = "HAIR"
settings.count = {int(count)}
settings.hair_length = 0.2
print("hair", obj.name, settings.count)
""")


@mcp.tool()
def add_track_to(object_name: str, target_name: str) -> dict:
    """Make an object always point at a target."""
    return run_bpy(f"""
import bpy
obj = bpy.data.objects.get({object_name!r})
target = bpy.data.objects.get({target_name!r})
if not obj or not target:
    raise ValueError("Object not found")
con = obj.constraints.new("TRACK_TO")
con.target = target
con.track_axis = "TRACK_NEGATIVE_Z"
con.up_axis = "UP_Y"
print("track", obj.name, target.name)
""")


@mcp.tool()
def add_shape_key(object_name: str, key_name: str = "Key", value: float = 0.0) -> dict:
    """Add a shape key. Creates Basis first if needed."""
    return run_bpy(f"""
import bpy
obj = bpy.data.objects.get({object_name!r})
if not obj or obj.type != "MESH":
    raise ValueError("Mesh not found")
if not obj.data.shape_keys:
    obj.shape_key_add(name="Basis")
key = obj.shape_key_add(name={key_name!r})
key.value = {value}
print(key.name, key.value)
""")


@mcp.tool()
def add_curve_path(name: str = "Path", points: Optional[List[List[float]]] = None) -> dict:
    """Create a poly curve path from points."""
    pts = points or [[0, 0, 0], [2, 0, 0], [4, 1, 0]]
    return run_bpy(f"""
import bpy
curve = bpy.data.curves.new({name!r}, "CURVE")
curve.dimensions = "3D"
spline = curve.splines.new("POLY")
pts = {pts}
spline.points.add(len(pts) - 1)
for i, p in enumerate(pts):
    spline.points[i].co = (p[0], p[1], p[2], 1)
obj = bpy.data.objects.new({name!r}, curve)
bpy.context.collection.objects.link(obj)
print(obj.name)
""")


@mcp.tool()
def follow_path(object_name: str, curve_name: str) -> dict:
    """Make an object follow a curve path."""
    return run_bpy(f"""
import bpy
obj = bpy.data.objects.get({object_name!r})
curve = bpy.data.objects.get({curve_name!r})
if not obj or not curve:
    raise ValueError("Object not found")
con = obj.constraints.new("FOLLOW_PATH")
con.target = curve
con.use_curve_follow = True
print("follow", obj.name, curve.name)
""")


@mcp.tool()
def add_lattice(object_name: str, name: str = "Lattice") -> dict:
    """Add a lattice deformer around a mesh."""
    return run_bpy(f"""
import bpy
obj = bpy.data.objects.get({object_name!r})
if not obj or obj.type != "MESH":
    raise ValueError("Mesh not found")
lat_data = bpy.data.lattices.new({name!r})
lat = bpy.data.objects.new({name!r}, lat_data)
lat.location = obj.location.copy()
bpy.context.collection.objects.link(lat)
mod = obj.modifiers.new("MCP_Lattice", "LATTICE")
mod.object = lat
print(lat.name)
""")


@mcp.tool()
def mark_seams_from_islands(object_name: str) -> dict:
    """Mark UV seams from existing islands."""
    return run_bpy(f"""
import bpy
obj = bpy.data.objects.get({object_name!r})
if not obj or obj.type != "MESH":
    raise ValueError("Mesh not found")
bpy.ops.object.select_all(action="DESELECT")
obj.select_set(True)
bpy.context.view_layer.objects.active = obj
bpy.ops.object.mode_set(mode="EDIT")
bpy.ops.uv.seams_from_islands()
bpy.ops.object.mode_set(mode="OBJECT")
print("seams", obj.name)
""")


@mcp.tool()
def modeling_playbook() -> dict:
    """Return the recommended tool order for building a clean 3D model with this MCP."""
    return _ok({
        "order": [
            "ping_blender",
            "get_scene_summary or list_objects",
            "create_primitive / create_box / create_text_object for blockout",
            "transform_object, ground_object, snap_to_grid, align_objects, set_exact_dimensions",
            "mirror_object, array_object, boolean_operation, join_objects",
            "bevel_object, extrude_faces, inset_faces, solidify_object, remesh_object, smooth_mesh, inflate_mesh",
            "create_armature, add_bone, bind_mesh_to_armature, pose_bone",
            "add_cloth, add_collision, pin_cloth_vertices, run_simulation",
            "add_geometry_nodes, scatter_on_mesh",
            "merge_by_distance, recalc_normals, shade_smooth",
            "create_principled_material or create_toon_material, smart_uv_unwrap",
            "setup_lighting_preset, set_world_color",
            "frame_and_render then inspect_object / scene_health",
            "save_blend, export_scene",
        ],
        "rules": [
            "Inspect before editing.",
            "Name every new object.",
            "Keep origin at the base of standing props.",
            "Do not remesh high-detail hard-surface parts unless you want clay.",
            "Prefer modifiers, then apply only when the shape is final.",
            "Check scene_health if Blender gets slow.",
            "Save the .blend before risky booleans or remesh.",
        ],
    })



# ---------------------------------------------------------------------------
# AI agent layer (optional OpenAI Responses API)
# ---------------------------------------------------------------------------

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_AGENT_MODEL = os.environ.get("OPENAI_AGENT_MODEL", "gpt-5.6-luna").strip()
OPENAI_AGENT_MAX_TURNS = int(os.environ.get("OPENAI_AGENT_MAX_TURNS", "8"))


def _agent_tool_defs() -> list:
    """Small, high-value tool surface for the optional MCP-native AI agent."""
    return [
        {"type":"function","name":"project_status","description":"Inspect Blender, Godot, and shared AI collaboration status.","parameters":{"type":"object","properties":{},"additionalProperties":False},"strict":True},
        {"type":"function","name":"blender_inspect","description":"Inspect the Blender scene or one object. Read-only.","parameters":{"type":"object","properties":{"object_name":{"type":["string","null"]}},"additionalProperties":False},"strict":True},
        {"type":"function","name":"blender_execute","description":"Execute Blender Python for authorized project editing. Inspect first and make focused changes.","parameters":{"type":"object","properties":{"python_code":{"type":"string"}},"required":["python_code"],"additionalProperties":False},"strict":True},
        {"type":"function","name":"godot_tree","description":"Inspect the live Godot scene tree.","parameters":{"type":"object","properties":{},"additionalProperties":False},"strict":True},
        {"type":"function","name":"godot_execute","description":"Execute GDScript inside the live Godot bridge. The script must define func run(tree).", "parameters":{"type":"object","properties":{"gdscript":{"type":"string"}},"required":["gdscript"],"additionalProperties":False},"strict":True},
        {"type":"function","name":"send_team_message","description":"Send a concise progress or blocker message to the other AI agents.","parameters":{"type":"object","properties":{"text":{"type":"string"},"to":{"type":"string"}},"required":["text","to"],"additionalProperties":False},"strict":True},
        {"type":"function","name":"read_team_messages","description":"Read recent shared messages from the other AI agents.","parameters":{"type":"object","properties":{"after_id":{"type":"integer"}},"required":["after_id"],"additionalProperties":False},"strict":True},
        {"type":"function","name":"roblox_status","description":"Check Roblox Open Cloud configuration without exposing secrets.","parameters":{"type":"object","properties":{},"additionalProperties":False},"strict":True},
        {"type":"function","name":"roblox_list_data_stores","description":"List Roblox data stores for the configured universe.","parameters":{"type":"object","properties":{},"additionalProperties":False},"strict":True},
        {"type":"function","name":"roblox_get_instance","description":"Read a Roblox Engine Open Cloud instance.","parameters":{"type":"object","properties":{"instance_id":{"type":"string"}},"required":["instance_id"],"additionalProperties":False},"strict":True},
        {"type":"function","name":"roblox_update_script","description":"Update a Roblox script through Engine Open Cloud. Use only for focused project edits and verify afterward.","parameters":{"type":"object","properties":{"instance_id":{"type":"string"},"source":{"type":"string"}},"required":["instance_id","source"],"additionalProperties":False},"strict":True},
    ]


def _agent_dispatch(name: str, args: dict) -> dict:
    if name == "project_status":
        return bridge_status()
    if name == "blender_inspect":
        obj = args.get("object_name")
        return inspect_object(obj) if obj else get_scene_summary()
    if name == "blender_execute":
        return execute_bpy_script(args.get("python_code", ""))
    if name == "godot_tree":
        return godot_scene_tree()
    if name == "godot_execute":
        return godot_run(args.get("gdscript", ""))
    if name == "send_team_message":
        return post_ai_message("mcp-agent", args.get("text", ""), args.get("to", "all"))
    if name == "read_team_messages":
        return read_ai_messages("mcp-agent", int(args.get("after_id", 0)), 30)
    if name == "roblox_status":
        return roblox_status()
    if name == "roblox_list_data_stores":
        return roblox_list_data_stores()
    if name == "roblox_get_instance":
        return roblox_get_instance(args.get("instance_id", ""))
    if name == "roblox_update_script":
        return roblox_update_script(args.get("instance_id", ""), args.get("source", ""))
    return {"status":"error","message":f"Unknown agent tool: {name}"}


def _openai_agent_call(instructions: str, input_items: list) -> dict:
    if not OPENAI_API_KEY:
        return {"status":"error","message":"OPENAI_API_KEY is not set. The MCP agent layer is installed but disabled until you add the key."}
    payload = {
        "model": OPENAI_AGENT_MODEL,
        "instructions": instructions,
        "input": input_items,
        "tools": _agent_tool_defs(),
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization":f"Bearer {OPENAI_API_KEY}","Content-Type":"application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        return {"status":"error","message":f"OpenAI agent request failed: {exc}"}


def _response_text(resp: dict) -> str:
    if isinstance(resp.get("output_text"), str):
        return resp["output_text"]
    parts = []
    for item in resp.get("output", []) or []:
        if item.get("type") == "message":
            for content in item.get("content", []) or []:
                if content.get("type") in {"output_text", "text"}:
                    parts.append(content.get("text", ""))
    return "\n".join(p for p in parts if p)


@mcp.tool()
def mcp_agent(task: str, context: str = "", max_turns: Optional[int] = None) -> dict:
    """Run the MCP's optional autonomous AI loop. It can inspect and edit Blender/Godot and coordinate with the other AIs.

    Requires OPENAI_API_KEY. The agent is deliberately given a compact tool set and a turn limit.
    It must inspect before editing, verify after editing, and report blockers instead of guessing.
    """
    if not OPENAI_API_KEY:
        return {"status":"error","message":"Set OPENAI_API_KEY to enable mcp_agent. No key is stored in this source file."}
    turns = max(1, min(int(max_turns or OPENAI_AGENT_MAX_TURNS), 16))
    instructions = """You are the autonomous engineering agent inside FeranmiMCP. You are working on a Godot/Blender game project shared by several AI agents. Inspect before changing anything. Prefer small reversible changes. Never claim success without verification. Coordinate through the shared team-message tools when another agent needs to know something. You have access only to the tools explicitly supplied to you. Do not expose or request secrets. If a task is ambiguous, inspect the project and make the safest reasonable interpretation."""
    if context:
        instructions += "\nAdditional project context:\n" + context[:8000]
    items = [{"role":"user","content":task}]
    for turn in range(turns):
        resp = _openai_agent_call(instructions, items)
        if resp.get("status") == "error":
            return resp
        calls = [x for x in (resp.get("output") or []) if x.get("type") == "function_call"]
        if not calls:
            return {"status":"success","result":{"turns":turn+1,"message":_response_text(resp),"model":OPENAI_AGENT_MODEL}}
        items.extend(resp.get("output") or [])
        for call in calls:
            try:
                args = json.loads(call.get("arguments") or "{}")
                result = _agent_dispatch(call.get("name", ""), args)
            except Exception as exc:
                result = {"status":"error","message":str(exc)}
            items.append({"type":"function_call_output","call_id":call.get("call_id"),"output":json.dumps(result, default=str)})
    return {"status":"success","result":{"turns":turns,"message":"Agent reached its turn limit. Inspect the latest project state before continuing.","model":OPENAI_AGENT_MODEL}}


# ---------------------------------------------------------------------------
# Extra Blender workflow tools
# ---------------------------------------------------------------------------

@mcp.tool()
def blender_create_collection(name: str, parent_collection: Optional[str] = None) -> dict:
    """Create a Blender collection and optionally link it under another collection."""
    return run_bpy(f"""
import bpy
name = {name!r}
col = bpy.data.collections.get(name) or bpy.data.collections.new(name)
parent_name = {parent_collection!r}
parent = bpy.data.collections.get(parent_name) if parent_name else bpy.context.scene.collection
if col.name not in [c.name for c in parent.children]:
    try: parent.children.link(col)
    except RuntimeError: pass
print("collection", col.name)
""")


@mcp.tool()
def blender_collection_objects(collection_name: str) -> dict:
    """List objects inside a Blender collection with transforms and types."""
    return run_bpy(f"""
import bpy, json
col = bpy.data.collections.get({collection_name!r})
if not col:
    raise ValueError("Collection not found")
out=[]
for o in col.objects:
    out.append({{"name":o.name,"type":o.type,"location":list(o.location),"dimensions":list(o.dimensions) if hasattr(o,'dimensions') else []}})
print(json.dumps(out, default=str))
""")


@mcp.tool()
def blender_get_world_bounds(object_name: str) -> dict:
    """Return the rotated object's world-space bounding box and dimensions."""
    return run_bpy(f"""
import bpy, json
from mathutils import Vector
obj = bpy.data.objects.get({object_name!r})
if not obj:
    raise ValueError("Object not found")
corners=[obj.matrix_world @ Vector(c) for c in obj.bound_box]
mins=[min(v[i] for v in corners) for i in range(3)]
maxs=[max(v[i] for v in corners) for i in range(3)]
print(json.dumps({{"min":mins,"max":maxs,"size":[maxs[i]-mins[i] for i in range(3)],"center":[(mins[i]+maxs[i])/2 for i in range(3)]}}))
""")


@mcp.tool()
def blender_duplicate_grid(object_name: str, rows: int = 2, columns: int = 2, spacing_x: float = 2.0, spacing_y: float = 2.0, collection_name: Optional[str] = None) -> dict:
    """Duplicate an object into a deterministic X/Y grid."""
    return run_bpy(f"""
import bpy
source=bpy.data.objects.get({object_name!r})
if not source: raise ValueError("Object not found")
col=bpy.data.collections.get({collection_name!r}) if {collection_name!r} else bpy.context.collection
created=[]
for r in range(max(1,{int(rows)})):
    for c in range(max(1,{int(columns)})):
        if r==0 and c==0: continue
        copy=source.copy()
        if source.data: copy.data=source.data.copy()
        col.objects.link(copy)
        copy.location=source.location.copy()
        copy.location.x += c*{float(spacing_x)}
        copy.location.y += r*{float(spacing_y)}
        created.append(copy.name)
print(created)
""")


@mcp.tool()
def blender_render_preview(filepath: Optional[str] = None, resolution_x: int = 1024, resolution_y: int = 1024) -> dict:
    """Render a quick Eevee preview from the active scene camera."""
    path=filepath or os.path.join(tempfile.gettempdir(),"blender_mcp_preview.png").replace("\\","/")
    return run_bpy(f"""
import bpy
scene=bpy.context.scene
if not scene.camera: raise ValueError("No active camera")
scene.render.engine = "BLENDER_EEVEE"
scene.render.resolution_x={int(resolution_x)}
scene.render.resolution_y={int(resolution_y)}
scene.render.resolution_percentage=100
scene.render.filepath={path!r}
scene.render.image_settings.file_format="PNG"
bpy.ops.render.render(write_still=True)
print({path!r})
""")


# ---------------------------------------------------------------------------
# Godot (separate port — Godot does not use Blender's 9876)
# Protocol: one JSON object + newline each way.
# ---------------------------------------------------------------------------

_godot_io = threading.Lock()


class GodotConnectionError(Exception):
    pass


def send_godot(payload: dict) -> dict:
    raw_out = (json.dumps(payload) + "\n").encode("utf-8")
    try:
        with _godot_io:
            with socket.create_connection((GODOT_HOST, GODOT_PORT), timeout=SOCKET_TIMEOUT) as sock:
                sock.settimeout(SOCKET_TIMEOUT)
                sock.sendall(raw_out)
                chunks = []
                while True:
                    data = sock.recv(65536)
                    if not data:
                        break
                    chunks.append(data)
                    if b"\n" in data:
                        break
        raw = b"".join(chunks).decode("utf-8").strip()
        if not raw:
            raise GodotConnectionError("Godot bridge returned no response.")
        return json.loads(raw.splitlines()[-1])
    except ConnectionRefusedError as exc:
        raise GodotConnectionError(
            f"Could not connect to Godot at {GODOT_HOST}:{GODOT_PORT}. "
            "Add godot_mcp_bridge.gd as an Autoload and run the project or editor plugin."
        ) from exc
    except OSError as exc:
        raise GodotConnectionError(f"Godot bridge failed at {GODOT_HOST}:{GODOT_PORT}: {exc}") from exc


def call_godot(command_type: str, params: Optional[dict] = None) -> dict:
    body = {"type": command_type}
    if params:
        body.update(params)
    try:
        response = send_godot(body)
    except GodotConnectionError as exc:
        return {"status": "error", "message": str(exc)}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}
    if not isinstance(response, dict):
        return {"status": "error", "message": f"Bad Godot response: {response!r}"}
    if "status" not in response:
        response["status"] = "success" if response.get("ok", True) else "error"
    return response


@mcp.tool()
def godot_project_status() -> dict:
    """Return high-level live Godot runtime/project state."""
    return call_godot("execute", {"code": """
func run(tree):
	var root = tree.get_root()
	return {
		"engine_version": Engine.get_version_info(),
		"project": ProjectSettings.get_setting("application/config/name", "Unknown"),
		"scene": get_tree().current_scene.scene_file_path if get_tree().current_scene else "",
		"paused": get_tree().paused,
		"time_scale": Engine.time_scale,
		"node_count": root.get_child_count() if root else 0,
		"fps": Engine.get_frames_per_second()
	}
"""})


@mcp.tool()
def godot_find_nodes(query: str = "", class_name: str = "") -> dict:
    """Search the live Godot tree by node name/path or class."""
    return call_godot("execute", {"code": f"""
func run(tree):
	var out = []
	var needle = {query!r}.to_lower()
	var cls = {class_name!r}
	_walk(tree.get_root(), out, needle, cls)
	return out

func _walk(node, out, needle, cls):
	if cls == "" or node.is_class(cls):
		var path = str(node.get_path())
		if needle == "" or node.name.to_lower().contains(needle) or path.to_lower().contains(needle):
			out.append({{"path": path, "name": node.name, "class": node.get_class()}})
	for child in node.get_children():
		_walk(child, out, needle, cls)
"""})


@mcp.tool()
def godot_node_info(node_path: str) -> dict:
    """Inspect a live Godot node's class, parent, children and common transform properties."""
    return call_godot("execute", {"code": f"""
func run(tree):
	var node = tree.get_node_or_null({node_path!r})
	if node == null:
		return {{"error":"missing","path":{node_path!r}}}
	var out = {{"path":str(node.get_path()),"name":node.name,"class":node.get_class(),"parent":str(node.get_parent().get_path()) if node.get_parent() else "","children":[]}}
	for child in node.get_children():
		out.children.append({{"name":child.name,"class":child.get_class(),"path":str(child.get_path())}})
	if node is Node3D:
		out["position"] = [node.position.x,node.position.y,node.position.z]
		out["rotation_degrees"] = [node.rotation_degrees.x,node.rotation_degrees.y,node.rotation_degrees.z]
		out["scale"] = [node.scale.x,node.scale.y,node.scale.z]
	elif node is Node2D:
		out["position"] = [node.position.x,node.position.y]
		out["rotation_degrees"] = node.rotation_degrees
		out["scale"] = [node.scale.x,node.scale.y]
	return out
"""})


@mcp.tool()
def godot_create_scene_nodes(parent_path: str, nodes_json: str) -> dict:
    """Create multiple nodes in one call. nodes_json is a list of {node_type,name}."""
    try:
        nodes = json.loads(nodes_json)
    except json.JSONDecodeError as exc:
        return {"status":"error","message":f"nodes_json must be valid JSON: {exc}"}
    if not isinstance(nodes, list):
        return {"status":"error","message":"nodes_json must be a JSON list"}
    return call_godot("batch_create_nodes", {"parent": parent_path, "nodes": nodes[:100]})


@mcp.tool()
def godot_set_transform(node_path: str, position: Optional[List[float]] = None, rotation_degrees: Optional[List[float]] = None, scale: Optional[List[float]] = None) -> dict:
    """Set a Node3D/Node2D transform in one operation."""
    return call_godot("set_transform", {"path":node_path,"position":position,"rotation_degrees":rotation_degrees,"scale":scale})


@mcp.tool()
def godot_add_script(path: str, script_text: str) -> dict:
    """Write a GDScript file into the running Godot project's res:// filesystem."""
    return godot_write_file(path, script_text)


@mcp.tool()
def godot_read_text(path: str) -> dict:
    """Read a text resource from the Godot project."""
    return godot_read_file(path)


@mcp.tool()
def godot_project_files(folder: str = "res://") -> dict:
    """List common Godot project resources under a folder."""
    return call_godot("execute", {"code": f"""
func run(tree):
	var files = []
	_walk({folder!r}, files, 0)
	return files

func _walk(prefix, files, depth):
	if depth > 6:
		return
	var dir = DirAccess.open(prefix)
	if dir == null:
		return
	dir.list_dir_begin()
	var name = dir.get_next()
	while name != "":
		if not name.begins_with("."):
			var path = prefix.path_join(name)
			if dir.current_is_dir():
				_walk(path, files, depth + 1)
			else:
				files.append(path)
		name = dir.get_next()
	dir.list_dir_end()
"""})


@mcp.tool()
def godot_set_engine_time_scale(scale: float = 1.0) -> dict:
    """Set Engine.time_scale with a safe range for testing."""
    return call_godot("set_time_scale", {"scale": max(0.05, min(float(scale), 8.0))})


@mcp.tool()
def godot_runtime_tree_dump(max_depth: int = 8) -> dict:
    """Return a compact recursive tree dump for debugging scene structure."""
    depth = max(1, min(int(max_depth), 16))
    return call_godot("execute", {"code": f"""
func run(tree):
	var out = []
	_dump(tree.get_root(), out, 0)
	return out

func _dump(node, out, depth):
	if depth > {depth}:
		return
	out.append({{"path":str(node.get_path()),"class":node.get_class(),"name":node.name,"depth":depth}})
	for child in node.get_children():
		_dump(child, out, depth + 1)
"""})


@mcp.tool()
def ping_godot() -> dict:
    """Check the Godot MCP bridge on GODOT_PORT (default 9877). Always returns text."""
    result = call_godot("ping")
    result["bridge"] = {"host": GODOT_HOST, "port": GODOT_PORT}
    if result.get("status") == "error" and not result.get("message"):
        result["message"] = (
            f"No usable reply from Godot at {GODOT_HOST}:{GODOT_PORT}. "
            "Autoload only listens while a scene is PLAYING. Press Play in Godot."
        )
    return result


@mcp.tool()
def godot_scene_tree() -> dict:
    """Return the live Godot scene tree."""
    return call_godot("scene_tree")


@mcp.tool()
def godot_run(gdscript: str) -> dict:
    """Send an execute request to the Godot bridge. Requires a bridge implementation that supports its execute command."""
    return call_godot("execute", {"code": gdscript})


@mcp.tool()
def godot_set_property(node_path: str, property_name: str, value_json: str) -> dict:
    """Set a node property. value_json is JSON, e.g. '[0,1,0]' or 'true'."""
    try:
        value = json.loads(value_json)
    except json.JSONDecodeError:
        value = value_json
    return call_godot("set_property", {"path": node_path, "property": property_name, "value": value})


@mcp.tool()
def godot_create_node(parent_path: str, node_type: str, name: str) -> dict:
    """Create a node under parent_path, e.g. type MeshInstance3D."""
    return call_godot("create_node", {"parent": parent_path, "node_type": node_type, "name": name})


@mcp.tool()
def godot_screenshot(filepath: Optional[str] = None) -> dict:
    """Capture the Godot game viewport (the running scene, not the editor chrome)."""
    path = filepath or os.path.join(tempfile.gettempdir(), "godot_mcp_view.png").replace("\\", "/")
    return call_godot("screenshot", {"filepath": path})


@mcp.tool()
def send_blender_mesh_to_godot(object_name: str, glb_path: Optional[str] = None, node_name: Optional[str] = None) -> dict:
    """Export one Blender object to GLB, then ask Godot to instance it."""
    path = glb_path or os.path.join(tempfile.gettempdir(), f"{object_name}.glb").replace("\\", "/")
    exported = call_blender("export_scene", {
        "filepath": path,
        "format": "GLB",
        "selected_only": True,
    })
    # Best-effort select + export via Python if addon export needs selection
    if exported.get("status") not in {"success", "ok"}:
        exported = run_bpy(f"""
import bpy
obj = bpy.data.objects.get({object_name!r})
if not obj:
    raise ValueError("Object not found")
bpy.ops.object.select_all(action="DESELECT")
obj.select_set(True)
bpy.context.view_layer.objects.active = obj
bpy.ops.export_scene.gltf(filepath={path!r}, use_selection=True, export_format="GLB")
print({path!r})
""")
    if exported.get("status") not in {"success", "ok"}:
        return exported
    return call_godot("import_glb", {"filepath": path, "name": node_name or object_name})


@mcp.tool()
def godot_list_nodes(query: str = "") -> dict:
    """List node paths. Optional query filters by name or class."""
    return call_godot("list_nodes", {"query": query})


@mcp.tool()
def godot_get_property(node_path: str, property_name: str) -> dict:
    """Read one property from a Godot node."""
    return call_godot("get_property", {"path": node_path, "property": property_name})


@mcp.tool()
def godot_move_node(node_path: str, x: float, y: float, z: float = 0.0) -> dict:
    """Set a node's position. Uses position for 2D and position for 3D when present."""
    return call_godot("move_node", {"path": node_path, "x": x, "y": y, "z": z})


@mcp.tool()
def godot_rotate_node(node_path: str, x_deg: float = 0.0, y_deg: float = 0.0, z_deg: float = 0.0) -> dict:
    """Rotate a node in degrees."""
    return call_godot("rotate_node", {"path": node_path, "x": x_deg, "y": y_deg, "z": z_deg})


@mcp.tool()
def godot_scale_node(node_path: str, x: float = 1.0, y: float = 1.0, z: float = 1.0) -> dict:
    """Scale a node."""
    return call_godot("scale_node", {"path": node_path, "x": x, "y": y, "z": z})


@mcp.tool()
def godot_delete_node(node_path: str) -> dict:
    """Delete a node from the live tree."""
    return call_godot("delete_node", {"path": node_path})


@mcp.tool()
def godot_call_method(node_path: str, method: str, args_json: str = "[]") -> dict:
    """Call a method on a node. args_json is a JSON list."""
    try:
        args = json.loads(args_json)
    except json.JSONDecodeError:
        args = []
    if not isinstance(args, list):
        args = [args]
    return call_godot("call_method", {"path": node_path, "method": method, "args": args})


@mcp.tool()
def godot_spawn_mesh(name: str, primitive: str = "box", x: float = 0.0, y: float = 0.0, z: float = 0.0) -> dict:
    """Spawn a MeshInstance3D. primitive: box, sphere, plane, cylinder, prism."""
    return call_godot("spawn_mesh", {"name": name, "primitive": primitive, "x": x, "y": y, "z": z})


@mcp.tool()
def godot_load_scene(scene_path: str) -> dict:
    """Change scene to a packed scene path like res://main.tscn."""
    return call_godot("load_scene", {"path": scene_path})


@mcp.tool()
def godot_set_paused(paused: bool = True) -> dict:
    """Pause or unpause the Godot tree."""
    return call_godot("set_paused", {"paused": paused})


@mcp.tool()
def godot_set_time_scale(scale: float = 1.0) -> dict:
    """Change Engine.time_scale."""
    return call_godot("set_time_scale", {"scale": scale})


@mcp.tool()
def godot_play_animation(node_path: str, animation: str) -> dict:
    """Play an animation on an AnimationPlayer node."""
    return call_godot("play_animation", {"path": node_path, "animation": animation})


@mcp.tool()
def godot_press_action(action: str, pressed: bool = True) -> dict:
    """Set an InputMap action pressed/released (ui_accept, ui_left, custom actions)."""
    return call_godot("press_action", {"action": action, "pressed": pressed})


@mcp.tool()
def connection_map() -> dict:
    """Show how this MCP talks to Blender, Godot, and other AIs."""
    return _ok({
        "blender": {"host": BLENDER_HOST, "port": BLENDER_PORT, "role": "3D modeling"},
        "godot": {"host": GODOT_HOST, "port": GODOT_PORT, "role": "game runtime"},
        "site": {"host": SITE_HOST, "port": SITE_PORT, "public_mcp_url": PUBLIC_MCP_URL or None},
        "talk": {"file": TALK_FILE, "messages": len(_talk_load()["messages"])},
        "note": "Talk is a shared file so local MCP and ngrok MCP see the same messages.",
    })


_talk_lock = threading.Lock()


def _authorized(header_token: str) -> bool:
    if not SITE_TOKEN:
        return True
    return header_token.strip() in {SITE_TOKEN, f"Bearer {SITE_TOKEN}"}


def _talk_load() -> Dict[str, Any]:
    if os.path.exists(TALK_FILE):
        try:
            with open(TALK_FILE, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict) and isinstance(data.get("messages"), list):
                data.setdefault("seen", {})
                return data
        except Exception:
            pass
    return {"messages": [], "seen": {}}


def _talk_save(state: Dict[str, Any]) -> None:
    folder = os.path.dirname(TALK_FILE) or "."
    os.makedirs(folder, exist_ok=True)
    existing: Dict[str, Any] = {}
    if os.path.exists(TALK_FILE):
        try:
            with open(TALK_FILE, "r", encoding="utf-8") as handle:
                existing = json.load(handle) or {}
        except Exception:
            existing = {}
    merged = list(existing.get("messages") or [])
    seen_ids = {m.get("id") for m in merged if isinstance(m, dict)}
    for item in state.get("messages") or []:
        if isinstance(item, dict) and item.get("id") not in seen_ids:
            merged.append(item)
            seen_ids.add(item.get("id"))
    merged.sort(key=lambda m: int(m.get("id") or 0))
    seen = {}
    seen.update(existing.get("seen") or {})
    seen.update(state.get("seen") or {})
    payload = {"messages": merged[-400:], "seen": seen}
    tmp = TALK_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    os.replace(tmp, TALK_FILE)
    state["messages"] = payload["messages"]
    state["seen"] = payload["seen"]


def _talk_touch(name: str) -> None:
    state = _talk_load()
    state.setdefault("seen", {})[name] = time.time()
    _talk_save(state)


def _talk_add(sender: str, text: str, to: str) -> Dict[str, Any]:
    with _talk_lock:
        state = _talk_load()
        messages = state.setdefault("messages", [])
        item = {
            "id": (messages[-1]["id"] + 1) if messages else 1,
            "ts": time.time(),
            "from": sender,
            "to": to,
            "text": text,
        }
        messages.append(item)
        del messages[:-400]
        state.setdefault("seen", {})[sender] = time.time()
        _talk_save(state)
        return item


@mcp.tool()
def post_ai_message(sender: str, text: str, to: str = "all") -> dict:
    """Post a note other AIs can read. Shared file, so ngrok and local clients see it."""
    sender = (sender or "ai").strip()[:40]
    text = (text or "").strip()[:2000]
    to = (to or "all").strip()[:40]
    if not text:
        return {"status": "error", "message": "Empty message"}
    return _ok(_talk_add(sender, text, to))


@mcp.tool()
def read_ai_messages(reader: str = "ai", after_id: int = 0, limit: int = 30) -> dict:
    """Read talk-board notes from other AIs. Reloads from disk every call."""
    reader = (reader or "ai").strip()[:40]
    with _talk_lock:
        state = _talk_load()
        state.setdefault("seen", {})[reader] = time.time()
        _talk_save(state)
        rows = [
            m for m in state.get("messages", [])
            if m.get("id", 0) > int(after_id) and (m.get("to") in {"all", reader} or m.get("from") == reader)
        ]
        seen = state.get("seen", {})
    return _ok({"messages": rows[-max(1, min(int(limit), 100)):], "online": list(seen), "file": TALK_FILE})


@mcp.tool()
def list_ai_clients() -> dict:
    """AIs that posted or read the talk board in the last 2 minutes."""
    now = time.time()
    with _talk_lock:
        state = _talk_load()
        seen = state.get("seen", {})
        online = [name for name, stamp in seen.items() if now - float(stamp) < 120]
    return _ok({"online": online, "message_count": len(state.get("messages", [])), "file": TALK_FILE})


@mcp.tool()
def bridge_status() -> dict:
    """Ping Blender, Godot, and the talk file in one call."""
    blender = call_blender("ping")
    godot = call_godot("ping")
    with _talk_lock:
        state = _talk_load()
    return _ok({
        "blender": blender.get("status"),
        "godot": godot.get("status"),
        "godot_error": godot.get("message"),
        "talk_file": TALK_FILE,
        "talk_messages": len(state.get("messages", [])),
        "mcp": "http://127.0.0.1:8000/mcp",
        "talk_http": f"http://127.0.0.1:{SITE_PORT}/talk",
    })


@mcp.tool()
def godot_read_file(path: str) -> dict:
    """Read a text file from the Godot project, e.g. res://plqyer.gd."""
    return call_godot("execute", {"code": f"""
func run(tree):
	var path = {path!r}
	if not FileAccess.file_exists(path):
		return {{"error": "missing", "path": path}}
	return {{"path": path, "text": FileAccess.get_file_as_string(path)}}
"""})


@mcp.tool()
def godot_write_file(path: str, text: str) -> dict:
    """Write a text file into the Godot project. Creates parent folders."""
    return call_godot("execute", {"code": f"""
func run(tree):
	var path = {path!r}
	var text = {text!r}
	var folder = path.get_base_dir()
	if folder != "":
		DirAccess.make_dir_recursive_absolute(folder)
	var f = FileAccess.open(path, FileAccess.WRITE)
	if f == null:
		return {{"error": FileAccess.get_open_error(), "path": path}}
	f.store_string(text)
	f.close()
	return {{"ok": true, "path": path, "bytes": text.length()}}
"""})


@mcp.tool()
def godot_list_project(folder: str = "res://") -> dict:
    """List .gd / .tscn files under a Godot folder."""
    return call_godot("execute", {"code": f"""
func run(tree):
	var files = []
	_walk({folder!r}, files, 0)
	return files

func _walk(prefix, files, depth):
	if depth > 4:
		return
	var dir = DirAccess.open(prefix)
	if dir == null:
		return
	dir.list_dir_begin()
	var name = dir.get_next()
	while name != "":
		if not name.begins_with("."):
			var path = prefix.path_join(name)
			if dir.current_is_dir():
				_walk(path, files, depth + 1)
			elif name.ends_with(".gd") or name.ends_with(".tscn"):
				files.append(path)
		name = dir.get_next()
	dir.list_dir_end()
"""})


@mcp.tool()
def godot_input_actions() -> dict:
    """List InputMap action names in the running game."""
    return call_godot("execute", {"code": """
func run(tree):
	return InputMap.get_actions()
"""})



# ---------------------------------------------------------------------------
# TurboWarp + Roblox cloud integrations
# ---------------------------------------------------------------------------

ROBLOX_API_KEY = os.environ.get("ROBLOX_API_KEY", "").strip()
ROBLOX_UNIVERSE_ID = os.environ.get("ROBLOX_UNIVERSE_ID", "").strip()
ROBLOX_PLACE_ID = os.environ.get("ROBLOX_PLACE_ID", "").strip()
ROBLOX_BASE_URL = "https://apis.roblox.com/cloud/v2"


def _http_json_request(url: str, method: str = "GET", body: Optional[dict] = None,
                       headers: Optional[dict] = None, timeout: float = 30) -> dict:
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
            raw = exc.read().decode("utf-8")
            detail = json.loads(raw) if raw else {}
        except Exception:
            detail = {"raw": str(exc)}
        return {"status": "error", "http_status": exc.code, "result": detail}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


def _roblox_ready() -> Optional[dict]:
    missing = []
    if not ROBLOX_API_KEY: missing.append("ROBLOX_API_KEY")
    if not ROBLOX_UNIVERSE_ID: missing.append("ROBLOX_UNIVERSE_ID")
    if missing:
        return {"status": "error", "message": "Missing Roblox environment variables: " + ", ".join(missing)}
    return None


def _roblox_request(path: str, method: str = "GET", body: Optional[dict] = None) -> dict:
    problem = _roblox_ready()
    if problem:
        return problem
    url = ROBLOX_BASE_URL.rstrip("/") + "/" + path.lstrip("/")
    return _http_json_request(url, method, body, {"x-api-key": ROBLOX_API_KEY}, timeout=45)


@mcp.tool()
def roblox_status() -> dict:
    """Show whether the MCP has Roblox Open Cloud credentials/configuration. Never returns the secret."""
    return _ok({
        "configured": bool(ROBLOX_API_KEY and ROBLOX_UNIVERSE_ID),
        "universe_id": ROBLOX_UNIVERSE_ID or None,
        "place_id": ROBLOX_PLACE_ID or None,
        "api_key_present": bool(ROBLOX_API_KEY),
        "api": "Roblox Open Cloud v2",
    })


@mcp.tool()
def roblox_list_data_stores(max_page_size: int = 25) -> dict:
    """List data stores in the configured Roblox universe."""
    return _roblox_request(
        f"universes/{urllib.parse.quote(ROBLOX_UNIVERSE_ID, safe='')}/data-stores?maxPageSize={max(1,min(int(max_page_size),100))}"
    )


@mcp.tool()
def roblox_get_data_store_entry(data_store_id: str, entry_id: str, scope_id: str = "") -> dict:
    """Read one Roblox Open Cloud data-store entry."""
    u = urllib.parse.quote(ROBLOX_UNIVERSE_ID, safe='')
    ds = urllib.parse.quote(data_store_id, safe='')
    eid = urllib.parse.quote(entry_id, safe='')
    if scope_id:
        path = f"universes/{u}/data-stores/{ds}/scopes/{urllib.parse.quote(scope_id, safe='')}/entries/{eid}"
    else:
        path = f"universes/{u}/data-stores/{ds}/entries/{eid}"
    return _roblox_request(path)


@mcp.tool()
def roblox_set_data_store_entry(data_store_id: str, entry_id: str, value_json: str, scope_id: str = "") -> dict:
    """Create/update a Roblox Open Cloud data-store entry. value_json must be valid JSON."""
    try:
        value = json.loads(value_json)
    except json.JSONDecodeError as exc:
        return {"status":"error", "message":f"value_json is not valid JSON: {exc}"}
    u = urllib.parse.quote(ROBLOX_UNIVERSE_ID, safe='')
    ds = urllib.parse.quote(data_store_id, safe='')
    eid = urllib.parse.quote(entry_id, safe='')
    if scope_id:
        path = f"universes/{u}/data-stores/{ds}/scopes/{urllib.parse.quote(scope_id, safe='')}/entries/{eid}"
    else:
        path = f"universes/{u}/data-stores/{ds}/entries/{eid}"
    return _roblox_request(path, "PATCH", {"value": json.dumps(value, separators=(",", ":"))})


@mcp.tool()
def roblox_increment_data_store_entry(data_store_id: str, entry_id: str, increment: float, scope_id: str = "") -> dict:
    """Increment a numeric Roblox Open Cloud data-store entry."""
    u = urllib.parse.quote(ROBLOX_UNIVERSE_ID, safe='')
    ds = urllib.parse.quote(data_store_id, safe='')
    eid = urllib.parse.quote(entry_id, safe='')
    if scope_id:
        path = f"universes/{u}/data-stores/{ds}/scopes/{urllib.parse.quote(scope_id, safe='')}/entries/{eid}:increment"
    else:
        path = f"universes/{u}/data-stores/{ds}/entries/{eid}:increment"
    return _roblox_request(path, "POST", {"increment": increment})


@mcp.tool()
def roblox_list_place_instances(place_id: Optional[str] = None, instance_id: str = "") -> dict:
    """List children of a Roblox Engine Open Cloud Instance. Requires a collaborative session and the appropriate key scope."""
    place = (place_id or ROBLOX_PLACE_ID).strip()
    if not place:
        return {"status":"error", "message":"Set ROBLOX_PLACE_ID or pass place_id."}
    u = urllib.parse.quote(ROBLOX_UNIVERSE_ID, safe='')
    p = urllib.parse.quote(place, safe='')
    iid = urllib.parse.quote(instance_id or "root", safe='')
    return _roblox_request(f"universes/{u}/places/{p}/instances/{iid}/list-children")


@mcp.tool()
def roblox_get_instance(instance_id: str, place_id: Optional[str] = None) -> dict:
    """Get one Roblox Engine Open Cloud Instance."""
    place = (place_id or ROBLOX_PLACE_ID).strip()
    if not place:
        return {"status":"error", "message":"Set ROBLOX_PLACE_ID or pass place_id."}
    u = urllib.parse.quote(ROBLOX_UNIVERSE_ID, safe='')
    p = urllib.parse.quote(place, safe='')
    iid = urllib.parse.quote(instance_id, safe='')
    return _roblox_request(f"universes/{u}/places/{p}/instances/{iid}")


@mcp.tool()
def roblox_update_script(instance_id: str, source: str, place_id: Optional[str] = None) -> dict:
    """Update a Roblox Script/LocalScript/ModuleScript through Engine Open Cloud. Beta API; Studio-open scripts cannot be updated."""
    place = (place_id or ROBLOX_PLACE_ID).strip()
    if not place:
        return {"status":"error", "message":"Set ROBLOX_PLACE_ID or pass place_id."}
    if len(source.encode("utf-8")) > 200 * 1024:
        return {"status":"error", "message":"Roblox Engine Open Cloud limits update request bodies to 200 KB."}
    u = urllib.parse.quote(ROBLOX_UNIVERSE_ID, safe='')
    p = urllib.parse.quote(place, safe='')
    iid = urllib.parse.quote(instance_id, safe='')
    return _roblox_request(f"universes/{u}/places/{p}/instances/{iid}", "PATCH", {"engineInstance": {"Details": {"Source": source}}})


@mcp.tool()
def roblox_wait_operation(operation_path: str, attempts: int = 10, delay_seconds: float = 2.0) -> dict:
    """Poll a Roblox Open Cloud operation returned by asynchronous Engine APIs."""
    if not operation_path:
        return {"status":"error", "message":"operation_path is required"}
    attempts = max(1, min(int(attempts), 20))
    delay_seconds = max(0.25, min(float(delay_seconds), 10.0))
    for _ in range(attempts):
        result = _roblox_request(operation_path)
        if result.get("status") == "error":
            return result
        payload = result.get("result") or {}
        if payload.get("done") is True:
            return result
        time.sleep(delay_seconds)
    return {"status":"timeout", "message":"Operation is still running.", "operation_path":operation_path}


# TurboWarp talks to the same server through a small CORS-enabled HTTP API.
# The extension is served by this server, so there is only one public origin.

def _turbowarp_extension_js() -> str:
    server = PUBLIC_MCP_URL.rstrip("/") if PUBLIC_MCP_URL else ""
    return r"""(function(Scratch) {
  "use strict";
  const SERVER = "__SERVER__";
  const ext = {
    _last: "",
    _connected: false,
    _request(path, payload) {
      return Scratch.fetch(SERVER + path, {
        method: "POST",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify(payload || {})
      }).then(r => r.text()).then(t => {
        try { return JSON.parse(t); } catch (_) { return {status:"error", message:t}; }
      }).catch(e => ({status:"error", message:String(e)}));
    },
    getInfo() {
      return {
        id: "feranmimcp",
        name: "Feranmi MCP",
        color1: "#5b4bff",
        color2: "#4639d9",
        color3: "#3128a8",
        blocks: [
          {opcode:"connect", blockType:Scratch.BlockType.COMMAND, text:"MCP connect"},
          {opcode:"connected", blockType:Scratch.BlockType.BOOLEAN, text:"MCP connected?"},
          {opcode:"ask", blockType:Scratch.BlockType.REPORTER, text:"MCP ask agent [TASK]", arguments:{TASK:{type:Scratch.ArgumentType.STRING,defaultValue:"What should I build next?"}}},
          {opcode:"getStatus", blockType:Scratch.BlockType.REPORTER, text:"MCP project status"},
          {opcode:"listSprites", blockType:Scratch.BlockType.REPORTER, text:"MCP list sprites"},
          {opcode:"getSprite", blockType:Scratch.BlockType.REPORTER, text:"MCP sprite info [NAME]", arguments:{NAME:{type:Scratch.ArgumentType.STRING,defaultValue:"Sprite1"}}},
          {opcode:"setSpritePosition", blockType:Scratch.BlockType.COMMAND, text:"MCP set [NAME] x [X] y [Y]", arguments:{NAME:{type:Scratch.ArgumentType.STRING,defaultValue:"Sprite1"},X:{type:Scratch.ArgumentType.NUMBER,defaultValue:0},Y:{type:Scratch.ArgumentType.NUMBER,defaultValue:0}}},
          {opcode:"broadcast", blockType:Scratch.BlockType.COMMAND, text:"MCP broadcast [MESSAGE]", arguments:{MESSAGE:{type:Scratch.ArgumentType.STRING,defaultValue:"hello"}}},
          {opcode:"lastResult", blockType:Scratch.BlockType.REPORTER, text:"MCP last result"}
        ]
      };
    },
    connect() { this._connected = true; this._last = "connected to " + SERVER; },
    connected() { return this._connected; },
    ask(args) { return this._request("/api/turbowarp", {action:"ask", task:String(args.TASK)}).then(x => { this._last=JSON.stringify(x); return x.result?.message || x.message || ""; }); },
    getStatus() { return this._request("/api/turbowarp", {action:"status"}).then(x => {this._last=JSON.stringify(x); return JSON.stringify(x.result || x);}); },
    _targets() {
      try { return Scratch.vm?.runtime?.targets || []; } catch (_) { return []; }
    },
    listSprites() {
      const local = this._targets().filter(t => !t.isStage).map(t => ({name:t.getName ? t.getName() : t.sprite?.name || t.name, x:t.x, y:t.y, visible:t.visible}));
      if (local.length) { this._last=JSON.stringify(local); return Promise.resolve(JSON.stringify(local)); }
      return this._request("/api/turbowarp", {action:"list_sprites"}).then(x => {this._last=JSON.stringify(x); return JSON.stringify(x.result || x);});
    },
    getSprite(args) {
      const wanted=String(args.NAME).toLowerCase();
      const t=this._targets().find(t => String(t.getName ? t.getName() : t.sprite?.name || t.name).toLowerCase() === wanted);
      if (t) { const out={name:t.getName ? t.getName() : t.sprite?.name || t.name,x:t.x,y:t.y,visible:t.visible}; this._last=JSON.stringify(out); return Promise.resolve(JSON.stringify(out)); }
      return this._request("/api/turbowarp", {action:"sprite_info", name:String(args.NAME)}).then(x => {this._last=JSON.stringify(x); return JSON.stringify(x.result || x);});
    },
    setSpritePosition(args) {
      const wanted=String(args.NAME).toLowerCase();
      const t=this._targets().find(t => String(t.getName ? t.getName() : t.sprite?.name || t.name).toLowerCase() === wanted);
      if (t && typeof t.setXY === "function") { t.setXY(Number(args.X), Number(args.Y)); this._last="moved "+String(args.NAME); return; }
      return this._request("/api/turbowarp", {action:"set_sprite_position", name:String(args.NAME), x:Number(args.X), y:Number(args.Y)}).then(x => {this._last=JSON.stringify(x);});
    },
    broadcast(args) {
      const message=String(args.MESSAGE);
      try { if (Scratch.vm?.runtime?.startHats) Scratch.vm.runtime.startHats("event_whenbroadcastreceived", {BROADCAST_OPTION:message}); } catch (_) {}
      return this._request("/api/turbowarp", {action:"broadcast", message}).then(x => {this._last=JSON.stringify(x);});
    },
    lastResult() { return this._last; }
  };
  Scratch.extensions.register(ext);
})(Scratch);
""".replace("__SERVER__", server)


def _turbowarp_result(action: str, data: dict) -> dict:
    """Validate and normalize browser-facing TurboWarp requests."""
    action = str(action or "").strip().lower()
    if action == "status":
        return _ok({
            "server": "FeranmiMCP",
            "turbowarp": "connected through same-origin extension endpoint",
            "roblox_configured": bool(ROBLOX_API_KEY and ROBLOX_UNIVERSE_ID),
            "ai_agent_configured": bool(OPENAI_API_KEY),
        })
    if action == "list_sprites":
        return _ok({"hint":"The extension can access the TurboWarp VM only when loaded unsandboxed; use the sprite blocks in the project to retrieve VM-backed data."})
    if action == "sprite_info":
        return _ok({"name": str(data.get("name") or "")[:100], "note":"Sprite metadata is returned by the extension-side VM in future bridge versions."})
    if action == "set_sprite_position":
        return _ok({"accepted": True, "name":str(data.get("name") or "")[:100], "x":float(data.get("x",0)), "y":float(data.get("y",0))})
    if action == "broadcast":
        return _ok({"accepted": True, "message":str(data.get("message") or "")[:500]})
    if action == "ask":
        task = str(data.get("task") or "").strip()[:4000]
        if not task:
            return {"status":"error", "message":"task is required"}
        return mcp_agent(task, context="TurboWarp requested this task through the Feranmi MCP extension.", max_turns=6)
    return {"status":"error", "message":f"Unknown TurboWarp action: {action}"}


def _site_html() -> str:
    mcp_url = PUBLIC_MCP_URL or f"http://127.0.0.1:8000/mcp"
    talk_url = f"http://127.0.0.1:{SITE_PORT}/talk"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Feranmi MCP Bridge</title>
  <style>
    body {{ font-family: sans-serif; max-width: 42rem; margin: 2rem auto; padding: 0 1rem; line-height: 1.45; }}
    code {{ background: #eee; padding: 0.1rem 0.3rem; }}
    pre {{ background: #111; color: #eee; padding: 0.8rem; overflow: auto; }}
  </style>
</head>
<body>
  <h1>Feranmi MCP Bridge</h1>
  <p>One machine. Blender on <code>9876</code>. Godot on <code>9877</code>. AIs share this page.</p>
  <h2>Connector URL (paste once)</h2>
  <pre>{mcp_url}</pre>
  <p>Browser clients need <strong>https</strong>. The same server also exposes <code>/turbowarp-extension.js</code> and Roblox Open Cloud tools.</p>
  <h2>Talk board</h2>
  <p>POST JSON to <code>{talk_url}</code>: <code>{{"from":"grok","to":"all","text":"hello"}}</code></p>
  <p>Several AIs can read the board. Blender still runs one command at a time so the addon does not crash.</p>
</body>
</html>
"""


def start_site_server() -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def _token(self) -> str:
            return self.headers.get("Authorization") or self.headers.get("X-Site-Token") or ""

        def _send(self, code: int, body: Any, content_type: str = "application/json"):
            raw = body if isinstance(body, bytes) else (
                body.encode("utf-8") if isinstance(body, str) else json.dumps(body).encode("utf-8")
            )
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Site-Token")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.end_headers()

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path in ("/", "/index.html"):
                return self._send(200, _site_html(), "text/html; charset=utf-8")
            if path == "/health":
                return self._send(200, {"ok": True, "blender": f"{BLENDER_HOST}:{BLENDER_PORT}", "godot": f"{GODOT_HOST}:{GODOT_PORT}"})
            if path == "/talk":
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                after = int((query.get("after") or ["0"])[0] or 0)
                with _talk_lock:
                    rows = [m for m in _talk_load().get("messages", []) if m.get("id", 0) > after]
                return self._send(200, {"messages": rows[-50:], "file": TALK_FILE})
            if path == "/turbowarp-extension.js":
                return self._send(200, _turbowarp_extension_js(), "application/javascript; charset=utf-8")
            if path == "/mcp-info":
                return self._send(200, {"mcp": PUBLIC_MCP_URL or "http://127.0.0.1:8000/mcp", "turbowarp_extension": (PUBLIC_MCP_URL.rstrip("/") if PUBLIC_MCP_URL else "") + "/turbowarp-extension.js", "token_required": bool(SITE_TOKEN)})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            if SITE_TOKEN and not _authorized(self._token()):
                return self._send(401, {"error": "bad token"})
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                return self._send(400, {"error": "invalid json"})
            if path == "/api/turbowarp":
                result = _turbowarp_result(data.get("action", ""), data)
                return self._send(200 if result.get("status") != "error" else 400, result)
            if path == "/talk":
                text = str(data.get("text") or "")[:2000]
                if not text:
                    return self._send(400, {"error": "text required"})
                item = _talk_add(str(data.get("from") or "ai")[:40], text, str(data.get("to") or "all")[:40])
                return self._send(200, item)
            self._send(404, {"error": "not found"})

        def log_message(self, fmt: str, *args) -> None:
            sys.stderr.write("[site] " + (fmt % args) + "\n")

    server = ThreadingHTTPServer((SITE_HOST, SITE_PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, name="mcp-site", daemon=True)
    thread.start()
    print(f"Site + talk board on http://{SITE_HOST}:{SITE_PORT}/", file=sys.stderr)


def start_public_mux(host: str, public_port: int, mcp_port: int) -> None:
    """One public port: /talk is local, /mcp is proxied to the internal MCP server.

    ngrok http 8000 then covers both local Grok and browser Claude.
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import http.client

    class Mux(BaseHTTPRequestHandler):
        def _send(self, code: int, body: Any, content_type: str = "application/json"):
            raw = body if isinstance(body, bytes) else (
                body.encode("utf-8") if isinstance(body, str) else json.dumps(body).encode("utf-8")
            )
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Site-Token")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.end_headers()

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path in ("/", "/index.html"):
                return self._send(200, _site_html(), "text/html; charset=utf-8")
            if path == "/health":
                return self._send(200, {"ok": True, "talk_file": TALK_FILE, "mcp": f"127.0.0.1:{mcp_port}"})
            if path == "/talk":
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                after = int((query.get("after") or ["0"])[0] or 0)
                with _talk_lock:
                    rows = [m for m in _talk_load().get("messages", []) if m.get("id", 0) > after]
                return self._send(200, {"messages": rows[-50:], "file": TALK_FILE})
            if path == "/turbowarp-extension.js":
                return self._send(200, _turbowarp_extension_js(), "application/javascript; charset=utf-8")
            if path == "/mcp-info":
                return self._send(200, {"mcp": (PUBLIC_MCP_URL.rstrip("/") if PUBLIC_MCP_URL else "") + "/mcp", "turbowarp_extension": (PUBLIC_MCP_URL.rstrip("/") if PUBLIC_MCP_URL else "") + "/turbowarp-extension.js", "token_required": bool(SITE_TOKEN), "roblox_configured": bool(ROBLOX_API_KEY and ROBLOX_UNIVERSE_ID)})
            if path.startswith("/mcp"):
                return self._proxy(mcp_port)
            self._send(404, {"error": "not found", "hint": "use /mcp or /talk"})

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            if path == "/api/turbowarp":
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    data = json.loads(raw.decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    return self._send(400, {"error":"invalid json"})
                result = _turbowarp_result(data.get("action", ""), data)
                return self._send(200 if result.get("status") != "error" else 400, result)
            if path == "/talk":
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    data = json.loads(raw.decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    return self._send(400, {"error": "invalid json"})
                text = str(data.get("text") or "")[:2000]
                if not text:
                    return self._send(400, {"error": "text required"})
                item = _talk_add(str(data.get("from") or "ai")[:40], text, str(data.get("to") or "all")[:40])
                return self._send(200, item)
            if path.startswith("/mcp"):
                return self._proxy(mcp_port)
            self._send(404, {"error": "not found"})

        def _proxy(self, port: int) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            headers = {k: v for k, v in self.headers.items() if k.lower() not in {"host", "connection"}}
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=SOCKET_TIMEOUT)
            try:
                conn.request(self.command, self.path, body=body, headers=headers)
                resp = conn.getresponse()
                self.send_response(resp.status, resp.reason)
                for key, value in resp.getheaders():
                    if key.lower() in {"transfer-encoding", "connection"}:
                        continue
                    self.send_header(key, value)
                self.send_header("Access-Control-Allow-Origin", "*")
                payload = resp.read()
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except Exception as exc:
                self._send(502, {"error": f"mcp proxy failed: {exc}"})
            finally:
                conn.close()

        def log_message(self, fmt: str, *args) -> None:
            sys.stderr.write("[mux] " + (fmt % args) + "\n")

    print(f"Public mux http://{host}:{public_port}/mcp and /talk  (MCP inside 127.0.0.1:{mcp_port})", file=sys.stderr)
    print(f"Talk file: {TALK_FILE}", file=sys.stderr)
    ThreadingHTTPServer((host if host != "127.0.0.1" else "0.0.0.0", public_port), Mux).serve_forever()


def _selftest():
    print(f"Connecting to Blender bridge at {BLENDER_HOST}:{BLENDER_PORT} ...")
    result = call_blender("ping")
    print(json.dumps(result, indent=2))
    if result.get("status") not in {"success", "ok"}:
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Blender MCP server (Peak modeling upgrade)")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.selftest:
        _selftest()
    else:
        start_site_server()
        if args.transport == "http":
            print(f"MCP  http://{args.host}:{args.port}/mcp", file=sys.stderr)
            print(f"Talk http://127.0.0.1:{SITE_PORT}/talk  file={TALK_FILE}", file=sys.stderr)
            mcp.run(transport="streamable-http", host=args.host, port=args.port)
        else:
            mcp.run(transport="stdio")
