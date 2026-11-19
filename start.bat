@echo off
REM Double-click on Windows to open Cat Watch.
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
title Cat Watch

if not exist "%~dp0requirements.txt" goto notunzipped
if not exist "%~dp0ui.py" goto notunzipped

set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY (
  where python >nul 2>nul && python -c "import sys" >nul 2>nul && set PY=python
)
if not defined PY (
  echo Python is needed. Install it from https://www.python.org/downloads/
  echo During install, tick "Add python.exe to PATH". Then open this file again.
  pause
  exit /b 1
)

where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo ffmpeg is needed. The easiest way: open PowerShell and run
  echo     winget install Gyan.FFmpeg
  echo then close this window and open start.bat again.
  pause
  exit /b 1
)

if exist "venv\.installed" goto run
echo Setting up. This takes a few minutes the first time...
if not exist "venv\Scripts\python.exe" %PY% -m venv venv
if not exist "venv\Scripts\python.exe" goto setupfail
"venv\Scripts\python.exe" -m pip install -q --upgrade pip
"venv\Scripts\python.exe" -m pip install -q -r "%~dp0requirements.txt"
if errorlevel 1 goto setupfail
type nul > "venv\.installed"

:run
"venv\Scripts\python.exe" ui.py
pause
exit /b 0

:notunzipped
echo Cat Watch is still inside the zip file, so its other files cannot be found.
echo.
echo   1. Right-click the downloaded zip and choose "Extract All..."
echo   2. Open the extracted cat-watch-agent folder
echo   3. Double-click start.bat in that folder
echo.
echo (This window was opened from: %~dp0)
pause
exit /b 1

:setupfail
echo.
echo Setup did not finish. Check the internet connection and open start.bat again.
echo If it keeps failing, send a photo of this window.
pause
exit /b 1
