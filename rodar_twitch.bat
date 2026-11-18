@echo off
cd /d "%~dp0"
python -m pip install -q --disable-pip-version-check -U numpy imageio-ffmpeg edge-tts "yt-dlp[default]"
python -m pip install -q --disable-pip-version-check -U deno
python fabrica.py --twitch
pause
