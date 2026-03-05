@echo off
chcp 65001 >nul
setlocal

set PROJECT_DIR=%~dp0
cd /d "%PROJECT_DIR%"

REM 激活虚拟环境（如果存在）
if exist ".venv\Scripts\activate.bat" (
    call .venv\Scripts\activate.bat
)

REM 确保包已安装
python -c "import claudefairy" >nul 2>&1
if errorlevel 1 (
    echo 正在安装 claudefairy...
    pip install -e . --quiet
)

REM 启动 GUI
start "" pythonw launcher.py
