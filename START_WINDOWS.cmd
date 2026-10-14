@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  if errorlevel 1 goto fail
)
".venv\Scripts\python.exe" -m pip install -e ".[dev]"
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m market_agent setup
if errorlevel 1 goto fail
echo.
echo Open http://127.0.0.1:8000 and use API_TOKEN from .env
echo Keep this window open while the agent is running.
".venv\Scripts\python.exe" -m market_agent serve
if errorlevel 1 goto fail
exit /b 0
:fail
echo.
echo Setup or startup failed. Copy the error above, without API keys.
pause
exit /b 1
