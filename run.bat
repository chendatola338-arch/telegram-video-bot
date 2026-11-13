@echo off
set PYTHONIOENCODING=utf-8
title Telegram Bot & Mini App Downloader
echo ==========================================
echo Starting Telegram Video Downloader Server
echo ==========================================

:: Load environment variables if they exist
if exist .env (
    echo Loading configuration from .env...
) else (
    echo [Warning] .env file not found. Copying .env.example to .env...
    copy .env.example .env
)

:: Start FastAPI backend in a separate terminal window
echo Starting FastAPI Backend (Port 8000)...
start "FastAPI Downloader Server" cmd /k "python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload"

:: Wait 2 seconds for server to start
timeout /t 2 /nobreak > nul

:: Start Telegram Bot
echo Starting Telegram Bot...
python backend/bot.py

echo All services started!
pause
