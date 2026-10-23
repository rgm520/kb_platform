# 看门狗：监督进程不在了就重新拉起平台
#
# 职责边界（重要）：
#   **Web 服务是否健康由监督进程自己管**（它有独立的健康检查与重启逻辑）。
#   看门狗只负责一件事：**监督进程本身不在了**，把平台重新拉起来。
#   早期版本看门狗还管健康检查，结果它自己 8 秒超时判失败就把
#   健康的服务杀了重启——用户看到的就是"服务异常"。教训：看门狗要保守。
#
# 保守策略：
#   * 连续 2 次检测不到监督进程才动手（避开任务启动瞬间的竞态）
#   * 刚重启过 120 秒内不再动（避免反复重启）
#   * 清理时只杀平台自己的进程（按命令行特征），不碰飞书机器人等其它服务
#
# 用法：powershell -NoProfile -ExecutionPolicy Bypass -File D:\kb-platform\tools\watchdog.ps1

$root = "D:\kb-platform"
$TaskName = "OpenCode-KB-Platform"
$log = "$root\kb\logs\watchdog.log"
$stateFile = "$root\kb\logs\watchdog-state.json"

function Log($msg) {
  $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg"
  Add-Content -Path $log -Value $line -Encoding UTF8
}

function Get-PlatformProcs {
  Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -and
      ($_.CommandLine.Contains("kbplatform.supervisor") -or
       $_.CommandLine.Contains("kbplatform.service:app") -or
       $_.CommandLine.Contains("kbplatform.pipeline")) }
}

try {
  $sup = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
           Where-Object { $_.CommandLine -and $_.CommandLine.Contains("kbplatform.supervisor") })

  # 状态：连续几次没看到监督进程 / 上次重启时间
  $st = @{ misses = 0; lastRestart = 0.0 }
  if (Test-Path $stateFile) {
    try { $st = Get-Content $stateFile -Raw -Encoding UTF8 | ConvertFrom-Json } catch { }
  }
  $st.misses = [int]$st.misses
  $st.lastRestart = [double]$st.lastRestart

  if ($sup.Count -gt 0) {
    if ($st.misses -ne 0) { Log "监督进程已恢复，并清零连续缺失计数" }
    $st.misses = 0
    $st | ConvertTo-Json | Set-Content -Path $stateFile -Encoding UTF8
    exit 0
  }

  $st.misses = $st.misses + 1
  $st | ConvertTo-Json | Set-Content -Path $stateFile -Encoding UTF8

  if ($st.misses -lt 2) {
    Log "未发现监督进程（第 $($st.misses) 次，暂不动作）"
    exit 0
  }

  $since = (Get-Date).AddSeconds(-120)
  if ($st.lastRestart -gt 0) {
    $last = (Get-Date).FromUnixTimeSeconds([int]$st.lastRestart)
    if ($last -gt $since) {
      Log "刚在 $([int]((Get-Date) - $last).TotalSeconds) 秒前重启过，跳过"
      $st.misses = 0
      $st | ConvertTo-Json | Set-Content -Path $stateFile -Encoding UTF8
      exit 0
    }
  }

  Log "连续 $($st.misses) 次未发现监督进程 -> 重新拉起平台"

  Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 2
  foreach ($p in @(Get-PlatformProcs)) {
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
  }
  Start-Sleep -Seconds 2

  Start-ScheduledTask -TaskName $TaskName
  $st.misses = 0
  $st.lastRestart = [int][double]::Parse((Get-Date -UFormat %s))
  $st | ConvertTo-Json | Set-Content -Path $stateFile -Encoding UTF8
  Log "已通过计划任务重新拉起 $TaskName"
} catch {
  Log "看门狗自身异常: $($_.Exception.Message)"
}
exit 0
