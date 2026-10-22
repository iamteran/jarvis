@echo off
title Installing JARVIS
cd /d "%~dp0"
echo.
echo   Installing Jarvis into .venv ...
echo.
where py >nul 2>nul
if %errorlevel%==0 (
  py -3.12 -m venv .venv 2>nul || py -3 -m venv .venv
) else (
  python -m venv .venv
)
if not exist ".venv\Scripts\python.exe" (
  echo   Could not create the virtual environment. Is Python 3.12 installed?
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install --upgrade pip --disable-pip-version-check
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
echo.
echo   Running diagnostics ...
".venv\Scripts\python.exe" -m jarvis --check
echo.
pause
