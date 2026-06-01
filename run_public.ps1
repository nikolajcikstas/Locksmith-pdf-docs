$ErrorActionPreference = "Stop"

$env:LOCKSMITH_HOST = "0.0.0.0"
$env:LOCKSMITH_PORT = "8001"

$Python = "C:\Users\User\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path

Set-Location $AppDir
Write-Host "Starting Locksmith Vehicle Docs on http://0.0.0.0:8001"
Write-Host "Use Ctrl+C to stop the server."
& $Python "$AppDir\app.py"
