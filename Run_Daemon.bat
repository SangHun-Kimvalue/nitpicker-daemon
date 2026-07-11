@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

cd /d "%~dp0"

if /I "%~1"=="/h"     goto :usage
if /I "%~1"=="/?"     goto :usage
if /I "%~1"=="-h"     goto :usage
if /I "%~1"=="--help" goto :usage

call "%~dp0Set_Env.bat"
if errorlevel 1 ( echo [ERROR] Setup failed. & exit /b 1 )
call ".venv\Scripts\activate.bat"
set "PYTHONIOENCODING=utf-8"

if /I "%~1"=="--layer2" goto :layer2_daemon

REM --- Default: Layer 1 file watcher ---
echo [L1] Starting Mini Nitpicker file watcher...
echo      Watch: src\   Extensions: .py .cpp .h .hpp
echo      Ctrl+C to stop.
echo.
python bin\mini_nitpicker_daemon.py
endlocal & exit /b %ERRORLEVEL%

:layer2_daemon
echo [L2] Starting ReviewOrchestrator (10-agent, port 5555)...
echo      Ctrl+C to stop.
echo.
python bin\jemmin_daemon.py
endlocal & exit /b %ERRORLEVEL%

:usage
echo.
echo Run_Daemon  -  start individual service
echo.
echo   .\Run_Daemon.bat            Layer 1 file watcher (Gemini on save)
echo   .\Run_Daemon.bat --layer2   Layer 2 orchestrator (10-agent, ZMQ)
echo.
echo   For combined start: .\Run_Gemini.bat or .\Run_Local.bat
echo.
endlocal & exit /b 0
