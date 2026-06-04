@echo off
title JobScrape
cd /d "G:\claude\JobScrape"
echo Starting JobScrape server...
echo.
echo Web UI will open at http://127.0.0.1:5000
echo Close this window to stop the server.
echo.

:: Open the browser after a 3-second delay (in background)
start /min "" cmd /c "timeout /t 3 /nobreak >nul && start http://127.0.0.1:5000"

:: Start the app (blocks until closed)
uv run python run.py

pause
