# 看门狗：监督进程不在了就把平台重新拉起来
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
#   * 任何一步出错都不能吞掉"重新拉起"这个动作
#
# 2026-10-24 事故教训（这次改动就是为了修它）：
#   1) 冷却判断里写成 `(Get-Date).FromUnixTimeSeconds(...)`，把静态方法当实例
#      方法调用，运行即抛异常，被最外层 catch 吃掉 —— 看门狗从第二次重启起
#      永远走不到拉起逻辑，平台一次宕机就永久躺平。
#   2) `Stop-ScheduledTask` 之后立刻 `Start-ScheduledTask`，任务还挂在 Running
#      时启动会被 IgnoreNew 静默丢弃 —— 重启"看起来做了"，其实没做。
#   3) 计划任务只认登录触发：注销后没人拉起，登录后才恢复。
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File D:\kb-platform\tools\watchdog.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File D:\kb-platform\tools\watchdog.ps1 -SelfTest

param([switch]$SelfTest)

$root = "D:\kb-platform"
$TaskName = "OpenCode-KB-Platform"
$log = "$root\kb\logs\watchdog.log"
$stateFile = "$root\kb\logs\watchdog-state.json"
$MissThreshold = 2
$CooldownSec = 120

function Log($msg) {
  $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg"
  try { Add-Content -Path $log -Value $line -Encoding UTF8 } catch { }
}

# 统一的 unix 秒取值。
# FromUnixTimeSeconds 是 DateTimeOffset 的**静态**方法，写成
# `(Get-Date).FromUnixTimeSeconds(...)` 属于把静态方法当实例方法调用，
# 运行时会抛"方法调用失败"，2026-10-24 的宕机根因就在这一行。
function Get-UnixNow { [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() }

function Get-PlatformProcs {
  Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -and
      ($_.CommandLine.Contains("kbplatform.supervisor") -or
       $_.CommandLine.Contains("kbplatform.service:app") -or
       $_.CommandLine.Contains("kbplatform.pipeline")) }
}

function Get-SupervisorProcs {
  Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine.Contains("kbplatform.supervisor") }
}

# 重启决策：连续缺失次数达标，且不在冷却窗口内，才允许动手。
# 单独抽成函数，是为了能被 -SelfTest 直接断言，不再藏在主流程里。
function Test-ShouldRestart([int]$misses, [double]$lastRestart, [double]$now) {
  if ($misses -lt $MissThreshold) { return $false }
  if ($lastRestart -le 0) { return $true }
  return (($now - $lastRestart) -ge $CooldownSec)
}

function Test-PortBusy([int]$port) {
  try {
    $c = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    return ($null -ne $c -and @($c).Count -gt 0)
  } catch {
    return $false
  }
}

function Save-State($misses, $lastRestart) {
  try {
    @{ misses = [int]$misses; lastRestart = [double]$lastRestart } |
      ConvertTo-Json | Set-Content -Path $stateFile -Encoding UTF8
  } catch { }
}

function Read-State {
  $st = @{ misses = 0; lastRestart = 0.0 }
  if (Test-Path $stateFile) {
    try {
      $raw = Get-Content $stateFile -Raw -Encoding UTF8 | ConvertFrom-Json
      if ($null -ne $raw.misses) { $st.misses = [int]$raw.misses }
      if ($null -ne $raw.lastRestart) { $st.lastRestart = [double]$raw.lastRestart }
    } catch { }
  }
  return $st
}

# 停止平台：先停计划任务（会向任务控制台发 Ctrl+C），等任务真的离开 Running，
# 再兜底杀进程，最后等 8000 端口释放，避免新实例抢不到端口。
function Stop-Platform {
  $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  if ($task -and $task.State -eq "Running") {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  }
  for ($i = 0; $i -lt 15; $i++) {
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $t -or $t.State -ne "Running") { break }
    Start-Sleep -Seconds 1
  }
  foreach ($p in @(Get-PlatformProcs)) {
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
  }
  for ($i = 0; $i -lt 20; $i++) {
    if (-not (Test-PortBusy 8000)) { break }
    Start-Sleep -Seconds 1
  }
}

