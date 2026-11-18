# 启动知识库平台（Web 服务 + 摄入流水线），由监督进程托管并自动重启
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File D:\kb-platform\tools\start-platform.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File D:\kb-platform\tools\start-platform.ps1 -Stop
#
# 开机自启（推荐，注册计划任务后即使无人登录也会拉起）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File D:\kb-platform\tools\install-task.ps1
#
# 说明：监督进程（kbplatform.supervisor）持有两个子进程，任何一个挂掉都会被
# 自动拉起；健康检查连续失败 3 次也会重启 Web 服务。

param([switch]$Stop)

$py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
$root = Split-Path -Parent $PSScriptRoot
$logs = "$root\kb\logs"
New-Item -ItemType Directory -Path $logs -Force | Out-Null

function Get-PlatformProcs {
  Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -and
      ($_.CommandLine.Contains("kbplatform.supervisor") -or
       $_.CommandLine.Contains("kbplatform.service:app") -or
       $_.CommandLine.Contains("kbplatform.pipeline")) }
}

if ($Stop) {
  Write-Host "停止知识库平台..." -ForegroundColor Yellow
  $procs = Get-PlatformProcs
  # 先停监督进程（它会带走子进程），再兜底清一遍
  foreach ($p in @($procs | Where-Object { $_.CommandLine.Contains("kbplatform.supervisor") })) {
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
  }
  Start-Sleep -Seconds 2
  foreach ($p in @(Get-PlatformProcs)) {
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
  }
  Write-Host "已停止" -ForegroundColor Yellow
  exit 0
}

# 幂等：已经在跑就不重复启动
$existing = @(Get-PlatformProcs)
if ($existing.Count -gt 0) {
  Write-Host "知识库平台已在运行（$($existing.Count) 个进程），不重复启动。" -ForegroundColor Green
  foreach ($p in $existing) {
    $k = if ($p.CommandLine.Contains("supervisor")) { "监督进程" }
         elseif ($p.CommandLine.Contains("service:app")) { "Web 服务 " }
         else { "摄入流水线" }
    Write-Host ("  " + $k + "  PID=" + $p.ProcessId)
  }
  try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:8000/health" -UseBasicParsing -TimeoutSec 8
    Write-Host ("  健康检查: " + $r.Content) -ForegroundColor Green
  } catch {
    Write-Host "  健康检查失败: $($_.Exception.Message)" -ForegroundColor Yellow
  }
  exit 0
}

# 会话密钥从 config.toml 的 [security].session_secret 读取；
# 如需覆盖再设置环境变量 KB_SESSION_SECRET（不要把真实密钥写进仓库）。
$env:PYTHONIOENCODING = "utf-8"

Write-Host "启动知识库平台监督进程..." -ForegroundColor Cyan
# 用 pythonw.exe + 隐藏窗口：服务在后台跑，不再弹黑色命令框
$pyBase = "$env:LOCALAPPDATA\Programs\Python\Python312"
$pyw = "$pyBase\pythonw.exe"
if (-not (Test-Path $pyw)) { $pyw = $py }
$p = Start-Process -FilePath $pyw `
  -ArgumentList "-m", "kbplatform.supervisor", "$root\config.toml" `
  -WorkingDirectory $root `
  -WindowStyle Hidden `
  -PassThru

Start-Sleep -Seconds 12
$alive = $false
try {
  $r = Invoke-WebRequest -Uri "http://127.0.0.1:8000/health" -UseBasicParsing -TimeoutSec 12
  $alive = ($r.StatusCode -eq 200)
  Write-Host ("  健康检查: " + $r.Content)
} catch { }

if ($alive) {
  Write-Host ""
  Write-Host "知识库平台已启动" -ForegroundColor Green
  Write-Host "  监督进程 PID : $($p.Id)" -ForegroundColor Green
  Write-Host "  本机     : http://127.0.0.1:8000" -ForegroundColor Green
  Write-Host "  局域网   : http://<本机IP>:8000" -ForegroundColor Green
  Write-Host "  日志     : $logs" -ForegroundColor Green
  Write-Host "  心跳     : $logs\supervisor-heartbeat.json" -ForegroundColor Green
} else {
  Write-Host "启动失败，请查看 $logs\supervisor.err.log" -ForegroundColor Red
  Get-Content "$logs\supervisor.err.log" -Tail 25 -ErrorAction SilentlyContinue
  exit 1
}
