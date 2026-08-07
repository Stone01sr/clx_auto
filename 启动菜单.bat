@echo off
rem clx_auto launcher - ASCII only on purpose.
rem cmd.exe mis-parses multi-byte characters in UTF-8 .bat files, and GBK-encoded
rem files show up as garbage in UTF-8 editors. So all Chinese text lives in
rem manage/menu.py, which prints it reliably via the Windows console API.

chcp 65001 >nul
cd /d "%~dp0"

set PY=
if exist "C:\Users\74484\miniconda3\envs\wyclx_auto_env\python.exe" set PY="C:\Users\74484\miniconda3\envs\wyclx_auto_env\python.exe"
if not defined PY (
    where python >nul 2>nul
    if errorlevel 1 (
        echo.
        echo [ERROR] Python not found.
        echo Please install Python 3.11 and check "Add Python to PATH" during setup.
        echo.
        pause
        exit /b 1
    )
    set PY=python
)

%PY% -X utf8 "manage\menu.py"
if errorlevel 1 pause
