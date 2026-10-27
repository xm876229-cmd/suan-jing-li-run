@echo off
cd /d "%~dp0.."

call "%~dp0_python.bat"
if errorlevel 1 exit /b 1

"%PYEXE%" -m bridge.server
echo.
echo Bridge stopped.
pause
