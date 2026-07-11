@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

cd /d "%~dp0"

if /I "%~1"=="/h"     goto :usage
if /I "%~1"=="/?"     goto :usage
if /I "%~1"=="-h"     goto :usage
if /I "%~1"=="--help" goto :usage
if /I "%~1"=="--review"   goto :manual_review
if /I "%~1"=="--staged"   goto :staged_review
if /I "%~1"=="--auto-fix" goto :auto_fix

call "%~dp0Set_Env.bat"
if errorlevel 1 ( echo [ERROR] Setup failed. & exit /b 1 )
call ".venv\Scripts\activate.bat"
set "PYTHONIOENCODING=utf-8"

REM ============================================================
REM  Default: Layer 1 file watcher daemon (Gemini direct review)
REM ============================================================
echo.
echo ============================================================
echo  Run_Watch  -  Layer 1 file watcher  (Gemini direct)
echo ============================================================
echo  Watches src\ for .py .cpp .h .hpp changes
echo  On save -> Gemini API review -> .jemmin\logs\latest_review.txt
echo.
echo  Prompt: config\system_prompt.md  (review persona + rules)
echo  No 10-agent pipeline. Fast single-call review.
echo  For deep review: Run_Gemini.bat or Run_Local.bat
echo ============================================================
echo.

python bin\mini_nitpicker_daemon.py
set "EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %EXIT_CODE%

REM ============================================================
:manual_review
REM  Run_Watch.bat --review src\foo.py [src\bar.py ...]
REM ============================================================
call "%~dp0Set_Env.bat"
if errorlevel 1 ( echo [ERROR] Setup failed. & exit /b 1 )
call ".venv\Scripts\activate.bat"
set "PYTHONIOENCODING=utf-8"
shift

if "%~1"=="" (
    echo [ERROR] --review requires at least one file path.
    echo   Example: Run_Watch.bat --review src\jemmin\models.py
    exit /b 1
)
python bin\mini_nitpicker.py %1 %2 %3 %4 %5 %6 %7 %8
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if exist ".jemmin\logs\latest_review.txt" type ".jemmin\logs\latest_review.txt"
python bin\_setup_helper.py --count-reviews
endlocal & exit /b %EXIT_CODE%

REM ============================================================
:staged_review
REM  Run_Watch.bat --staged
REM ============================================================
call "%~dp0Set_Env.bat"
if errorlevel 1 ( echo [ERROR] Setup failed. & exit /b 1 )
call ".venv\Scripts\activate.bat"
set "PYTHONIOENCODING=utf-8"

echo [Watch] Reviewing staged files (Gemini direct)...
python bin\mini_nitpicker.py --staged
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if exist ".jemmin\logs\latest_review.txt" type ".jemmin\logs\latest_review.txt"
python bin\_setup_helper.py --count-reviews
endlocal & exit /b %EXIT_CODE%

REM ============================================================
:auto_fix
REM  Run_Watch.bat --auto-fix [files]
REM ============================================================
call "%~dp0Set_Env.bat"
if errorlevel 1 ( echo [ERROR] Setup failed. & exit /b 1 )
call ".venv\Scripts\activate.bat"
set "PYTHONIOENCODING=utf-8"
set "NITPICKER_AUTO_APPLY=1"
shift

if "%~1"=="" (
    python bin\mini_nitpicker.py --staged
) else (
    python bin\mini_nitpicker.py %1 %2 %3 %4 %5 %6 %7 %8
)
set "EXIT_CODE=%ERRORLEVEL%"
python bin\_setup_helper.py --show-review
endlocal & exit /b %EXIT_CODE%

REM ============================================================
:usage
echo.
echo Run_Watch  -  Layer 1 Gemini file watcher
echo ==========================================
echo.
echo   .\Run_Watch.bat                    Start file watcher daemon
echo   .\Run_Watch.bat --review src\f.py  Review specific files (Gemini)
echo   .\Run_Watch.bat --staged           Review staged files (Gemini)
echo   .\Run_Watch.bat --auto-fix [files] Review + auto-apply patches
echo.
echo   For 10-agent deep review: .\Run_Gemini.bat or .\Run_Local.bat
echo.
endlocal & exit /b 0
