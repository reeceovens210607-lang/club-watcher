@echo off
rem Starts the club ticket watcher in a minimised window. Close that window to stop it.
cd /d "%~dp0"
rem At login, give the network a moment to connect first
if /i "%~1"=="startup" timeout /t 30 /nobreak >nul
start "Club Watcher" /min python club_watcher.py
