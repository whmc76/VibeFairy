@echo off
chcp 65001 >nul
setlocal

set PROJECT_DIR=%~dp0
cd /d "%PROJECT_DIR%"

REM 检查 uv 是否安装
where uv >nul 2>&1
if errorlevel 1 (
    echo 正在安装 uv 包管理器...
    pip install uv --quiet
    if errorlevel 1 (
        echo 尝试通过 PowerShell 安装 uv...
        powershell -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    )
)

REM 用 uv sync 自动创建 .venv 并安装依赖（有缓存时几乎瞬间完成）
uv sync --quiet

REM 启动 GUI
start "" .venv\Scripts\pythonw.exe launcher.py
