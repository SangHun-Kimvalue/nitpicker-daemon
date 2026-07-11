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
REM  Default: start Layer 2 orchestrator with Ollama provider
REM           (Layer 1 watcher uses Gemini; L2 uses local LLM)
REM ============================================================
echo.
echo ============================================================
echo  Run_Local  -  Ollama local LLM provider
echo ============================================================
echo  Layer 2  orchestrator  -> 10-agent pipeline (port 5555)
echo                            provider: Ollama (free, local)
echo.
echo  Requires: ollama serve   (background)
echo            ollama pull qwen2.5-coder:7b
echo  Model   : qwen2.5-coder:7b  (change: OLLAMA_MODEL env var)
echo  Prompt  : config\system_prompt.md  (review persona + rules)
echo  VRAM    : 8GB minimum
echo ============================================================
echo.

REM -- Check Ollama availability --
python -c "
import sys
sys.path.insert(0, 'src')
from jemmin.providers.ollama import OllamaProvider
p = OllamaProvider()
if p.available():
    print('  Ollama : OK  (model: ' + p._model + ')')
else:
    print('  Ollama : NOT AVAILABLE')
    print('  Run: ollama serve   then rerun this script.')
    sys.exit(1)
" 2>nul
if errorlevel 1 (
    echo.
    echo  Start Ollama first:
    echo    1. ollama serve
    echo    2. ollama pull qwen2.5-coder:7b
    echo    3. rerun .\Run_Local.bat
    echo.
    pause
    endlocal & exit /b 1
)

echo.
pause

REM -- Layer 2: orchestrator with Ollama provider --
start "Nitpicker [L2] Local Orchestrator" cmd /k ^
    "chcp 65001 >nul && cd /d "%~dp0" ^
    && call .venv\Scripts\activate.bat ^
    && set PYTHONIOENCODING=utf-8 ^
    && echo [Layer 2] 10-agent orchestrator started (Ollama provider). ^
    && python bin\jemmin_daemon.py"

echo  Layer 2 started in a new window.
echo  CLI: Run_Local.bat --review --file src\foo.py
echo  CLI: Run_Local.bat --staged
echo.
endlocal & exit /b 0

REM ============================================================
:direct_review
REM  Run_Local.bat --review --file src\foo.py [--diff "..."]
REM ============================================================
call "%~dp0Set_Env.bat"
if errorlevel 1 ( echo [ERROR] Setup failed. & exit /b 1 )
call ".venv\Scripts\activate.bat"
set "PYTHONIOENCODING=utf-8"
shift

set "TARGET_FILE="
set "DIFF_TEXT="

:parse_review
if "%~1"=="" goto :run_review_local
if /I "%~1"=="--file" ( set "TARGET_FILE=%~2" & shift & shift & goto :parse_review )
if /I "%~1"=="--diff" ( set "DIFF_TEXT=%~2"   & shift & shift & goto :parse_review )
shift & goto :parse_review

:run_review_local
if "%TARGET_FILE%"=="" (
    echo [ERROR] --file required.
    echo   Example: Run_Local.bat --review --file src\foo.py
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
python bin\jemmin_cli.py --no-daemon --file "%TARGET_FILE%" --diff "%DIFF_TEXT%" --provider ollama
set "EXIT_CODE=%ERRORLEVEL%"
python bin\_setup_helper.py --show-review
endlocal & exit /b %EXIT_CODE%

REM ============================================================
:staged_review
REM  Run_Local.bat --staged
REM ============================================================
call "%~dp0Set_Env.bat"
if errorlevel 1 ( echo [ERROR] Setup failed. & exit /b 1 )
call ".venv\Scripts\activate.bat"
set "PYTHONIOENCODING=utf-8"

echo [Local] Reviewing staged files (10-agent, Ollama)...
for /f "delims=" %%F in ('git diff --cached --name-only --diff-filter=ACM 2^>nul') do (
    echo   -> %%F
    for /f "delims=" %%D in ('git diff --cached -- "%%F" 2^>nul') do (
        del ".jemmin\spool.db" >nul 2>nul
        python bin\jemmin_cli.py --no-daemon --file "%%F" --diff "%%D" --provider ollama 2>nul
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
echo Run_Local  -  Ollama local LLM provider (free)
echo ================================================
echo.
echo   .\Run_Local.bat                       Start L2 orchestrator (Ollama)
echo   .\Run_Local.bat --review --file f     Direct 10-agent review (Ollama)
echo   .\Run_Local.bat --staged              Review all staged files
echo   .\Run_Local.bat --stats               DuckDB analytics
echo.
echo   Requires: ollama serve
echo             ollama pull qwen2.5-coder:7b
echo   Prompt  : config\system_prompt.md  (edit to change review rules)
echo.
endlocal & exit /b 0
