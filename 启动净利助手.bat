@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 净利助手

rem 新手只需要双击这个文件。
rem 它会自动找一个能用的 Python，然后启动程序界面。
rem 找不到 Python 时会提示去 python.org 装一个 3.10 以上版本。

call "%~dp0scripts\_python.bat"
if errorlevel 1 (
  echo.
  echo 没有找到可用的 Python。请先到 https://www.python.org 下载安装 3.10 以上版本，
  echo 安装时记得勾选 "Add Python to PATH"，然后再双击本文件。
  echo.
  pause
  exit /b 1
)

echo 正在启动净利助手，第一次启动可能要等几秒...
echo 启动后浏览器会自动打开程序界面；如果没开，手动访问 http://127.0.0.1:8765
echo.
"%PYEXE%" -m src.app serve

echo.
echo 程序已退出。按任意键关闭窗口。
pause >nul
