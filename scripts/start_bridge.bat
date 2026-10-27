@echo off
chcp 65001 >nul
cd /d "%~dp0.."

echo ============================================
echo  净利取数助手 - 本地桥
echo  保持这个窗口开着，关掉就停止接收数据
echo  地址：http://127.0.0.1:8765
echo ============================================
echo.

python -m bridge.server
echo.
echo 已停止。
pause
