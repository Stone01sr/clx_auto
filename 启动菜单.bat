@echo off
rem clx_auto launcher - ASCII only on purpose.
rem cmd.exe mis-parses multi-byte characters in UTF-8 .bat files, and GBK-encoded
rem files show up as garbage in UTF-8 editors. So all Chinese text lives in
rem manage/menu.py, which prints it reliably via the Windows console API.

rem --- Self-elevate, so double-clicking is enough and "Run as administrator"
rem --- can no longer be forgotten. fltmc is a System32 tool that only succeeds
rem --- when already elevated, which makes it a cheap admin check.
rem --- The path is handed to PowerShell through an environment variable instead
rem --- of being inlined into the command string: this file lives under a folder
rem --- whose name has spaces/non-ASCII characters, and $env: needs no quoting.
fltmc >nul 2>nul
if not errorlevel 1 goto :admin
set "CLX_SELF=%~f0"
powershell -NoProfile -Command "try { Start-Process -FilePath $env:CLX_SELF -Verb RunAs -ErrorAction Stop } catch { exit 1 }"
if errorlevel 1 (
    echo.
    echo [ERROR] Could not restart with administrator rights.
    echo Click "Yes" on the UAC prompt, or right-click this file and pick
    echo "Run as administrator".
    echo.
    pause
)
exit /b

:admin
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
