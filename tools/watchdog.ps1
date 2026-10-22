# 看门狗：每分钟检查一次平台是否健康，异常则通过计划任务重新拉起
#
# 为什么不用 Task Scheduler 的「失败后重启」：
#   实测强杀任务进程后，LastTaskResult=0xFFFFFFFF 但任务直接转 Ready，
#   **不会**按 RestartCount 重新启动（外部终止不算「失败」）。
#   周期看门狗是 Windows 上更可靠的做法。
#
# 检查三件事，任一不满足就重启：
#   1. 监督进程存在
#   2. 心跳文件新鲜（< 90 秒）
#   3. /health 返回 200
#
# 重新拉起的方式是 Start-ScheduledTask（而不是自己拉进程），
# 这样新进程仍然归 Task Scheduler 托管，不会被看门狗自己的 shell 连坐杀掉。

$root = "D:\opencode"
$TaskName = "OpenCode-KB-Platform"
$hb = "$root\kb\logs\supervisor-heartbeat.json"
$log = "$root\kb\logs\watchdog.log"

function Log($msg) {
  $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg"
  Add-Content -Path $log -Value $line -Encoding UTF8
}

try {
  # 1) 监督进程
  $sup = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
           Where-Object { $_.CommandLine -and $_.CommandLine.Contains("kbplatform.supervisor") })

  # 2) 心跳新鲜度
  $hbFresh = $false
  if (Test-Path $hb) {
    try {
      $j = Get-Content $hb -Raw -Encoding UTF8 | ConvertFrom-Json
      # 注意：FromUnixTimeSeconds 是 [DateTimeOffset] 的**静态**方法，
      # 在 [DateTime] 实例上调用会抛 "方法调用失败"，
      # 若被 catch 吞掉就会让心跳永远判定为不新鲜，导致每分钟误重启。
      $parsed = [DateTimeOffset]::FromUnixTimeSeconds([int64]$j.ts).LocalDateTime
      $age = (Get-Date) - $parsed
      $hbFresh = ($age.TotalSeconds -lt 90)
    } catch {
      Log "心跳解析失败（将按不健康处理）: $($_.Exception.Message)"
      $hbFresh = $false
    }
  }

  # 3) 健康检查
  $healthy = $false
  try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:8000/health" -UseBasicParsing -TimeoutSec 8
    $healthy = ($r.StatusCode -eq 200)
  } catch { $healthy = $false }

  if ($sup.Count -gt 0 -and $hbFresh -and $healthy) {
    exit 0
  }

  Log "异常: 监督进程=$($sup.Count) 心跳新鲜=$hbFresh 健康=$healthy -> 重启"

  # 停掉任务（连带其子进程）与**所有**平台进程
  # ——包括孤儿监督进程：看门狗重启时若只清 web/ingest 不清孤儿监督进程，
  #   会出现两个监督进程并存，后者因端口被占用而反复崩溃。
  Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 2
  Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -and
      ($_.CommandLine.Contains("kbplatform.supervisor") -or
       $_.CommandLine.Contains("kbplatform.service:app") -or
       $_.CommandLine.Contains("kbplatform.pipeline")) } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  Start-Sleep -Seconds 3

  Start-ScheduledTask -TaskName $TaskName
  Log "已通过计划任务重新拉起 $TaskName"
} catch {
  Log "看门狗自身异常: $($_.Exception.Message)"
}
exit 0
