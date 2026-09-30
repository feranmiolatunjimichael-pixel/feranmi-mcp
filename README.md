# Feranmi stable URL

Browser AIs cannot see `127.0.0.1`. Render also cannot see your Blender.
This package splits the two jobs.

```
Browser AI
    |
    |  https://YOUR-SERVICE.onrender.com/mcp     stable URL
    v
Render  cloud_relay.py
    ^
    |  PC dials OUT (no router setup)
    |
local_agent.py
    |
    v
server.py :8000
    |-- 9876 Blender
    |-- 9877 Godot
```

Talk (`/talk`) lives on Render, so Claude and Grok can chat even if Blender is closed.
`/mcp` only works while `local_agent.py` is running.

TurboWarp extension (stable URL):

`https://YOUR-SERVICE.onrender.com/turbowarp-extension.js`

Roblox Open Cloud runs on Render, not on the PC. Set these on Render:

- `ROBLOX_API_KEY`
- `ROBLOX_UNIVERSE_ID`
- `ROBLOX_PLACE_ID`

Check with `GET /api/roblox/status`.

The local `server.py` still has the full Roblox MCP tools. Those ride through `/mcp` when the PC agent is online. TurboWarp cannot control the Scratch project from the cloud; the extension only calls this server.

## Render (once)

1. Push this folder to GitHub.
2. Render: New Blueprint, pick `render.yaml`.
3. Copy the service URL and the generated `SITE_TOKEN`.

That URL does not change when you restart your PC.

Free Render sleeps after about 15 minutes of no traffic. The URL stays the same.
The first hit after sleep can take 30–60 seconds.

## PC (every session)

```bat
python server.py --transport http --host 127.0.0.1 --port 8000
set RELAY_URL=https://YOUR-SERVICE.onrender.com
set SITE_TOKEN=the-render-token
python local_agent.py
```

Blender Start Server (9876) and Godot Play (9877) stay as they are.

## Browser AI connector

```
https://YOUR-SERVICE.onrender.com/mcp
```

If it asks for a header:

```
Authorization: Bearer YOUR_SITE_TOKEN
```

Team chat:

```
https://YOUR-SERVICE.onrender.com/talk
```

## What was wrong before

The old entrypoint started the Blender server *on Render* and set
`BLENDER_HOST=127.0.0.1`. That is Render's own loopback, not your PC.
Port 9876 on Render is empty, so every tool timed out.
