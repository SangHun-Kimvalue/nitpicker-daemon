@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

cd /d "%~dp0"

if /I "%~1"=="/h"     goto :usage
if /I "%~1"=="/?"     goto :usage
if /I "%~1"=="-h"     goto :usage
if /I "%~1"=="--help" goto :usage
if /I "%~1"=="--review"  goto :direct_review
if /I "%~1"=="--staged"  goto :staged_review
if /I "%~1"=="--stats"   goto :show_stats

call "%~dp0Set_Env.bat"
if errorlevel 1 ( echo [ERROR] Setup failed. & exit /b 1 )
call ".venv\Scripts\activate.bat"
set "PYTHONIOENCODING=utf-8"

REM ============================================================
REM  Default: start both Layer 1 watcher + Layer 2 orchestrator
REM           both using Gemini provider
REM ============================================================
echo.
echo ============================================================
echo  Run_Gemini  -  Gemini API provider
echo ============================================================
echo  Layer 1  file watcher  -> Gemini direct review on save
echo  Layer 2  orchestrator  -> 10-agent pipeline (port 5555)
echo.
echo  API key : config\nitpicker.local.json  (gemini_api_key)
echo            or env GEMINI_API_KEY
echo  Model   : gemini-2.0-flash  (change: GEMINI_MODEL or JEMMIN_MODEL)
echo  Prompt  : config\system_prompt.md  (review persona + rules)
echo ============================================================
echo.
python bin\_setup_helper.py --show-provider
echo.
pause

REM -- Layer 1: Gemini file watcher --
start "Nitpicker [L1] Gemini Watcher" cmd /k ^
    "chcp 65001 >nul && cd /d "%~dp0" ^
    && call .venv\Scripts\activate.bat ^
    && set PYTHONIOENCODING=utf-8 ^
    && echo [Layer 1] Gemini file watcher started. ^
    && python bin\mini_nitpicker_daemon.py"

REM -- Layer 2: orchestrator with Gemini provider --
start "Nitpicker [L2] Gemini Orchestrator" cmd /k ^
    "chcp 65001 >nul && cd /d "%~dp0" ^
    && call .venv\Scripts\activate.bat ^
    && set PYTHONIOENCODING=utf-8 ^
    && set JEMMIN_MODEL=gemini-2.0-flash ^
    && echo [Layer 2] 10-agent orchestrator started (Gemini provider). ^
    && python bin\jemmin_daemon.py"

echo  Both services started in separate windows.
echo  Layer 1 : reviews files on save -> .jemmin\logs\latest_review.txt
echo  Layer 2 : CLI requests -> Run_Gemini.bat --review --file src\foo.py
echo.
endlocal & exit /b 0

REM ============================================================
:direct_review
REM  Run_Gemini.bat --review --file src\foo.py [--diff "..."]
REM ============================================================
call "%~dp0Set_Env.bat"
if errorlevel 1 ( echo [ERROR] Setup failed. & exit /b 1 )
call ".venv\Scripts\activate.bat"
set "PYTHONIOENCODING=utf-8"
shift

set "TARGET_FILE="
set "DIFF_TEXT="

:parse_review
if "%~1"=="" goto :run_review_gemini
if /I "%~1"=="--file" ( set "TARGET_FILE=%~2" & shift & shift & goto :parse_review )
if /I "%~1"=="--diff" ( set "DIFF_TEXT=%~2"   & shift & shift & goto :parse_review )
shift & goto :parse_review

:run_review_gemini
if "%TARGET_FILE%"=="" (
    echo [ERROR] --file required.
    echo   Example: Run_Gemini.bat --review --file src\foo.py
    exit /b 1
)
if "%DIFF_TEXT%"=="" (
    for /f "delims=" %%D in ('git diff HEAD -- "%TARGET_FILE%" 2^>nul') do set "DIFF_TEXT=%%D"
)
if "%DIFF_TEXT%"=="" (
    for /f "delims=" %%D in ('git diff --cached -- "%TARGET_FILE%" 2^>nul') do set "DIFF_TEXT=%%D"
)
if "%DIFF_TEXT%"=="" set "DIFF_TEXT=--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new"

del ".jemmin\spool.db" >nul 2>nul
python bin\jemmin_cli.py --no-daemon --file "%TARGET_FILE%" --diff "%DIFF_TEXT%" --provider gemini
set "EXIT_CODE=%ERRORLEVEL%"
python bin\_setup_helper.py --show-review
endlocal & exit /b %EXIT_CODE%

REM ============================================================
:staged_review
REM  Run_Gemini.bat --staged
REM ============================================================
call "%~dp0Set_Env.bat"
if errorlevel 1 ( echo [ERROR] Setup failed. & exit /b 1 )
call ".venv\Scripts\activate.bat"
set "PYTHONIOENCODING=utf-8"

echo [Gemini] Reviewing staged files (10-agent)...
for /f "delims=" %%F in ('git diff --cached --name-only --diff-filter=ACM 2^>nul') do (
    echo   -> %%F
    for /f "delims=" %%D in ('git diff --cached -- "%%F" 2^>nul') do (
        del ".jemmin\spool.db" >nul 2>nul
        python bin\jemmin_cli.py --no-daemon --file "%%F" --diff "%%D" --provider gemini 2>nul
    )
)
python bin\jemmin_cli.py --stats
endlocal & exit /b 0

REM ============================================================
:show_stats
call "%~dp0Set_Env.bat"
if errorlevel 1 ( echo [ERROR] Setup failed. & exit /b 1 )
call ".venv\Scripts\activate.bat"
set "PYTHONIOENCODING=utf-8"
python bin\jemmin_cli.py --stats
endlocal & exit /b %ERRORLEVEL%

REM ============================================================
:usage
echo.
echo Run_Gemini  -  Gemini API provider (cloud)
echo ==========================================
echo.
echo   .\Run_Gemini.bat                      Start L1 watcher + L2 orchestrator
echo   .\Run_Gemini.bat --review --file f    Direct 10-agent review (Gemini)
echo   .\Run_Gemini.bat --staged             Review all staged files
echo   .\Run_Gemini.bat --stats              DuckDB analytics
echo.
echo   Requires: config\nitpicker.local.json  (gemini_api_key)
echo             or set GEMINI_API_KEY=...
echo   Prompt  : config\system_prompt.md  (edit to change review rules)
echo.
endlocal & exit /b 0
