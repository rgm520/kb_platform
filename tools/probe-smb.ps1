param([string]$Share = $env:KB_PROBE_SHARE)
# 探测：当前会话能否读取 NAS 共享（用于判断后台/未登录运行的可行性）
# 输出写到 kb\logs\smb-probe.txt
$out = "$(Split-Path -Parent $PSScriptRoot)\kb\logs\smb-probe.txt"
$lines = @()
$lines += "time: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
$lines += "session: $((Get-Process -Id $PID).SessionId)"
$lines += "user: $env:USERNAME"
$lines += "--- share list ---"
if (-not $Share) { $Share = Read-Host "共享路径（例如 \\server\share）" }
try {
  $items = Get-ChildItem $Share -ErrorAction Stop | Select-Object -First 5 -ExpandProperty Name
  $lines += "OK: $($items -join ', ')"
} catch {
  $lines += "FAIL: $($_.Exception.Message)"
}
Set-Content -Path $out -Value $lines -Encoding UTF8
