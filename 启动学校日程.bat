@echo off
chcp 65001 >nul
schtasks /Run /TN SchoolAssistant >nul 2>&1
timeout /t 2 /nobreak >nul
start "" http://127.0.0.1:8765
