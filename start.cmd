@echo off
setlocal
cd /d "%~dp0"

if /I "%~1"=="--check" (
    pwsh.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1" -Check
) else (
    pwsh.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1"
)

exit /b %ERRORLEVEL%
