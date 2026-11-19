#!/bin/bash
# Double-click this file on a Mac to open Cat Watch.
cd "$(dirname "$0")"
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg is needed. Install it first:  brew install ffmpeg   (see https://brew.sh)"; read -p "Press Enter to close"; exit 1
fi
if [ ! -f venv/.installed ]; then
  echo "Setting up (a few minutes the first time)..."
  python3 -m venv venv && ./venv/bin/pip install -q -r requirements.txt && touch venv/.installed || { echo "Setup did not finish. Check the internet connection and open this again."; read -p "Press Enter to close"; exit 1; }
fi
./venv/bin/python ui.py
