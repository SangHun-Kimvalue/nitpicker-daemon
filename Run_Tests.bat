@echo off
chcp 65001 >nul
setlocal

cd /d "%~dp0"

call "%~dp0Set_Env.bat"
if errorlevel 1 ( echo [ERROR] Setup failed. & exit /b 1 )
call ".venv\Scripts\activate.bat"
set "PYTHONIOENCODING=utf-8"

if /I "%~1"=="-v"        goto :verbose
if /I "%~1"=="--verbose" goto :verbose
if /I "%~1"=="-k"        goto :filter
if /I "%~1"=="--phase"   goto :phase

echo [Tests] Running all 511+ unit tests...
echo.
python -m pytest tests -q --tb=short
set "EXIT_CODE=%ERRORLEVEL%"

if %EXIT_CODE%==0 ( echo. & echo  ALL TESTS PASSED ) else ( echo. & echo  SOME TESTS FAILED )
endlocal & exit /b %EXIT_CODE%

:verbose
echo [Tests] Running verbose...
python -m pytest tests -v --tb=short
endlocal & exit /b %ERRORLEVEL%

:filter
echo [Tests] Filter: %2
python -m pytest tests -k "%~2" -v --tb=short
endlocal & exit /b %ERRORLEVEL%

:phase
echo [Tests] Phase: %2
python -m pytest tests\test_phase_%2.py -v --tb=short
endlocal & exit /b %ERRORLEVEL%
