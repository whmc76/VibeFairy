@echo off
REM VibeFairy V2 — start daemon (Windows)
setlocal

set SCRIPT_DIR=%~dp0
set PROJECT_DIR=%SCRIPT_DIR%..

cd /d "%PROJECT_DIR%"

REM 确保依赖已安装
uv sync --quiet

REM Ensure data dirs
if not exist "data\logs" mkdir "data\logs"

echo [VibeFairy] Starting daemon...
.venv\Scripts\python.exe -m vibefairy run %*
