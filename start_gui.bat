@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title usblcd-display
set "PYTHONPATH="

REM ===== find python =====
set "PY="
where python >nul 2>&1
if errorlevel 1 goto try_pylauncher
set "PY=python"
goto have_python

:try_pylauncher
where py >nul 2>&1
if errorlevel 1 goto no_python
set "PY=py -3"
goto have_python

:no_python
echo [ERROR] Python 3.10+ not found.
echo Install it from https://www.python.org/downloads/
echo IMPORTANT: tick "Add python.exe to PATH" during install.
echo.
pause
exit /b 1

:have_python
echo Using: %PY%

REM ===== create venv if missing =====
if exist ".venv\Scripts\python.exe" goto deps_ok
echo First run - creating virtual environment...
%PY% -m venv .venv
if errorlevel 1 goto venv_fail
goto deps_ok

:venv_fail
echo [ERROR] Failed to create venv.
pause
exit /b 1

:deps_ok
".venv\Scripts\python.exe" -c "import PIL, usb, libusb" >nul 2>&1
if not errorlevel 1 goto launch

echo Installing dependencies (one-time)...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto deps_fail
goto launch

:deps_fail
echo [ERROR] Dependency install failed. Check your internet connection.
pause
exit /b 1

:launch
".venv\Scripts\python.exe" usblcd_app.py
if errorlevel 1 pause
endlocal
