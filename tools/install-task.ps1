# 注册知识库平台为 Windows 计划任务
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File D:\kb-platform\tools\install-task.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File D:\kb-platform\tools\install-task.ps1 -Mode Service
#   powershell -NoProfile -ExecutionPolicy Bypass -File D:\kb-platform\tools\install-task.ps1 -Uninstall
#
# 两种运行模式：
#   Interactive（默认）——「以登录用户身份运行」，登录时启动。
#       盘符映射（U:/V:/W:/X:/Y:）一定可用；但**注销后平台会停**，要等下次登录。
#   Service ——「不管用户是否登录都运行」，开机自启 + 注销后继续跑。
#       需要 Windows 账号密码（脚本会本地弹窗索要，密码只写进任务凭据，不进命令行/日志）。
#       实测：S4U（不存密码）在后台会话里**读不到 NAS**（无网络凭据），
#       所以后台模式必须用 Password 方式保存凭据。
#
# 为什么用计划任务而不是 Start-Process：
#   Start-Process 起的进程，父 shell 一被回收（会话结束 / 终端关闭 / 命令超时被杀）
#   就会被连带终止，日志里没有任何异常，只是服务静默消失——这是实际踩过的坑。
#   计划任务由 Task Scheduler 托管，不受交互 shell 生命周期影响，且能开机自启。

param(
  [switch]$Uninstall,
  [ValidateSet("Interactive", "Service")]
  [string]$Mode = "Interactive",
  [switch]$SkipVerify
)

$TaskName = "OpenCode-KB-Platform"
$WatchName = "OpenCode-KB-Watchdog"
$BackupName = "OpenCode-KB-Backup"
$root = "D:\kb-platform"
$logs = "$root\kb\logs"

if ($Uninstall) {
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
  Unregister-ScheduledTask -TaskName $WatchName -Confirm:$false -ErrorAction SilentlyContinue
  Unregister-ScheduledTask -TaskName $BackupName -Confirm:$false -ErrorAction SilentlyContinue
  Write-Host "已删除计划任务 $TaskName / $WatchName / $BackupName" -ForegroundColor Yellow
  exit 0
}

# ---------------------------------------------------------------- 凭据
$cred = $null
$user = $env:USERNAME
if ($Mode -eq "Service") {
  Write-Host "后台常驻模式：需要本机 Windows 账号密码（只保存在任务凭据里）。" -ForegroundColor Cyan
  $cred = Get-Credential -UserName $user -Message "请输入运行知识库平台的 Windows 账号密码（$user）"
  if (-not $cred) { Write-Host "未提供凭据，已放弃。" -ForegroundColor Red; exit 1 }
  $user = $cred.UserName
}

# ---------------------------------------------------------------- 停旧实例
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
  Write-Host "清理旧的计划任务与运行实例..." -ForegroundColor Yellow
  Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 2
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
  Where-Object { $_.CommandLine -and
    ($_.CommandLine.Contains("kbplatform.supervisor") -or
     $_.CommandLine.Contains("kbplatform.service:app") -or
     $_.CommandLine.Contains("kbplatform.pipeline")) } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2

# ---------------------------------------------------------------- 动作
# 走 run-supervisor.ps1 包装：它用 pythonw 启动监督进程（无黑色控制台），
# 日志自己写进 kb\logs\supervisor.*.log。
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$root\tools\run-supervisor.ps1`"" `
  -WorkingDirectory $root

$settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries `
  -StartWhenAvailable `
  -RestartCount 99 `
  -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit ([TimeSpan]::Zero) `
  -MultipleInstances IgnoreNew

