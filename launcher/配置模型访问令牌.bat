@echo off
set "PROJECT_ROOT=%~dp0.."
"%PROJECT_ROOT%\work\model-runtime\venv\Scripts\python.exe" "%~dp0configure_hf_token.py"
pause
