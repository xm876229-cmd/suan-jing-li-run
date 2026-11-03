@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 净利助手 · 源码模式（开发用）

rem 开发/排查问题专用：直接跑 Python 源码，改完代码不用打包就能看到效果。
rem
rem 注意：这里的代码是**工作区最新**的，而 release\净利助手\jingli.exe 是
rem 上一次打包时的版本。两者共用同一份数据（数据目录.txt 指向项目根），
rem 但版本可能不一致——非开发场景请用「启动净利助手.bat」。
rem
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

echo 正在以源码模式启动净利助手（开发用）...
echo 窗口会自己打开，取数服务在后台一起起来。
echo.
"%PYEXE%" -m src.app

echo.
echo 程序已退出。按任意键关闭窗口。
pause >nul
