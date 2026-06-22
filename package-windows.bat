@echo off
setlocal
chcp 65001 >nul 2>&1

cd /d "%~dp0"

where powershell >nul 2>nul
if %errorlevel% neq 0 (
  echo PowerShell is required to build the Windows installer.
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\desktop\build-windows.ps1" %*
exit /b %errorlevel%