# ---------------------------------------------------------------- 注册平台任务
# 失败必须能回退：2026-10-24 试 Service 模式时凭据被拒，脚本中途退出，
# 结果**任务被注销、平台也被停了**，等于平台彻底没人管。绝不能重演。
function Register-PlatformTask($mode, $cred, $user) {
  if ($mode -eq "Service") {
    # 开机自启 + 登录时也启动（两种情况都能兜住）
    $triggers = @()
    $tStart = New-ScheduledTaskTrigger -AtStartup
    $tStart.Delay = "PT1M"          # 等网络与磁盘就绪
    $triggers += $tStart
    $triggers += (New-ScheduledTaskTrigger -AtLogOn -User $user)
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Password -RunLevel Highest
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers `
      -Principal $principal -Settings $settings -User $user -Password $cred.GetNetworkCredential().Password `
      -Description "企业知识平台：Web 服务 + 摄入流水线（后台常驻，开机自启，崩溃自动重启）" `
      -ErrorAction Stop | Out-Null
  } else {
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
      -Principal $principal -Settings $settings `
      -Description "企业知识平台：Web 服务 + 摄入流水线（登录时启动，崩溃自动重启）" `
      -ErrorAction Stop | Out-Null
  }
}

try {
  Register-PlatformTask $Mode $cred $user
} catch {
  Write-Host ""
  Write-Host "计划任务注册失败: $($_.Exception.Message)" -ForegroundColor Red
  if ($Mode -eq "Service") {
    Write-Host "回退到「登录时启动」模式，保证平台照常有人管……" -ForegroundColor Yellow
    try {
      Register-PlatformTask "Interactive" $null $user
      Start-ScheduledTask -TaskName $TaskName
      Write-Host "已回退成功，平台按「登录时启动」运行（注销后需重新登录才恢复）。" -ForegroundColor Green
    } catch {
      Write-Host "回退也失败: $($_.Exception.Message)" -ForegroundColor Red
      Write-Host "请手动执行：powershell -File $root\tools\start-platform.ps1" -ForegroundColor Yellow
    }
  }
  exit 1
}
if ($Mode -eq "Service") {
  Write-Host "已注册计划任务 $TaskName（后台常驻：开机自启 + 注销后继续运行）" -ForegroundColor Green
} else {
  Write-Host "已注册计划任务 $TaskName（登录时启动）" -ForegroundColor Green
}
Write-Host "  失败重启: 最多 99 次，间隔 1 分钟" -ForegroundColor Green

# ---------------------------------------------------------------- 看门狗
# Task Scheduler 的「失败后重启」对外部强杀不生效（实测 LastTaskResult=0xFFFFFFFF
# 但任务直接转 Ready，不会重启），所以用周期看门狗兜底。
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

try {
  if ($Mode -eq "Service") {
    Register-ScheduledTask -TaskName $WatchName -Action $wAction -Trigger $wTrigger `
      -Principal $principal -Settings $wSettings -User $user -Password $cred.GetNetworkCredential().Password `
      -Description "企业知识平台看门狗：每分钟检查，异常则重启平台" -ErrorAction Stop | Out-Null
  } else {
    Register-ScheduledTask -TaskName $WatchName -Action $wAction -Trigger $wTrigger `
      -Principal $principal -Settings $wSettings `
      -Description "企业知识平台看门狗：每分钟检查，异常则重启平台" -ErrorAction Stop | Out-Null
  }
} catch {
  Write-Host "看门狗任务注册失败: $($_.Exception.Message)" -ForegroundColor Red
  Write-Host "（平台任务已就位，看门狗缺失只会影响自动拉起能力）" -ForegroundColor Yellow
}
Write-Host "已注册看门狗任务 $WatchName（每 1 分钟）" -ForegroundColor Green

# ---------------------------------------------------------------- 每日备份
# 每天 02:00 用 SQLite 备份 API 固化一份索引（kb.db + fts.db + vectors），
# 保留最近 7 份。备份目录在本地盘 kb\backups，不会落进被监控的 NAS 根。
# 失败重试 3 次：半夜 NAS/网络抖一下没理由放弃整晚的备份。
$bAction = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$root\tools\backup-db.ps1`"" `
  -WorkingDirectory $root
$bTrigger = New-ScheduledTaskTrigger -Daily -At 02:00
$bSettings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
  -MultipleInstances IgnoreNew `
  -RestartCount 3 `
  -RestartInterval (New-TimeSpan -Minutes 5) `
  -ExecutionTimeLimit (New-TimeSpan -Hours 2)

try {
  if ($Mode -eq "Service") {
    Register-ScheduledTask -TaskName $BackupName -Action $bAction -Trigger $bTrigger `
      -Principal $principal -Settings $bSettings -User $user -Password $cred.GetNetworkCredential().Password `
      -Description "企业知识平台每日备份：02:00 快照索引库，保留最近 7 份" -ErrorAction Stop | Out-Null
  } else {
    Register-ScheduledTask -TaskName $BackupName -Action $bAction -Trigger $bTrigger `
      -Principal $principal -Settings $bSettings `
      -Description "企业知识平台每日备份：02:00 快照索引库，保留最近 7 份" -ErrorAction Stop | Out-Null
  }
} catch {
  Write-Host "备份任务注册失败: $($_.Exception.Message)" -ForegroundColor Red
  Write-Host "（平台照常运行，可手工执行 $root\tools\backup-db.ps1 备份）" -ForegroundColor Yellow
}
Write-Host "已注册备份任务 $BackupName（每天 02:00，失败重试 3 次）" -ForegroundColor Green

# ---------------------------------------------------------------- 后台可访问 NAS 验证
if ($Mode -eq "Service" -and -not $SkipVerify) {
  Write-Host ""
  Write-Host "验证后台会话能否读取 NAS 共享..." -ForegroundColor Cyan
  $probeName = "KB-SmbProbe-Test"
  $probeAction = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$root\tools\probe-smb.ps1`""
  $probeTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5)
  Unregister-ScheduledTask -TaskName $probeName -Confirm:$false -ErrorAction SilentlyContinue
  Register-ScheduledTask -TaskName $probeName -Action $probeAction -Trigger $probeTrigger `
    -Principal $principal -Settings $wSettings -User $user -Password $cred.GetNetworkCredential().Password `
    -ErrorAction Stop | Out-Null
  Start-ScheduledTask -TaskName $probeName
  Start-Sleep -Seconds 12
  $probeOut = "$logs\smb-probe.txt"
  if (Test-Path $probeOut) {
    $txt = Get-Content $probeOut -Raw
    if ($txt -match "OK:") {
      Write-Host "  后台会话可访问 NAS ✓" -ForegroundColor Green
    } else {
      Write-Host "  后台会话**读不到 NAS** ✗" -ForegroundColor Red
      Write-Host "  多半是该 Windows 账号在 NAS 上没有权限；可以给 2.0 的每个共享单独填校验账号密码" -ForegroundColor Yellow
      Write-Host "  （管理台 → 存储接入 → 编辑 → 账号/密码），老流水线则需要用有权限的账号。" -ForegroundColor Yellow
    }
  }
  Unregister-ScheduledTask -TaskName $probeName -Confirm:$false -ErrorAction SilentlyContinue
}

# ---------------------------------------------------------------- 启动
Write-Host ""
Write-Host "通过计划任务启动平台..." -ForegroundColor Cyan
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 16

$ok = $false
try {
  $r = Invoke-WebRequest -Uri "http://127.0.0.1:8000/health" -UseBasicParsing -TimeoutSec 12
  $ok = ($r.StatusCode -eq 200)
  Write-Host ("  健康检查: " + $r.Content) -ForegroundColor Green
} catch { }

if (-not $ok) {
  Write-Host "  健康检查失败，查看 $logs\supervisor.err.log" -ForegroundColor Red
  Get-Content "$logs\supervisor.err.log" -Tail 20 -ErrorAction SilentlyContinue
  exit 1
}

Write-Host ""
Write-Host "平台已由计划任务托管" -ForegroundColor Green
Write-Host "  本机   : http://127.0.0.1:8000" -ForegroundColor Green
Write-Host "  局域网 : http://192.0.2.203:8000" -ForegroundColor Green
Write-Host "  状态   : Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo" -ForegroundColor Green
Write-Host "  日志   : $logs" -ForegroundColor Green
