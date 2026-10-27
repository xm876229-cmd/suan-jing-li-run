@echo off
chcp 65001 >nul
cd /d "%~dp0.."

rem 一键运行：默认算「本月 1 号 ~ 今天」
rem   run.bat                      本月至今
rem   run.bat --mode yesterday     昨天
rem   run.bat --mode last-month    上月
rem   run.bat --mode yesterday --store demo-my   指定店铺
rem   run.bat scheduled            任务计划程序用（昨天）

if /i "%~1"=="scheduled" (
    python -m scripts.run_default --mode yesterday
) else (
    python -m scripts.run_default %*
)

echo.
echo 生成完毕，报表在 output\ 目录下。
pause
