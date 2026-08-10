@echo off
REM usblcd-display GUI launcher
REM Double-click this file to open the app.

cd /d "%~dp0"

REM Use the project's own Python (venv) so Hermes/global PYTHONPATH
REM cannot hijack PIL and other imports.
set "PYTHONPATH="

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" usblcd_app.py
) else (
    echo venv not found - running  setup first...
    python -m venv .venv
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    ".venv\Scripts\python.exe" usblcd_app.py
)

pause
