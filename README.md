# Cat Watch

Helps find a missing cat using your **own** home cameras, entirely on your **own** computer.

- **Search past recordings** from when the cat went missing (TP-Link VIGI; TP-Link Tapo experimental).
- **Check video files** downloaded from any camera's app or memory card (Tapo, Eufy, any brand).
- **Watch live** and get a desktop notification when something cat-shaped appears.
- Every find is saved as a photo with the camera and the exact recorded time, labelled **Cat**, **Dog-like** or **Movement**
  (movement catches cats the detector cannot recognise, e.g. half hidden behind a car).
- Optional **Ask Claude** second opinion on a photo, only if Claude Code is installed (this one sends that photo to Anthropic).

## Privacy

- Runs only on this computer; the page is served on `127.0.0.1` and refuses other hosts.
- Video and photos are never uploaded. The detection model is downloaded once during setup.
- Camera passwords are saved only in `~/.catwatch/config.json` (readable only by your user).

## Start

- **Mac:** double-click `start.command`. **Windows:** double-click `start.bat` (not yet tested on Windows).
- Needs Python 3.9+ and `ffmpeg` (`brew install ffmpeg` on Mac).
- The page opens in your browser: add a camera, then search, check files or watch live.

## Camera notes

- **VIGI:** found automatically on your Wi-Fi; the search reads the camera's own recordings.
- **Tapo:** create a *Camera Account* in the Tapo app (camera settings, Advanced settings, Camera account) and use it in Cat Watch.
  Searching the memory card also needs the Tapo account password (uses the unofficial `pytapo` library; untested on a real Tapo).
- **Others (Eufy etc.):** download clips from the camera's app and use *Check video files*. Live watching works if the camera offers an RTSP stream.

## Limits

- It finds cat- or dog-shaped animals, not *your* cat: look at each photo.
- Dark cats at night, small or half-hidden cats can be missed; movement results include shadows and leaves.
- The camera sends old video at about 30x real time, so three days of recordings take about 2-3 hours.

## Files

- `ui.py` / `ui.html`: the local page. `agent.py`: detection, search, live watching, video files. `setup.py`: camera discovery and command-line setup.

The detection model (Ultralytics YOLO) is AGPL-3.0 licensed.
