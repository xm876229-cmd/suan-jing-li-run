# 注册「本地桥」开机自启：登录 Windows 后在后台静默启动，不需要黑窗口。
#
# 用法（普通 PowerShell 窗口，在项目根目录执行一次）：
#   powershell -ExecutionPolicy Bypass -File scripts\install_bridge_autostart.ps1
#
# 卸载：
#   Unregister-ScheduledTask -TaskName "算净利润-本地桥" -Confirm:$false
#
# 为什么需要它：
#   扩展取完数要把文件回传到本地桥，核算也要桥来触发。
#   桥只在当前窗口活着——电脑重启、或那个黑窗口被关掉，整条链就断了，
#   而且断得很安静：扩展那边只会报「连不上本地桥」。

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$taskName = "算净利润-本地桥"

# 复用 _python.bat 那套解释器解析逻辑（它会跳过 WindowsApps 的占位程序）
$resolver = Join-Path $PSScriptRoot "_python.bat"
if (-not (Test-Path $resolver)) { throw "找不到 $resolver" }

$pyexe = & cmd.exe /v:on /c "call `"$resolver`" >nul 2>nul & echo !PYEXE!" 2>$null
$pyexe = ($pyexe | Where-Object { $_ -and $_.Trim() } | Select-Object -Last 1)
if (-not $pyexe) { $pyexe = $env:PYEXE }

if (-not $pyexe -or -not (Test-Path $pyexe)) {
    throw "找不到可用的 Python 解释器。请先确认 scripts\start_bridge.bat 能正常启动。"
}
Write-Output "Python: $pyexe"

# 优先用 pythonw.exe：没有控制台窗口，后台跑完全不打扰
$pyw = Join-Path (Split-Path -Parent $pyexe) "pythonw.exe"
if (Test-Path $pyw) {
    $exe = $pyw
    Write-Output "使用无窗口解释器: $pyw"
} else {
    $exe = $pyexe
    Write-Output "没有 pythonw.exe，使用控制台解释器（会有个窗口）"
}

$action = New-ScheduledTaskAction -Execute $exe -Argument "-m bridge.server" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn

# 崩溃/被关掉后自动重启，避免静默失效
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Output "已移除同名旧任务，重新创建"
}

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "净利工具本地桥（127.0.0.1:8765），登录后自动启动" | Out-Null
Write-Output "已创建: $taskName（登录后自动启动，无窗口）"

# 8765 可能已被手动启动的桥占着（那个黑窗口）。端口只能被一个进程绑定，
# 硬启第二个会直接退出，还容易让人误判成「自启失败」。先探活，活着就不重复起。
$already = $false
try {
    $probe = Invoke-WebRequest -Uri "http://127.0.0.1:8765/health" -UseBasicParsing -TimeoutSec 3
    if ($probe.StatusCode -eq 200) { $already = $true }
} catch {
    $already = $false
}

if ($already) {
    Write-Output "本机已有桥在运行（可能是那个黑窗口），跳过立刻启动。"
    Write-Output "它关闭后，下次登录 Windows 会由计划任务自动接管。"
} else {
    try {
        Start-ScheduledTask -TaskName $taskName
        Start-Sleep -Seconds 3
        $probe = Invoke-WebRequest -Uri "http://127.0.0.1:8765/health" -UseBasicParsing -TimeoutSec 5
        Write-Output "已启动并探活成功: $($probe.Content)"
    } catch {
        Write-Output "任务已注册，但立刻探活失败: $($_.Exception.Message)"
    }
}

Write-Output ""
Write-Output "查看/修改: 任务计划程序 -> 任务计划程序库 -> $taskName"
