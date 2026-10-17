@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Run run.bat once first, then run this file.
    pause
    goto :eof
)

echo Installing speech recognition (faster-whisper). This takes a few minutes...
".venv\Scripts\python.exe" -m pip install -r requirements-voice.txt
if errorlevel 1 (
    echo.
    echo Install failed. Check your internet connection and try again.
) else (
    echo.
    echo Done. Restart run.bat to use speech recognition.
)
pause
