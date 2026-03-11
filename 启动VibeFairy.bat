@echo off
chcp 65001 >nul
setlocal

set PROJECT_DIR=%~dp0
cd /d "%PROJECT_DIR%"

if exist ".venv\Scripts\activate.bat" (
    call .venv\Scripts\activate.bat
)

python -c "import vibefairy" >nul 2>&1
if errorlevel 1 (
    echo 正在安装 vibefairy...
    pip install -e . --quiet
)

start "" pythonw launcher.py
