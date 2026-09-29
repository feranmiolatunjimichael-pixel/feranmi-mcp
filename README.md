# Feranmi MCP on Render

This bundle exposes the MCP server through one Render HTTPS service:

- `/mcp` — streamable HTTP MCP endpoint
- `/talk` — talk board API
- `/health` — health check
- `/turbowarp-extension.js` — generated extension

## Important Blender networking note

Render cannot reach Blender running on your personal computer through `127.0.0.1`.
For live Blender control, either run Blender on the same host as this service or add a
secure reverse relay/tunnel. Do not expose Blender port 9876 directly to the internet.

## Render start command

`python render_entrypoint.py`
