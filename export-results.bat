@echo off
rem Windows: double-click this file.
cd /d "%~dp0"
set PYTHONUTF8=1
set "PATH=%USERPROFILE%\.local\bin;%PATH%"
rem The app's Python environment lives outside this folder (short paths, no OneDrive sync).
set "UV_PROJECT_ENVIRONMENT=%LOCALAPPDATA%\dot-voice-agent\venv"
where uv >nul 2>nul
if errorlevel 1 (
  echo First time only: installing the tools this needs...
  powershell -NoProfile -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex" >nul
)
where uv >nul 2>nul
if errorlevel 1 (
  echo.
  echo Couldn't download the tools. Check your internet connection and try again.
  pause
  exit /b 1
)
if not exist "%UV_PROJECT_ENVIRONMENT%" echo First time only: downloading what the agent needs. This takes about 5 minutes...
uv run --no-dev --python 3.12 python -m scripts.export
echo.
pause
