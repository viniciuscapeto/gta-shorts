@echo off
cd /d "%~dp0"
python -m pip install -q --disable-pip-version-check numpy imageio-ffmpeg edge-tts
python fabrica.py
pause