# 拉起平台：启动计划任务并等监督进程真的出现。
# 计划任务是 IgnoreNew：任务还挂在 Running 时启动会被静默丢弃，
# 所以先确保它回到 Ready 再 Start-ScheduledTask。
function Start-Platform {
  $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  if ($task -and $task.State -eq "Running") {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    for ($i = 0; $i -lt 15; $i++) {
      $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
      if (-not $t -or $t.State -ne "Running") { break }
      Start-Sleep -Seconds 1
    }
  }
  Start-ScheduledTask -TaskName $TaskName
  for ($i = 0; $i -lt 40; $i++) {
    if (@(Get-SupervisorProcs).Count -gt 0) { return $true }
    Start-Sleep -Seconds 1
  }
  return $false
}

if ($SelfTest) {
  $fails = 0
  function Assert-True($cond, $msg) {
    if ($cond) { Write-Host "ok  : $msg" }
    else { Write-Host "FAIL: $msg"; $script:fails = $script:fails + 1 }
  }
  $now = Get-UnixNow
  Assert-True ($now -gt 1700000000) "Get-UnixNow 返回 unix 秒"
  Assert-True (-not (Test-ShouldRestart 1 0 $now)) "连续 1 次缺失 -> 不重启"
  Assert-True (Test-ShouldRestart 2 0 $now) "连续 2 次缺失且从未重启 -> 重启"
  Assert-True (-not (Test-ShouldRestart 2 ($now - 30) $now)) "冷却窗口内 -> 不重启"
  Assert-True (Test-ShouldRestart 2 ($now - 300) $now) "冷却窗口外 -> 重启"
  Assert-True (-not (Test-ShouldRestart 2 ($now + 600) $now)) "上次重启时间在将来 -> 不重启"
  # 只看代码行（注释里可以解释这个坑，但不能再真的这么调用）
  $code = @(Get-Content $PSCommandPath -Encoding UTF8 |
    Where-Object { -not $_.TrimStart().StartsWith("#") })
  $bad = @($code | Where-Object { $_ -match "\(Get-Date\)\.FromUnixTimeSeconds" })
  Assert-True ($bad.Count -eq 0) "代码里不再把静态方法当实例方法调用"
  Assert-True (@($code | Where-Object { $_ -match "Test-ShouldRestart" }).Count -gt 0) "重启决策已抽成可测函数"
  if ($fails -eq 0) { Write-Host "SELFTEST OK"; exit 0 }
  Write-Host "SELFTEST FAILED ($fails)"
  exit 1
}

$sup = @(Get-SupervisorProcs)
$st = Read-State

if ($sup.Count -gt 0) {
  if ($st.misses -ne 0) { Log "监督进程已恢复，并清零连续缺失计数" }
  Save-State 0 $st.lastRestart
  exit 0
}

$st.misses = $st.misses + 1
Save-State $st.misses $st.lastRestart

$now = Get-UnixNow
$should = $true
try {
  $should = Test-ShouldRestart $st.misses $st.lastRestart $now
} catch {
  Log "判断重启时机失败（按需要重启处理）: $($_.Exception.Message)"
  $should = $true
}

if (-not $should) {
  Log "未发现监督进程（第 $($st.misses) 次，暂不动作）"
  exit 0
}

Log "连续 $($st.misses) 次未发现监督进程 -> 重新拉起平台"
try { Stop-Platform } catch { Log "停止平台时出错（继续尝试拉起）: $($_.Exception.Message)" }
$ok = $false
try { $ok = Start-Platform } catch { Log "拉起平台时出错: $($_.Exception.Message)" }
Save-State 0 (Get-UnixNow)
if ($ok) { Log "已通过计划任务重新拉起 $TaskName（监督进程已就位）" }
else { Log "拉起后仍未发现监督进程，请检查 $root\kb\logs\supervisor.err.log" }
exit 0
