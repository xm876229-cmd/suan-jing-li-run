@echo off
cd /d "%~dp0.."

rem run.bat                  -> current month to today
rem run.bat --mode yesterday -> yesterday
rem run.bat --mode last-month-> last month
rem run.bat --store demo-my  -> specific store
rem run.bat scheduled        -> used by Task Scheduler (yesterday)

call "%~dp0_python.bat"
if errorlevel 1 exit /b 1

if /i "%~1"=="scheduled" (
    "%PYEXE%" -m scripts.run_default --mode yesterday
) else (
    "%PYEXE%" -m scripts.run_default %*
)

echo.
echo Done. Report is in the output folder.
pause
