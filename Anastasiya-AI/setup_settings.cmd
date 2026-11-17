@echo off
setlocal EnableExtensions
cd /d "%~dp0"
if not exist ".env" (
    if not exist ".env.example" (
        echo ERROR: .env.example is missing. Extract the entire ZIP to an empty folder.
        pause
        exit /b 1
    )
    copy /Y ".env.example" ".env" >nul
)
if exist ".env" (
    echo Settings file .env is ready. Keep it private.
    notepad ".env"
) else (
    echo ERROR: .env could not be created.
)
pause
