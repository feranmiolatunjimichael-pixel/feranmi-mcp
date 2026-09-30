@echo off
if "%RELAY_URL%"=="" (
  echo Set RELAY_URL and SITE_TOKEN first.
  echo Example: set RELAY_URL=https://feranmi-mcp.onrender.com
  exit /b 1
)
start "mcp" python server.py --transport http --host 127.0.0.1 --port 8000
timeout /t 2 >nul
python local_agent.py
