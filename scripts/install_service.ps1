# HydraMind V2 — Install as Windows Service (requires NSSM)
# Usage: .\scripts\install_service.ps1
# Requires: nssm.exe in PATH (https://nssm.cc/)

param(
    [string]$ServiceName = "HydraMind",
    [string]$ProjectDir = (Split-Path -Parent $PSScriptRoot),
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"

# Find Python
if (-not $PythonExe) {
    $PythonExe = (Get-Command python -ErrorAction SilentlyContinue)?.Source
    $VenvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
    if (Test-Path $VenvPython) {
        $PythonExe = $VenvPython
    }
}

if (-not $PythonExe -or -not (Test-Path $PythonExe)) {
    Write-Error "Python not found. Set -PythonExe parameter."
    exit 1
}

# Check nssm
if (-not (Get-Command nssm -ErrorAction SilentlyContinue)) {
    Write-Error "nssm not found. Install from https://nssm.cc/ and add to PATH."
    exit 1
}

Write-Host "Installing HydraMind as Windows service '$ServiceName'..."

nssm install $ServiceName $PythonExe
nssm set $ServiceName AppParameters "-m hydramind run"
nssm set $ServiceName AppDirectory $ProjectDir
nssm set $ServiceName DisplayName "HydraMind V2 — AI Assistant Daemon"
nssm set $ServiceName Description "Secure autonomous AI assistant daemon"
nssm set $ServiceName Start SERVICE_AUTO_START
nssm set $ServiceName AppStdout (Join-Path $ProjectDir "data\logs\service_stdout.log")
nssm set $ServiceName AppStderr (Join-Path $ProjectDir "data\logs\service_stderr.log")
nssm set $ServiceName AppRotateFiles 1
nssm set $ServiceName AppRotateSeconds 86400
nssm set $ServiceName AppRotateBytes 10485760

Write-Host "Service installed. Start with: nssm start $ServiceName"
Write-Host "Or: Start-Service $ServiceName"
