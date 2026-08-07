@echo off
rem Scheduled-task entry point for clx_auto. Also works by double-clicking.
rem ASCII only: cmd.exe mis-parses multi-byte characters in UTF-8 .bat files.
rem %~dp0 resolves to this file's own folder, so the path never needs editing.

chcp 65001 >nul
cd /d "%~dp0"

set PY=
if exist "C:\Users\74484\miniconda3\envs\wyclx_auto_env\python.exe" set PY="C:\Users\74484\miniconda3\envs\wyclx_auto_env\python.exe"
if not defined PY (
    where python >nul 2>nul
    if errorlevel 1 (
        echo [ERROR] Python not found. Install Python 3.11 with "Add Python to PATH".
        exit /b 1
    )
    set PY=python
)

%PY% -X utf8 daily_cleanup.py
