@echo off
title JARVIS
cd /d "%~dp0"
if not exist "data\logs" mkdir "data\logs"

:: Log file with timestamp for this session
set LOG_FILE=data\logs\jarvis_%date:~-4,4%%date:~-10,2%%date:~-7,2%_%time:~0,2%%time:~3,2%%time:~6,2%.log
set LOG_FILE=%LOG_FILE: =0%

if exist "%LOCALAPPDATA%\Programs\AVATAR\AVATAR.exe" (
  tasklist /FI "IMAGENAME eq AVATAR.exe" 2>nul | find /I "AVATAR.exe" >nul
  if errorlevel 1 start "" "%LOCALAPPDATA%\Programs\AVATAR\AVATAR.exe"
)
if not exist ".venv\Scripts\python.exe" (
  echo.
  echo   Jarvis is not installed yet.
  echo   Double-click install_jarvis.bat first, or see README.md.
  echo.
  pause
  exit /b 1
)

:loop
:: Run with stderr appended to log file; stdout still shows in console
".venv\Scripts\python.exe" -m jarvis %* 2>>"%LOG_FILE%"
set CODE=%errorlevel%
if "%CODE%"=="0" goto :done
if "%CODE%"=="2" goto :done
echo.
echo   Jarvis stopped unexpectedly (exit %CODE%). Restarting in 5 seconds...
echo   Press Ctrl+C twice to give up.
timeout /t 5 /nobreak >nul
goto loop

:done
