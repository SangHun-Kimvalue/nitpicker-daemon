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

if /I "%~1"=="--test-all" goto :test_all
if /I "%~1"=="--layer2"   goto :layer2
if /I "%~1"=="--stats"    goto :stats
if /I "%~1"=="--auto-fix" goto :auto_fix

REM --- Default: Layer 1 Mini Nitpicker ---
if "%~1"=="" (
    echo [L1] Reviewing staged changes (Gemini)...
    python bin\mini_nitpicker.py --staged
) else (
    echo [L1] Reviewing: %*
    python bin\mini_nitpicker.py %*
)
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if exist ".jemmin\logs\latest_review.txt" (
    echo --- Result ---
    type ".jemmin\logs\latest_review.txt"
)
endlocal & exit /b %EXIT_CODE%

REM ============================================================
:layer2
echo [L2] 10-agent ReviewOrchestrator (direct)...
shift
if "%~1"=="" (
    echo [ERROR] --layer2 requires --file and --diff.
    exit /b 1
)
del ".jemmin\spool.db" >nul 2>nul
python bin\jemmin_cli.py --no-daemon %1 %2 %3 %4 %5 %6 %7 %8 %9
endlocal & exit /b %ERRORLEVEL%

REM ============================================================
:stats
python bin\jemmin_cli.py --stats
endlocal & exit /b %ERRORLEVEL%

REM ============================================================
:auto_fix
set "NITPICKER_AUTO_APPLY=1"
shift
if "%~1"=="" (
    python bin\mini_nitpicker.py --staged
) else (
    python bin\mini_nitpicker.py %1 %2 %3 %4 %5 %6 %7 %8 %9
)
python bin\_setup_helper.py --show-review
endlocal & exit /b %ERRORLEVEL%

REM ============================================================
:test_all
echo.
echo ============================================================
echo  Integrated Test Suite  (Phase I ~ VII)
echo ============================================================
echo.
set "PASS=0"
set "FAIL=0"

REM 1. pytest
echo [1/7] pytest 511+ tests...
python -m pytest tests -q --tb=short 2>nul
if %ERRORLEVEL%==0 ( echo   PASS & set /a PASS+=1 ) else ( echo   FAIL & set /a FAIL+=1 )

REM 2. Layer 1 skip mode
echo [2/7] Layer 1 skip mode...
set "NITPICKER_SKIP=1"
python bin\mini_nitpicker.py src\jemmin\mini_reviewer.py 2>nul
if %ERRORLEVEL%==0 ( echo   PASS & set /a PASS+=1 ) else ( echo   FAIL & set /a FAIL+=1 )
set "NITPICKER_SKIP="

REM 3. Layer 2 direct
echo [3/7] Layer 2 orchestrator...
del ".jemmin\spool.db" >nul 2>nul
python bin\jemmin_cli.py --no-daemon --file src\jemmin\mini_reviewer.py --diff "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new" 2>nul
if %ERRORLEVEL%==0 ( echo   PASS & set /a PASS+=1 ) else ( echo   FAIL & set /a FAIL+=1 )

REM 4. DuckDB stats
echo [4/7] DuckDB analytics...
python bin\jemmin_cli.py --stats 2>nul
if %ERRORLEVEL%==0 ( echo   PASS & set /a PASS+=1 ) else ( echo   FAIL & set /a FAIL+=1 )

REM 5. AST Security smoke
echo [5/7] AST Security Analyzer...
python -c "import sys; sys.path.insert(0,'src'); from jemmin.analyzers.ast_security import AstSecurityAnalyzer; from jemmin.models import ReviewRequest, ContextBundle; a=AstSecurityAnalyzer(); req=ReviewRequest(request_id='t',idempotency_key='ik',project_id='p',project_profile='g',trigger='cli',trigger_intent='active_intent',target_file='x.py',git_revision='HEAD',base_file_hash='h',diff_text='+import subprocess as sp\n+sp.Popen(\"ls\",shell=True)',metadata={}); ctx=ContextBundle(context_hash='h',token_estimate=10,tiers={},metadata={}); d=a.run(req,ctx); assert d.status=='reject'" 2>nul
if %ERRORLEVEL%==0 ( echo   PASS & set /a PASS+=1 ) else ( echo   FAIL & set /a FAIL+=1 )

REM 6. Ollama fallback
echo [6/7] Ollama graceful fallback...
python -c "import sys; sys.path.insert(0,'src'); from jemmin.providers.ollama import OllamaProvider; p=OllamaProvider(); p.available()" 2>nul
if %ERRORLEVEL%==0 ( echo   PASS & set /a PASS+=1 ) else ( echo   FAIL & set /a FAIL+=1 )

REM 7. SQLite schema
echo [7/7] SQLite trigger_intent column...
del ".jemmin\spool.db" >nul 2>nul
python -c "import sys,sqlite3; sys.path.insert(0,'src'); from jemmin.state.sqlite_spooler import SQLiteJobStore; from pathlib import Path; SQLiteJobStore(Path('.jemmin/spool.db')); conn=sqlite3.connect('.jemmin/spool.db'); s=conn.execute(\"SELECT sql FROM sqlite_master WHERE name='review_requests'\").fetchone()[0]; conn.close(); assert 'trigger_intent' in s" 2>nul
if %ERRORLEVEL%==0 ( echo   PASS & set /a PASS+=1 ) else ( echo   FAIL & set /a FAIL+=1 )

echo.
echo ============================================================
echo  Results: !PASS!/7 passed,  !FAIL!/7 failed
echo ============================================================
if !FAIL! GTR 0 ( endlocal & exit /b 1 ) else ( endlocal & exit /b 0 )

REM ============================================================
:usage
echo.
echo Run_CLI  -  quick CLI access
echo.
echo   .\Run_CLI.bat                      L1 review staged files
echo   .\Run_CLI.bat src\foo.py           L1 review specific file
echo   .\Run_CLI.bat --auto-fix           L1 review + auto-apply patch
echo   .\Run_CLI.bat --layer2 --file f --diff "..." [--provider ollama]
echo   .\Run_CLI.bat --stats              DuckDB analytics
echo   .\Run_CLI.bat --test-all           Phase I~VII integration tests
echo.
endlocal & exit /b 0
