# 注册定时任务：每天 09:00 自动跑「昨天」的净利，结果落在 output\
# 用法（在项目根目录、普通 PowerShell 窗口执行一次即可）：
#   powershell -ExecutionPolicy Bypass -File scripts\install_scheduled_task.ps1
# 卸载：
#   Unregister-ScheduledTask -TaskName "算净利润-每日" -Confirm:$false

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$bat  = Join-Path $PSScriptRoot "run.bat"
$taskName = "算净利润-每日"

if (-not (Test-Path $bat)) { throw "找不到 run.bat：$bat" }

$action = New-ScheduledTaskAction -Execute "cmd.exe" `
    -Argument "/c `"$bat`" scheduled" -WorkingDirectory $root

# 每天 09:00；机器关机错过的话，开机后补跑
$trigger = New-ScheduledTaskTrigger -Daily -At 09:00
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Output "已移除同名旧任务，重新创建"
}

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "每天自动核算昨日净利润（战斧店铺环境需已登录）" | Out-Null

Write-Output "已创建定时任务：$taskName（每天 09:00，补跑关闭期间的遗漏）"
Write-Output "工作目录：$root"
Write-Output "查看/修改：任务计划程序 → 任务计划程序库 → $taskName"
Write-Output ""
Write-Output "提示：定时运行要求数据已经导出到指定目录；浏览器取数环节需要战斧环境处于登录态。"
