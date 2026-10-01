@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0首次安装.ps1"
if errorlevel 1 pause
