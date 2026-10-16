@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONDONTWRITEBYTECODE=1
set PYTHONNOUSERSITE=1
"%~dp0runtime\python\cpython-3.11.17-windows-x86_64-none\python.exe" "%~dp0scripts\setup_desktop.py" --ensure
if errorlevel 1 goto failed
"%~dp0runtime\voice-venv\Scripts\python.exe" "%~dp0scripts\verify_desktop_migration.py" %*
if errorlevel 1 goto failed
pause
exit /b 0
:failed
echo Ronova environment setup failed. See the message above and the migration guide.
pause
exit /b 1
