@echo off
setlocal
cd /d "%~dp0"
title CouchPad

rem Restarted by the updater below: admin rights were already decided, the update is done.
if defined PM_UPDATED goto :elevated

rem --- Offer to run as administrator ---
rem Windows drops input sent by a normal program into admin windows (Task Manager,
rem On-Screen Keyboard, installers), so CouchPad needs admin rights to control them.
net session >nul 2>nul && goto :elevated
choice /c YN /t 10 /d N /m "Run as administrator so CouchPad can control Task Manager and other admin windows"
if errorlevel 2 goto :elevated
if "%~1"=="" (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs" && exit /b 0
) else (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs" && exit /b 0
)
echo Could not start as administrator; continuing without admin rights.

:elevated
set "VENV_PY=.venv\Scripts\python.exe"
if exist "%VENV_PY%" goto :update

rem --- Find Python 3.11+ to create the virtual environment ---
set "PY="
where py >nul 2>nul && py -3 -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>nul && set "PY=py -3"
if not defined PY (
    where python >nul 2>nul && python -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo Python 3.11 or newer was not found.
    echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
    pause
    exit /b 1
)

echo Creating virtual environment in .venv ...
%PY% -m venv .venv || (echo Failed to create the virtual environment. & pause & exit /b 1)

:update
rem --- Update to the latest release (skip with  start.bat --no-update) ---
rem cmd reads the whole block before running it, so the update may replace this file.
if not defined PM_UPDATED (
    "%VENV_PY%" -m server.update %*
    if errorlevel 3 (
        set "PM_UPDATED=1"
        call "%~f0" %*
        exit /b
    )
)

:deps
"%VENV_PY%" -c "import fastapi, uvicorn, websockets, pynput, segno, nacl" >nul 2>nul
if errorlevel 1 (
    echo Installing requirements ...
    "%VENV_PY%" -m pip install --disable-pip-version-check -r requirements.txt || (echo Failed to install requirements. & pause & exit /b 1)
)

"%VENV_PY%" -m server %*
if errorlevel 1 pause
