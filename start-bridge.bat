@echo off
REM Start the bridge on this Windows PC (Task Scheduler: \OpenXiaoAI-Bridge). Secrets: .hermes_api_key
cd /d D:\ai\open-xiaoai-bridge
if not exist logs mkdir logs

set API_SERVER_ENABLE=1
set API_SERVER_HOST=127.0.0.1
set API_SERVER_PORT=9092
REM AUDIO_INPUT=1: VAD/KWS models installed under core\models
set AUDIO_INPUT_ENABLE=1
REM MUSE_ENABLE=1: "ni hao miu si" wake words (KWS + XiaoAI ASR), routed to the OpenAI/Hermes flow
set MUSE_ENABLE=1

REM Hermes (OpenAI-compatible continuous dialogue)
set OPENAI_ENABLE=1
set OPENAI_BASE_URL=https://hermes.oci.sounfury.top/v1
set OPENAI_MODEL=muse-home
set OPENAI_API_KEY=
if exist .hermes_api_key (
  set /p OPENAI_API_KEY=<.hermes_api_key
)
if not defined OPENAI_API_KEY (
  echo [OpenXiaoAI] ERROR: .hermes_api_key missing or empty >> logs\bridge-autostart.log
)

set LOGLEVEL=INFO
set PYTHONIOENCODING=utf-8
set PYTHONFAULTHANDLER=1
set PATH=%USERPROFILE%\.cargo\bin;%USERPROFILE%\.local\bin;%PATH%

.venv\Scripts\python.exe main.py >> logs\bridge-autostart.log 2>&1
