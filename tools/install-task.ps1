# 注册知识库平台为 Windows 计划任务（开机自启 + 失败自动重启）
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File D:\opencode\tools\install-task.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File D:\opencode\tools\install-task.ps1 -Uninstall
#
# 为什么用计划任务而不是 Start-Process：
#   Start-Process 起的进程，父 shell 一被回收（会话结束 / 终端关闭 / 命令超时被杀）
# 就会被连带终止，日志里没有任何异常，只是服务静默消失——这是实际踩过的坑。
#   计划任务由 Task Scheduler 托管，不受交互 shell 生命周期影响，且能开机自启。
#
# 必须「以登录用户身份运行」：U:/V:/W:/X:/Y: 是该用户的持久盘符映射，
# 换账户运行就看不到 NAS 了。

param([switch]$Uninstall)

$TaskName = "OpenCode-KB-Platform"
$root = "D:\opencode"
$script = "$root\tools\start-platform.ps1"

if ($Uninstall) {
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
  Write-Host "已删除计划任务 $TaskName" -ForegroundColor Yellow
  exit 0
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
  Write-Host "计划任务已存在，先删除再重建" -ForegroundColor Yellow
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
# 走 run-supervisor.ps1 包装：它把 stdout/stderr 重定向进日志文件。
# 直接跑 python 的话，计划任务会把输出丢进 Task Scheduler 自己的捕获区，
# **出问题时什么都看不到**（踩过：监督进程死在哪完全无从查起）。
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$root\tools\run-supervisor.ps1`"" `
  -WorkingDirectory $root

# 登录时启动（需要用户会话才能拿到盘符映射）
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME `
  -LogonType Interactive -RunLevel Highest

$settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries `
  -StartWhenAvailable `
  -RestartCount 99 `
  -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit ([TimeSpan]::Zero) `
  -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName `
  -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
  -Description "企业知识平台：Web 服务 + 摄入流水线（监督进程托管，崩溃自动重启）" `
  -ErrorAction Stop | Out-Null

Write-Host "已注册计划任务 $TaskName" -ForegroundColor Green
Write-Host "  动作    : $py -m kbplatform.supervisor config.toml" -ForegroundColor Green
Write-Host "  触发器  : 登录时" -ForegroundColor Green
Write-Host "  失败重启: 最多 99 次，间隔 1 分钟" -ForegroundColor Green
Write-Host ""
Write-Host "任务动作直接就是监督进程（前台常驻），因此："
Write-Host "  * Task Scheduler 持有它，shell 退出不会被连坐杀掉"
Write-Host "  * 监督进程再把 Web 服务与摄入流水线拉起并看护" -ForegroundColor Cyan

# ---------------------------------------------------------------- 看门狗
# Task Scheduler 的「失败后重启」对外部强杀不生效（实测 LastTaskResult=0xFFFFFFFF
# 但任务直接转 Ready，不会重启），所以用周期看门狗兜底。
$WatchName = "OpenCode-KB-Watchdog"
$wAction = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$root\tools\watchdog.ps1`"" `
  -WorkingDirectory $root
$wTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
  -RepetitionInterval (New-TimeSpan -Minutes 1) `
  -RepetitionDuration ([TimeSpan]::FromDays(3650))   # 10 年；MaxValue 会被 Task Scheduler 判为越界
$wSettings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
  -MultipleInstances IgnoreNew `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

Unregister-ScheduledTask -TaskName $WatchName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $WatchName `
  -Action $wAction -Trigger $wTrigger -Principal $principal -Settings $wSettings `
  -Description "企业知识平台看门狗：每分钟检查，异常则重启平台" -ErrorAction Stop | Out-Null
Write-Host "已注册看门狗任务 $WatchName（每 1 分钟）" -ForegroundColor Green
Write-Host ""

# 立即通过计划任务启动（关键：由 Task Scheduler 托管，不在调用者的
# shell job object 里，因此调用者退出后服务不会被连坐杀掉）
Write-Host "通过计划任务启动平台..." -ForegroundColor Cyan
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 14

$ok = $false
try {
  $r = Invoke-WebRequest -Uri "http://127.0.0.1:8000/health" -UseBasicParsing -TimeoutSec 12
  $ok = ($r.StatusCode -eq 200)
  Write-Host ("  健康检查: " + $r.Content) -ForegroundColor Green
} catch { }

if (-not $ok) {
  Write-Host "  健康检查失败，查看 $root\kb\logs\supervisor.err.log" -ForegroundColor Red
  Get-Content "$root\kb\logs\supervisor.err.log" -Tail 20 -ErrorAction SilentlyContinue
  exit 1
}

Write-Host ""
Write-Host "平台已由计划任务托管" -ForegroundColor Green
Write-Host "  本机   : http://127.0.0.1:8000" -ForegroundColor Green
Write-Host "  局域网 : http://192.0.2.203:8000" -ForegroundColor Green
Write-Host "  状态   : Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo" -ForegroundColor Green
Write-Host "  停止   : powershell -File `"$script`" -Stop" -ForegroundColor Green
