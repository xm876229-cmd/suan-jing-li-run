@echo off
rem Resolve a usable Python interpreter into PYEXE.
rem NOTE: WindowsApps\python.exe is a Store stub that does nothing - must be skipped.
setlocal enabledelayedexpansion
set "PYEXE="

if defined PYTHON_EXE if exist "%PYTHON_EXE%" set "PYEXE=%PYTHON_EXE%"
if defined PYEXE goto :verify

for /f "delims=" %%i in ('where python 2^>nul') do (
    if not defined PYEXE (
        echo %%i | findstr /i "WindowsApps" >nul || set "PYEXE=%%i"
    )
)
if defined PYEXE goto :verify

where py >nul 2>nul
if not errorlevel 1 (
    for /f "delims=" %%i in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PYEXE=%%i"
)
if defined PYEXE goto :verify

for %%d in (
    "%LOCALAPPDATA%\Programs\Python\Python313"
    "%LOCALAPPDATA%\Programs\Python\Python312"
    "%LOCALAPPDATA%\Programs\Python\Python311"
    "C:/Python313"
    "C:/Python312"
    "C:/Python311"
    "%ProgramFiles%\Python313"
    "%ProgramFiles%\Python312"
) do (
    if not defined PYEXE if exist "%%~d\python.exe" set "PYEXE=%%~d\python.exe"
)
if defined PYEXE goto :verify

for /d %%d in ("%USERPROFILE%\.workbuddy\binaries\python\versions\*") do (
    if exist "%%~d\python.exe" set "PYEXE=%%~d\python.exe"
)

:verify
if not defined PYEXE goto :fail
"%PYEXE%" -c "import sys" >nul 2>nul
if errorlevel 1 goto :fail

endlocal & set "PYEXE=%PYEXE%" & exit /b 0

:fail
endlocal
echo.
echo [ERROR] No usable Python interpreter found.
echo.
echo Fix: install Python 3.10+ from python.org (check "Add to PATH"),
echo      or set env var PYTHON_EXE to the full path of python.exe.
echo      See README.md for details.
echo.
pause
exit /b 1
