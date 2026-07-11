@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================================
echo  Nitpicker Daemon - Environment Setup
echo ============================================================
echo  Project root: %CD%
echo.

REM ============================================================
REM  [1] Python 3.11+
REM ============================================================
set "PYTHON_CMD="

where python >nul 2>nul
if %ERRORLEVEL%==0 (
    python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
    if %ERRORLEVEL%==0 set "PYTHON_CMD=python"
)

if not defined PYTHON_CMD (
    where py >nul 2>nul
    if %ERRORLEVEL%==0 (
        py -3.13 -V >nul 2>nul
        if %ERRORLEVEL%==0 ( set "PYTHON_CMD=py -3.13" ) else (
        py -3.12 -V >nul 2>nul
        if %ERRORLEVEL%==0 ( set "PYTHON_CMD=py -3.12" ) else (
        py -3.11 -V >nul 2>nul
        if %ERRORLEVEL%==0 ( set "PYTHON_CMD=py -3.11" ) else (
        py -3 -V >nul 2>nul
        if %ERRORLEVEL%==0   set "PYTHON_CMD=py -3"
        )))
    )
)

if not defined PYTHON_CMD (
    echo [ERROR] Python 3.11+ not found.
    echo         https://www.python.org/downloads/
    pause
    exit /b 1
)
echo [1/6] Python      : OK  (%PYTHON_CMD%)

REM ============================================================
REM  [2] Virtual environment
REM ============================================================
if not exist ".venv\Scripts\python.exe" (
    echo [2/6] Creating .venv...
    call %PYTHON_CMD% -m venv .venv
    if errorlevel 1 ( echo [ERROR] venv creation failed. & pause & exit /b 1 )
) else (
    echo [2/6] .venv        : OK
)

call ".venv\Scripts\activate.bat"
if errorlevel 1 ( echo [ERROR] venv activate failed. & pause & exit /b 1 )

REM ============================================================
REM  [3] Packages
REM ============================================================
echo [3/6] Packages    : installing / verifying...

python -m pip install --upgrade pip setuptools wheel -q
if errorlevel 1 ( echo [ERROR] pip upgrade failed. & pause & exit /b 1 )

python -m pip install -e "." -q
if errorlevel 1 ( echo [ERROR] pip install -e failed. & pause & exit /b 1 )

python -c "import google.genai" >nul 2>nul
if errorlevel 1 ( python -m pip install google-genai -q )

python -c "import duckdb" >nul 2>nul
if errorlevel 1 ( python -m pip install duckdb -q )

python -c "import zmq" >nul 2>nul
if errorlevel 1 ( python -m pip install pyzmq -q )

python -c "import watchdog" >nul 2>nul
if errorlevel 1 ( python -m pip install watchdog -q )

python -c "import ruff" >nul 2>nul
if errorlevel 1 ( python -m pip install ruff -q )

python -c "import pytest" >nul 2>nul
if errorlevel 1 ( python -m pip install pytest -q )

python -c "import mypy" >nul 2>nul
if errorlevel 1 ( python -m pip install mypy -q )

echo         done.

REM ============================================================
REM  [4] Runtime directories & Config scaffolding
REM ============================================================
if not exist ".jemmin"         mkdir ".jemmin"
if not exist ".jemmin\logs"    mkdir ".jemmin\logs"
if not exist ".jemmin\patches" mkdir ".jemmin\patches"
if not exist "config"          mkdir "config"

if not exist "config\reviewer_config.yaml" (
    echo provider:> "config\reviewer_config.yaml"
    echo   default: mock>> "config\reviewer_config.yaml"
)

if not exist "config\nitpicker.local.json" (
    echo { "gemini_api_key": "YOUR_GEMINI_API_KEY_HERE", "gemini_model": "gemini-3.1-pro-preview", "gemini_fallback_model": "gemini-2.0-flash", "auto_apply_patches": false } > "config\nitpicker.local.json"
)

echo [4/6] Runtime dir : OK  (.jemmin\, config\)

REM ============================================================
REM  [5] .gitignore safety net
REM ============================================================
echo [5/6] Gitignore   : checking...

if not exist ".gitignore" (
    echo .venv/>                          ".gitignore"
    echo .jemmin/>>                       ".gitignore"
    echo config/nitpicker.local.json>>    ".gitignore"
    echo __pycache__/>>                   ".gitignore"
    echo *.pyc>>                          ".gitignore"
) else (
    findstr /I "\.jemmin" ".gitignore" >nul 2>nul
    if errorlevel 1 ( echo .jemmin/>> ".gitignore" )

    findstr /I "nitpicker.local.json" ".gitignore" >nul 2>nul
    if errorlevel 1 ( echo config/nitpicker.local.json>> ".gitignore" )
)
echo         done.

