# 更新 / 重装 Superpowers
#
# 背景：本机 github.com / raw.githubusercontent.com / codeload 均被墙，
# 但 gh-proxy.com 可用。脚本走 codeload tarball + gh-proxy 下载，
# 解压后覆盖 ~/.config/opencode/superpowers。
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File D:\opencode\tools\update-superpowers.ps1

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$repo   = "obra/superpowers"
$dst    = "$env:USERPROFILE\.config\opencode\superpowers"
$cfg    = "$env:USERPROFILE\.config\opencode\opencode.json"
$work   = "$env:TEMP\sp_update"
$tarball = "$env:TEMP\superpowers.tar.gz"

$proxies = @(
  "https://gh-proxy.com/https://codeload.github.com/$repo/tar.gz/refs/heads/main",
  "https://gh-proxy.net/https://codeload.github.com/$repo/tar.gz/refs/heads/main",
  "https://ghproxy.com/https://codeload.github.com/$repo/tar.gz/refs/heads/main",
  "https://codeload.github.com/$repo/tar.gz/refs/heads/main"
)

Write-Host "==> 下载 $repo" -ForegroundColor Cyan
$ok = $false
foreach ($u in $proxies) {
  try {
    Write-Host "    尝试 $u"
    Invoke-WebRequest -Uri $u -OutFile $tarball -UseBasicParsing -TimeoutSec 180
    $ok = $true
    Write-Host "    成功，$((Get-Item $tarball).Length) 字节" -ForegroundColor Green
    break
  } catch {
    Write-Host "    失败: $($_.Exception.Message)" -ForegroundColor Yellow
  }
}
if (-not $ok) { throw "全部下载源都失败" }

Write-Host "==> 解压" -ForegroundColor Cyan
Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $work -Force | Out-Null
tar -xzf $tarball -C $work
$src = (Get-ChildItem $work -Directory | Select-Object -First 1).FullName

Write-Host "==> 覆盖安装到 $dst" -ForegroundColor Cyan
$backup = "$dst.bak.$(Get-Date -Format yyyyMMddHHmmss)"
if (Test-Path $dst) { Move-Item $dst $backup; Write-Host "    旧版本已备份到 $backup" }
Copy-Item $src $dst -Recurse -Force
foreach ($d in @("tests", "docs", "assets", ".github")) {
  if (Test-Path (Join-Path $dst $d)) { Remove-Item (Join-Path $dst $d) -Recurse -Force }
}

# 确保 opencode.json 里的 plugins 指向新目录
if (-not (Test-Path $cfg)) {
  @{ '$schema' = 'https://opencode.ai/config.json'; 'plugins' = @($dst) } |
    ConvertTo-Json -Depth 5 | Set-Content -Path $cfg -Encoding utf8
  Write-Host "    已创建 $cfg" -ForegroundColor Green
} else {
  $j = Get-Content $cfg -Raw | ConvertFrom-Json
  if (-not $j.plugins) { $j | Add-Member -NotePropertyName plugins -NotePropertyValue @($dst) }
  else { $j.plugins = @($dst) }
  $j | ConvertTo-Json -Depth 5 | Set-Content -Path $cfg -Encoding utf8
  Write-Host "    已更新 $cfg 的 plugins" -ForegroundColor Green
}

Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item $tarball -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "完成。重启 OpenCode (或 opencode service restart) 后生效。" -ForegroundColor Cyan
Write-Host "验证：在对话里问 'Tell me about your superpowers'，或用 skill 工具列出 skills。" -ForegroundColor Cyan
