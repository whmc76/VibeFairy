@echo off
REM VibeFairy — start daemon (Windows)
setlocal

set SCRIPT_DIR=%~dp0
set PROJECT_DIR=%SCRIPT_DIR%..

cd /d "%PROJECT_DIR%"

REM Activate venv if present
if exist ".venv\Scripts\activate.bat" (
    call .venv\Scripts\activate.bat
)

REM Ensure data dirs
if not exist "data\logs" mkdir "data\logs"

echo [VibeFairy] Starting daemon...
python -m vibefairy run %*
