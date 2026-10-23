# 注册飞书机器人为 Windows 计划任务（登录自启 + 失败自动重启）
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File D:\kb-platform\tools\install-feishu-task.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File D:\kb-platform\tools\install-feishu-task.ps1 -Uninstall
#
# 与 install-task.ps1（知识库平台）同样的理由：
#   Start-Process 起的进程，父 shell 一被回收就会被连带终止 ——
#   日志里没有任何异常，只是服务静默消失。计划任务不受交互 shell 生命周期影响。
#
# 必须「以登录用户身份运行」：U:/V:/W:/X:/Y: 是该用户的持久盘符映射，
# 换账户运行就看不到 NAS 了。

param([switch]$Uninstall)

$TaskName = "OpenCode-Feishu-Bot"
$root = "D:\kb-platform"
$logs = "$root\kb\logs"
New-Item -ItemType Directory -Path $logs -Force | Out-Null

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
# 走 start-feishu-bot.ps1 包装：它会把 stdout/stderr 重定向进日志文件。
# 直接跑 python 的话，计划任务会把输出丢进 Task Scheduler 自己的捕获区，
# 出问题时什么都看不到。
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$root\tools\start-feishu-bot.ps1`"" `
  -WorkingDirectory $root

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
  -Description "飞书机器人：长连接接收指令 -> OpenCode 干活 -> 回复飞书" `
  -ErrorAction Stop | Out-Null

Write-Host "已注册计划任务 $TaskName" -ForegroundColor Green
Write-Host "  动作    : $py -m kbplatform.feishu.runner config.toml" -ForegroundColor Green
Write-Host "  触发器  : 登录时" -ForegroundColor Green
Write-Host "  失败重启: 最多 99 次，间隔 1 分钟" -ForegroundColor Green
Write-Host ""

# 先把手动跑着的实例停掉，避免和计划任务抢同一个 opencode 端口。
# 注意：机器人被强杀时，它 spawn 的 opencode serve 子进程**不会跟着死**，
# 会一直占着端口 —— 机器人启动时会用 .feishu/opencode.pid 清理孤儿，
# 这里额外清一次端口上的残留，保证立刻能起来。
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -and $_.CommandLine.Contains("kbplatform.feishu.runner") } |
  ForEach-Object {
    Write-Host "  停掉手动实例 PID=$($_.ProcessId)" -ForegroundColor Yellow
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
  }
Start-Sleep -Seconds 2

$port = 4599
try {
  $cfg = Get-Content "$root\config.toml" -Raw
  if ($cfg -match 'opencode_port\s*=\s*(\d+)') { $port = [int]$Matches[1] }
} catch { }
Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue |
  ForEach-Object {
    Write-Host "  清理占用端口 $port 的残留进程 PID=$($_.OwningProcess)" -ForegroundColor Yellow
    Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
  }
Start-Sleep -Seconds 2

Write-Host "通过计划任务启动机器人..." -ForegroundColor Cyan
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 20

$info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue
Write-Host "  任务状态: LastRunTime=$($info.LastRunTime)  LastTaskResult=$($info.LastTaskResult)" -ForegroundColor Green
Write-Host "  日志尾部:" -ForegroundColor DarkGray
Get-Content "$logs\feishu-bot.log.err" -Tail 6 -ErrorAction SilentlyContinue | ForEach-Object { "    $_" }
Write-Host ""
Write-Host "机器人已由计划任务托管" -ForegroundColor Green
Write-Host "  日志 : $logs\feishu-bot.log.err" -ForegroundColor Green
Write-Host "  停止 : Stop-ScheduledTask -TaskName $TaskName" -ForegroundColor Green
Write-Host ""
Write-Host "注意：知识库查询依赖平台服务（http://127.0.0.1:8000），" -ForegroundColor Cyan
Write-Host "      若平台没起，机器人会答不出知识库内容。平台由 OpenCode-KB-Platform 任务托管。" -ForegroundColor Cyan
