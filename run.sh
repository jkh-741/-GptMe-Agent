#!/usr/bin/env bash
cd /d/Desktop/gptme || exit 1
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
./.venv/Scripts/gptme.exe -m deepseek/deepseek-chat "$@"
