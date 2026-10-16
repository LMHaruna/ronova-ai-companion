@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONDONTWRITEBYTECODE=1
set PYTHONNOUSERSITE=1
"%~dp0runtime\python\cpython-3.11.17-windows-x86_64-none\python.exe" "%~dp0scripts\setup_desktop.py" --ensure %*
set "RONOVA_SETUP_EXIT=%ERRORLEVEL%"
if /i not "%~1"=="--no-pause" pause
exit /b %RONOVA_SETUP_EXIT%
