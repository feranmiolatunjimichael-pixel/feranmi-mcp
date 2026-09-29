# Feranmi MCP on Render

One Render HTTPS service exposes all browser/cloud integrations:

- `GET /health` — Render health check
- `GET /` — bridge landing page
- `GET /mcp-info` — integration URLs and token status
- `/mcp` — streamable HTTP MCP endpoint
- `GET /turbowarp-extension.js` — TurboWarp extension
- `POST /api/turbowarp` — TurboWarp API
- `/talk` — talk board API

## TurboWarp

After deployment, load this URL in TurboWarp as a custom extension:

`https://YOUR-RENDER-SERVICE.onrender.com/turbowarp-extension.js`

The extension calls the same Render origin, so no second URL is needed.

## Roblox Open Cloud

In Render, open **Environment > Environment Variables** and add:

- `ROBLOX_API_KEY` — secret Roblox Open Cloud API key
- `ROBLOX_UNIVERSE_ID` — numeric universe ID
- `ROBLOX_PLACE_ID` — numeric place ID, required for instance/script tools

The server includes status, data-store, instance, script-update, and operation-polling MCP tools.
Never commit `ROBLOX_API_KEY` to GitHub.

## MCP authentication

Render generates `SITE_TOKEN`. Use it as a Bearer token if the MCP client asks for authentication:

`Authorization: Bearer YOUR_SITE_TOKEN`

## Blender/Godot networking note

Render cannot reach Blender or Godot running on your personal computer through
`127.0.0.1`. For live local-app control, run the bridge locally with a secure
Cloudflare Tunnel/reverse relay, or run the applications on the same host as the
server. Do not expose ports 9876 or 9877 directly to the internet.
