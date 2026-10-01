@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0启动童声配音台.ps1"
if errorlevel 1 pause
