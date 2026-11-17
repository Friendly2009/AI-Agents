@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHON_CMD=python"
where py >nul 2>&1
if not errorlevel 1 (
    py -3 -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PYTHON_CMD=py -3"
)
%PYTHON_CMD% -c "import sys" >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python 3.11 or newer is required. Install Python and check PATH.
    echo Download from https://www.python.org/downloads/windows/
    pause
    exit /b 1
)
echo Checking configuration offline...
%PYTHON_CMD% doctor.py
set "RESULT=%ERRORLEVEL%"
if errorlevel 1 (
    echo.
    echo ERROR: The command failed. Please send a screenshot of this window, without secrets.
) else (
    echo.
    echo Done.
)
pause
exit /b %RESULT%
