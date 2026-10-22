@echo off
title JARVIS
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo.
  echo   Jarvis is not installed yet.
  echo   Double-click install_jarvis.bat first, or see README.md.
  echo.
  pause
  exit /b 1
)

:loop
".venv\Scripts\python.exe" -m jarvis %*
set CODE=%errorlevel%
if "%CODE%"=="0" goto :done
if "%CODE%"=="2" goto :done
echo.
echo   Jarvis stopped unexpectedly (exit %CODE%). Restarting in 5 seconds...
echo   Press Ctrl+C twice to give up.
timeout /t 5 /nobreak >nul
goto loop

:done
