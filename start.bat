@echo off
REM Double-click on Windows to open Cat Watch. (Not yet tested by us on Windows.)
cd /d "%~dp0"
where ffmpeg >nul 2>nul || (echo ffmpeg is needed. Install it from https://ffmpeg.org then run this again. & pause & exit /b 1)
if not exist venv\.installed (
  echo Setting up, this takes a few minutes the first time...
  python -m venv venv && venv\Scripts\pip install -q -r requirements.txt && type nul > venv\.installed || (echo Setup did not finish. Check the internet connection and open this again. & pause & exit /b 1)
)
venv\Scripts\python ui.py
pause
