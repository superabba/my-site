@echo off
cd /d "%~dp0"

if not exist ".venv\installed.ok" (
  echo [Setup] First run: installing Python packages. This takes a few minutes...
  if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 goto nopython
  )
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 goto failed
  echo ok> ".venv\installed.ok"
)

echo.
echo Shorts Studio is starting. Your browser will open shortly.
echo Close this window to quit the program.
echo.
".venv\Scripts\python.exe" -m streamlit run streamlit_app.py
pause
goto :eof

:nopython
echo.
echo Python was not found. Install it from https://www.python.org/downloads/
echo During install, check "Add python.exe to PATH". Then run this file again.
pause
goto :eof

:failed
echo.
echo Package install failed. Check your internet connection and run this file again.
pause
