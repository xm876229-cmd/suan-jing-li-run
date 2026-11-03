@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 净利助手

rem ── 唯一入口 ──────────────────────────────────────────────
rem 双击这个文件 = 启动 release\净利助手\jingli.exe（打包好的正式版）。
rem
rem 以前这里是 `python -m src.app`，也就是**源码**模式：
rem 同一个项目里就有两个"程序"——源码跑的和 exe 跑的。数据是同一份
rem （都靠 数据目录.txt 指到项目根），但代码版本可能不一样：
rem 改了源码没重新打包，两个入口的行为就会不同，排查问题时会互相误导。
rem
rem 现在统一成 exe：双击这里和双击 exe 完完全全是同一个程序。
rem 要跑源码调试，用「开发-源码启动.bat」。
rem ─────────────────────────────────────────────────────────

set "EXE=%~dp0release\净利助手\jingli.exe"
if not exist "%EXE%" (
  echo.
  echo 找不到 "%EXE%"
  echo.
  echo 可能还没打包。开发时可以改用「开发-源码启动.bat」，
  echo 或者运行：python -m scripts.sync_all --build
  echo.
  pause
  exit /b 1
)

echo 正在启动净利助手（release 版）...
start "" "%EXE%"
exit /b 0
