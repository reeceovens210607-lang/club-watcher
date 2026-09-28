@echo off
rem Shows when each club tends to drop tickets.
cd /d "%~dp0"
python club_watcher.py report
echo.
pause
