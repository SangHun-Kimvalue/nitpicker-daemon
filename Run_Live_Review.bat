@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

cd /d "%~dp0"

if /I "%~1"=="/h" goto :usage
if /I "%~1"=="/?" goto :usage
if /I "%~1"=="-h" goto :usage
if /I "%~1"=="--help" goto :usage

call "%~dp0Set_Env.bat"
if errorlevel 1 (
    echo [Nitpicker][ERROR] Environment setup failed.
    exit /b 1
)

call ".venv\Scripts\activate.bat"
if errorlevel 1 (
    echo [Nitpicker][ERROR] Failed to activate virtual environment.
    exit /b 1
)

set "PYTHONIOENCODING=utf-8"

echo ============================================================
echo  Nitpicker Live Review  (Gemini API Real-Time)
echo ============================================================
echo.

REM --- Mode select ---
if /I "%~1"=="--auto-fix"  goto :auto_fix_review
if /I "%~1"=="--watch"     goto :watch_mode
if /I "%~1"=="--benchmark" goto :cache_benchmark
if /I "%~1"=="--layer2"    goto :layer2_review

REM --- Default: staged or specific files ---
if "%~1"=="" (
    echo [Live] Running Gemini review for staged changes...
    python bin\mini_nitpicker.py --staged
) else (
    echo [Live] Running Gemini review for targets: %*
    python bin\mini_nitpicker.py %*
)
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo ============================================================
echo  Review Results
echo ============================================================

if exist ".jemmin\logs\latest_review.txt" (
    echo.
    echo --- latest_review.txt ---
    type ".jemmin\logs\latest_review.txt"
)

if exist ".jemmin\logs\latest_review.json" (
    echo.
    echo --- latest_review.json ---
    python bin\_setup_helper.py --show-review 2>nul
)

echo.
echo --- Review Log Stats ---
python bin\_setup_helper.py --count-reviews 2>nul

endlocal & exit /b %EXIT_CODE%

REM ============================================================
:layer2_review
REM --- Layer 2 direct review (10-agent) ---
echo [Layer 2] Running 10-agent ReviewOrchestrator (direct mode)...
shift
if "%~1"=="" (
    echo [ERROR] --layer2 requires --file and --diff arguments.
    echo   Example: Run_Live_Review.bat --layer2 --file src\main.py --diff "some diff"
    echo   Example: Run_Live_Review.bat --layer2 --file src\main.py --diff "..." --provider ollama
    exit /b 1
)
python bin\jemmin_cli.py --no-daemon %1 %2 %3 %4 %5 %6 %7 %8 %9
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo --- Layer 2 Analytics ---
python bin\jemmin_cli.py --stats 2>nul

endlocal & exit /b %EXIT_CODE%

REM ============================================================
:auto_fix_review
echo [Auto-Fix Live] Running Gemini review with auto-apply enabled...
echo [Auto-Fix Live] Patches will be applied via git apply automatically.
echo.
set "NITPICKER_AUTO_APPLY=1"
shift
if "%~1"=="" (
    python bin\mini_nitpicker.py --staged
) else (
    python bin\mini_nitpicker.py %1 %2 %3 %4 %5 %6 %7 %8 %9
)
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo --- Auto-Fix Results ---
python bin\_setup_helper.py --show-review 2>nul

if %EXIT_CODE%==0 (
    echo.
    echo [Auto-Fix] Check applied changes:
    echo   git diff
    echo   git diff --cached
)

endlocal & exit /b %EXIT_CODE%

REM ============================================================
:watch_mode
echo [Watch] Starting daemon + real-time log tail...
echo [Watch] Press Ctrl+C to stop.
echo.

REM Start daemon in background window
start "Nitpicker Watcher" cmd /k "cd /d "%~dp0" && call .venv\Scripts\activate.bat && set PYTHONIOENCODING=utf-8 && python bin\mini_nitpicker_daemon.py"

echo [Watch] Daemon started in background window.
echo [Watch] Tailing latest_review.txt (refresh every 2s)...
echo [Watch] Edit a .py file in src/ to trigger a review.
echo.

:watch_loop
if exist ".jemmin\logs\latest_review.txt" (
    cls
    echo ============================================================
    echo  Nitpicker Live Watch  [%DATE% %TIME%]
    echo ============================================================
    echo.
    type ".jemmin\logs\latest_review.txt"
    echo.
    echo ---
    echo [Watch] Waiting for next review... (Ctrl+C to stop)
)
timeout /t 2 /nobreak >nul
goto :watch_loop

REM ============================================================
:cache_benchmark
echo ============================================================
echo  Context Cache Benchmark
echo ============================================================
echo.
echo [Benchmark] Runs the same review twice to measure cache hit effect.
echo.

shift
set "TARGET=%~1"
if "%TARGET%"=="" set "TARGET=src\jemmin\mini_reviewer.py"

echo [Benchmark] Target: %TARGET%
echo.

REM Run 1: Cache Miss (cold)
echo --- Run 1: Cache MISS (cold start) ---
set "START_1=%TIME%"
python bin\mini_nitpicker.py "%TARGET%" 2>nul
set "END_1=%TIME%"
echo [Time] Start: %START_1%  End: %END_1%
echo.

REM Run 2: Cache Hit (warm)
echo --- Run 2: Cache HIT (warm, same file) ---
set "START_2=%TIME%"
python bin\mini_nitpicker.py "%TARGET%" 2>nul
set "END_2=%TIME%"
echo [Time] Start: %START_2%  End: %END_2%
echo.

echo ============================================================
echo  Context Cache is in-process memory (StaticContextService).
echo  Speed gain is most visible in daemon mode (long-running process).
echo  For in-process benchmark, run: Run_CLI.bat --test-all
echo ============================================================

endlocal & exit /b 0

REM ============================================================
:usage
echo.
echo Nitpicker Live Review - Real-Time Review Runner
echo ================================================
echo.
echo Usage:
echo   Run_Live_Review.bat                           Review staged changes + show results
echo   Run_Live_Review.bat ^<file1^> [...]           Review specific files + show results
echo   Run_Live_Review.bat --auto-fix [...]          Review + auto-apply patches (git apply)
echo   Run_Live_Review.bat --watch                   Start daemon + tail review output
echo   Run_Live_Review.bat --benchmark [file]        Cache hit speed test (run twice)
echo   Run_Live_Review.bat --layer2 --file ^<f^> --diff ^<d^>   Layer 2 10-agent review
echo   Run_Live_Review.bat --layer2 ... --provider ollama      Layer 2 with Ollama LLM
echo.
echo Config:
echo   config\system_prompt.md   Review persona + rules (editable)
echo.
echo Output:
echo   latest_review.txt   Human-readable review summary
echo   latest_review.json  Machine-readable (LSP / automation)
echo   mini_reviews.jsonl  Append-only audit log
echo.
echo Examples:
echo   Run_Live_Review.bat src\jemmin\mini_reviewer.py
echo   Run_Live_Review.bat --auto-fix src\jemmin\services\feedback_svc.py
echo   Run_Live_Review.bat --watch
echo   Run_Live_Review.bat --benchmark src\jemmin\mini_reviewer.py
echo   Run_Live_Review.bat --layer2 --file src\main.py --diff "..." --provider ollama
echo.
endlocal & exit /b 0
