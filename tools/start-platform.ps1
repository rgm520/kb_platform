# 启动知识库平台（Web 服务 + 摄入流水线）
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File D:\opencode\tools\start-platform.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File D:\opencode\tools\start-platform.ps1 -Stop
#
# 开机自启：把下面这行加到「任务计划程序 → 创建任务 → 触发器：计算机启动时」：
#   powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File D:\opencode\tools\start-platform.ps1

param([switch]$Stop)

$py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
$root = "D:\opencode"
$logs = "$root\kb\logs"
New-Item -ItemType Directory -Path $logs -Force | Out-Null

function Stop-Matching($needle) {
  Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine.Contains($needle) } |
    ForEach-Object {
      Write-Host "  停止 PID $($_.ProcessId) ($needle)"
      Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

if ($Stop) {
  Write-Host "停止知识库平台..." -ForegroundColor Yellow
  Stop-Matching "uvicorn"
  Stop-Matching "kbplatform.pipeline"
  Write-Host "已停止" -ForegroundColor Yellow
  exit 0
}

# 清理旧进程
Stop-Matching "uvicorn"
Stop-Matching "kbplatform.pipeline"
Start-Sleep -Seconds 2

# 会话签名密钥：固定值避免重启后会话失效
$env:KB_SESSION_SECRET = "CHANGE_ME_SESSION_SECRET"

Write-Host "启动知识库 Web 服务 (http://0.0.0.0:8000)..." -ForegroundColor Cyan
Start-Process -FilePath $py `
  -ArgumentList "-m", "uvicorn", "kbplatform.service:app",
                "--host", "0.0.0.0", "--port", "8000", "--log-level", "warning" `
  -WorkingDirectory $root `
  -RedirectStandardOutput "$logs\web.out.log" `
  -RedirectStandardError  "$logs\web.err.log"

Start-Sleep -Seconds 8

Write-Host "启动摄入流水线 (监控 NAS 知识库目录)..." -ForegroundColor Cyan
Start-Process -FilePath $py `
  -ArgumentList "-m", "kbplatform.pipeline", "$root\config.toml" `
  -WorkingDirectory $root `
  -RedirectStandardOutput "$logs\ingest.out.log" `
  -RedirectStandardError  "$logs\ingest.err.log"

Start-Sleep -Seconds 5

$ok = $false
try {
  $r = Invoke-WebRequest -Uri "http://127.0.0.1:8000/" -UseBasicParsing -TimeoutSec 15
  $ok = ($r.StatusCode -eq 200)
} catch { }

if ($ok) {
  Write-Host ""
  Write-Host "知识库平台已启动" -ForegroundColor Green
  Write-Host "  本机     : http://127.0.0.1:8000" -ForegroundColor Green
  Write-Host "  局域网   : http://<本机内网IP>:8000" -ForegroundColor Green
  Write-Host "  默认账号 : admin / admin12345  (请立即改密码)" -ForegroundColor Yellow
  Write-Host "  日志     : $logs" -ForegroundColor Green
} else {
  Write-Host "启动失败，请查看 $logs\web.err.log" -ForegroundColor Red
  Get-Content "$logs\web.err.log" -Tail 20 -ErrorAction SilentlyContinue
}
