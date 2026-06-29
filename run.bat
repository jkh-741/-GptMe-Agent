@echo off
cd /d "%~dp0"
call conda activate "%~dp0.venv"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
".\.venv\Scripts\gptme.exe" -m deepseek/deepseek-chat %*
