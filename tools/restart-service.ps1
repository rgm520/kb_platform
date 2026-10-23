param([switch]$Restart)
$py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'"
foreach ($p in $procs) {
  if ($p.CommandLine -and $p.CommandLine.Contains("uvicorn")) {
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
  }
}
Start-Sleep -Seconds 2
# 会话密钥由 config.toml 注入（勿硬编码）
Start-Process -FilePath $py -ArgumentList "-m","uvicorn","kbplatform.service:app","--host","0.0.0.0","--port","8000","--log-level","warning" -WorkingDirectory "D:\kb-platform" -RedirectStandardOutput "$env:TEMP\svc_out.txt" -RedirectStandardError "$env:TEMP\svc_err.txt"
Start-Sleep -Seconds 10
$t = Test-NetConnection -ComputerName 127.0.0.1 -Port 8000 -WarningAction SilentlyContinue
Write-Output ("服务端口 8000: " + $t.TcpTestSucceeded)
