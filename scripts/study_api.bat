@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0.."
.venv\Scripts\python.exe -c "import fastapi, uvicorn" >nul 2>nul
if errorlevel 1 (
  echo First install the project dependencies with scripts\run.bat.
  pause
  exit /b 1
)
echo Swagger: http://127.0.0.1:8001/docs
echo Press Ctrl+C to stop. Code changes reload the server.
.venv\Scripts\python.exe -m uvicorn jolup.study_api:app --host 127.0.0.1 --port 8001 --reload --reload-dir jolup
