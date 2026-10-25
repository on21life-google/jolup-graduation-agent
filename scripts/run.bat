@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0.."

if exist ".venv\Scripts\python.exe" goto check_python
py -3.12 -c "import sys; assert sys.version_info[:2] == (3, 12)" >nul 2>nul
if not errorlevel 1 goto create_with_launcher
python -c "import sys; assert sys.version_info[:2] == (3, 12)" >nul 2>nul
if errorlevel 1 goto missing_python
python -m venv .venv
if errorlevel 1 goto failed
goto check_python

:create_with_launcher
py -3.12 -m venv .venv
if errorlevel 1 goto failed

:check_python
.venv\Scripts\python.exe -c "import sys; assert sys.version_info[:2] == (3, 12)" >nul 2>nul
if errorlevel 1 goto wrong_python
set "REQ_FILE=requirements.lock"
if not exist "%REQ_FILE%" set "REQ_FILE=requirements.txt"
for /f "delims=" %%H in ('.venv\Scripts\python.exe -c "import hashlib; print(hashlib.sha256(open(r'%REQ_FILE%', 'rb').read()).hexdigest())"') do set "REQ_HASH=%%H"
if not defined REQ_HASH goto failed
set "INSTALLED_HASH="
if exist ".venv\.jolup_requirements_sha256" set /p INSTALLED_HASH=<.venv\.jolup_requirements_sha256
if "%REQ_HASH%"=="%INSTALLED_HASH%" goto launch
echo Installing dependencies. Internet is required on the first run.
.venv\Scripts\python.exe -m pip install --disable-pip-version-check --cache-dir .cache\pip -r "%REQ_FILE%"
if errorlevel 1 goto failed
echo %REQ_HASH%>.venv\.jolup_requirements_sha256

:launch
echo Open http://127.0.0.1:8501 in your browser.
echo Press Ctrl+C in this window to stop the app.
.venv\Scripts\python.exe -m streamlit run app.py --server.address=127.0.0.1 --server.port=8501 --server.maxUploadSize=2 --browser.gatherUsageStats=false
if errorlevel 1 goto failed
exit /b 0

:missing_python
echo Python 3.12 is required. See docs\RUN.md.
goto failed

:wrong_python
echo This .venv is not Python 3.12. Extract the shared ZIP into a new folder and try again.

:failed
echo Setup or launch failed. Keep the error message and share it with the team.
pause
exit /b 1
