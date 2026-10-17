@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (set "PY=py -3") else (set "PY=python")

if not exist ".venv\Scripts\python.exe" (
    echo [1/2] First run: creating Python environment...
    %PY% -m venv .venv
    if errorlevel 1 goto nopython
)

".venv\Scripts\python.exe" -c "import streamlit, anthropic, imageio_ffmpeg, requests" >nul 2>nul
if errorlevel 1 (
    echo [2/2] Installing packages. This happens only once and takes a few minutes...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto pipfail
)

echo.
echo Shorts Uploader is starting. Your browser will open at http://localhost:8501
echo Keep this window open while you use the app. Close it to quit.
echo.
start "" /b cmd /c "timeout /t 4 /nobreak >nul & start "" http://localhost:8501"
".venv\Scripts\python.exe" -m streamlit run streamlit_app.py --server.headless true
pause
goto :eof

:nopython
echo.
echo Python was not found. Install Python 3.11 or newer from https://www.python.org/downloads/
echo During install, check "Add python.exe to PATH", then run this file again.
pause
goto :eof

:pipfail
echo.
echo Package install failed. Check your internet connection and run this file again.
pause
