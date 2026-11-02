@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 净利助手

rem 新手只需要双击这个文件。
rem 它会自动找一个能用的 Python，然后启动程序（窗口 + 取数服务一起）。
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
echo 窗口会自己打开，取数服务在后台一起起来，不用再点别的。
echo.
"%PYEXE%" -m src.app

echo.
echo 程序已退出。按任意键关闭窗口。
pause >nul