REM ============================================================
REM  [6] Setup Wizard  (provider / watch folder / review mode)
REM ============================================================
echo [6/6] Setup Wizard...
echo.
set "PYTHONIOENCODING=utf-8"
python bin\setup_wizard.py
set "WIZARD_EXIT=%ERRORLEVEL%"

REM  Wizard exit codes:
REM    0 = Gemini chosen  -> skip Ollama, go to done
REM    2 = Ollama chosen  -> run Ollama install + model pull

if "!WIZARD_EXIT!"=="2" goto :ollama_setup
goto :setup_done

REM ============================================================
REM  Ollama install + model pull  (only when wizard chose Ollama)
REM ============================================================
:ollama_setup
echo.
echo ============================================================
echo  Ollama Setup
echo ============================================================
set "OLLAMA_FOUND=0"
set "OLLAMA_READY=0"
set "OLLAMA_MODEL_NAME=qwen2.5-coder:7b"
set "OLLAMA_EXE=ollama"

where ollama >nul 2>nul
if %ERRORLEVEL%==0 (
    set "OLLAMA_FOUND=1"
) else (
    if exist "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" (
        set "OLLAMA_FOUND=1"
        set "OLLAMA_EXE=%LOCALAPPDATA%\Programs\Ollama\ollama.exe"
    )
)

if "!OLLAMA_FOUND!"=="0" (
    echo  Ollama binary not found.
    echo.
    set /p "INSTALL_ANSWER=  Download and install Ollama now? (~200MB) [Y/N]: "
    if /I "!INSTALL_ANSWER!"=="Y" (
        echo.
        echo  [Ollama 1/3] Downloading OllamaSetup.exe...
        curl -L -o OllamaSetup.exe "https://ollama.com/download/OllamaSetup.exe"
        if exist OllamaSetup.exe (
            echo  [Ollama 2/3] Running installer - follow the prompts...
            start /wait OllamaSetup.exe
            del OllamaSetup.exe >nul 2>nul
            if exist "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" (
                set "OLLAMA_FOUND=1"
                set "OLLAMA_EXE=%LOCALAPPDATA%\Programs\Ollama\ollama.exe"
                echo  [Ollama 2/3] Installation complete.
                echo  Waiting for Ollama service to initialize...
                timeout /t 6 /nobreak >nul
            ) else (
                echo [ERROR] Installation failed or was cancelled.
                echo         Run .\Run_Local.bat after installing Ollama manually.
                goto :ollama_skip
            )
        ) else (
            echo [ERROR] Download failed. Check internet connection.
            goto :ollama_skip
        )
    ) else (
        echo  Skipped. Run Set_Env.bat again to install Ollama later.
        echo  Download page: ollama.com/download
        goto :ollama_skip
    )
)

REM -- Model pull --
echo  Binary: !OLLAMA_EXE!
!OLLAMA_EXE! list 2>nul | findstr /I "qwen2.5-coder" >nul 2>nul
if %ERRORLEVEL%==0 (
    echo  Model [!OLLAMA_MODEL_NAME!] : already pulled.
    set "OLLAMA_READY=1"
    goto :ollama_done
)

echo  Model [!OLLAMA_MODEL_NAME!] not found.
echo.
set /p "PULL_ANSWER=  Pull !OLLAMA_MODEL_NAME! now? (~5GB, needs VRAM 8GB+) [Y/N]: "
if /I "!PULL_ANSWER!"=="Y" (
    echo  [Ollama 3/3] Pulling !OLLAMA_MODEL_NAME!...
    !OLLAMA_EXE! pull !OLLAMA_MODEL_NAME!
    if errorlevel 1 (
        echo [WARNING] Pull failed. Run manually: ollama pull !OLLAMA_MODEL_NAME!
    ) else (
        echo  [Ollama 3/3] Model ready.
        set "OLLAMA_READY=1"
    )
) else (
    echo  Skipped. Run later: ollama pull !OLLAMA_MODEL_NAME!
)

:ollama_skip
:ollama_done

REM ============================================================
REM  Done
REM ============================================================
:setup_done
echo.
echo ============================================================
echo  Setup Complete
echo ============================================================
echo  Python  : %CD%\.venv\Scripts\python.exe
echo  Runtime : %CD%\.jemmin\
echo.
python bin\_setup_helper.py --show-provider
echo.
echo ============================================================
echo  Config Files
echo ============================================================
echo   config\nitpicker.local.json   API key, model, watch path
echo   config\system_prompt.md       Review persona + rules (edit!)
echo   config\reviewer_config.yaml   Provider selection
echo.
echo ============================================================
echo  Quick Start  (PowerShell: use .\ prefix)
echo ============================================================
echo   .\Run_Gemini.bat    Gemini API  (cloud, API key required)
echo   .\Run_Local.bat     Ollama LLM  (local, free, GPU required)
echo   .\Run_Watch.bat     File watcher only (no LLM)
echo   .\Run_Tests.bat     Run unit tests
echo ============================================================
echo.
pause

endlocal
exit /b 0
