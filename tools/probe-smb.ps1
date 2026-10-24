# 探测：当前会话能否读取 NAS 共享（用于判断后台/未登录运行的可行性）
# 输出写到 kb\logs\smb-probe.txt
$out = "D:\kb-platform\kb\logs\smb-probe.txt"
$lines = @()
$lines += "time: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
$lines += "session: $((Get-Process -Id $PID).SessionId)"
$lines += "user: $env:USERNAME"
$lines += "--- share list ---"
try {
  $items = Get-ChildItem "\\192.0.2.19\信息化管理" -ErrorAction Stop | Select-Object -First 5 -ExpandProperty Name
  $lines += "OK: $($items -join ', ')"
} catch {
  $lines += "FAIL: $($_.Exception.Message)"
}
Set-Content -Path $out -Value $lines -Encoding UTF8
