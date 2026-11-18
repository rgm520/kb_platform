# 备份知识库平台索引（kb.db + fts.db + vectors/）
#
# 计划任务 OpenCode-KB-Backup 每天调用；也可手工触发：
#   powershell -NoProfile -ExecutionPolicy Bypass -File D:\kb-platform\tools\backup-db.ps1
#   参数：-Dest <备份目录>  -Keep <保留份数，默认 7>
#
# 备份为什么不能直接复制文件（详见 docs/runbook/索引重建与备份.md）：
#   kb.db 是 WAL 模式，已提交但还没 checkpoint 的数据躺在 kb.db-wal 里，
#   直接复制 kb.db 会漏掉这些事务 —— 恢复时可能直接打不开。
#   python -m kbplatform.backup 用 SQLite 官方备份 API（Connection.backup）
#   在线做一致性快照，不删不改生产数据，对源库只读。
#
# 备份目录必须落在被监控的 NAS 根之外（默认本地盘 kb\backups），
# 否则备份文件会被平台自己扫进索引（Task 0.1 刚修过这类自吞问题）。

param(
  [string]$Dest = "",
  [int]$Keep = 7
)

$root = Split-Path -Parent $PSScriptRoot
$pyBase = "$env:LOCALAPPDATA\Programs\Python\Python312"
$py = "$pyBase\python.exe"

# 备份时长/结果日志由 python 直接写进 kb\logs\backup.log（UTF-8，不经过
# PowerShell 管道解码，中文不会坏——见 kbplatform/backup.py 的 _log）。
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"

Set-Location $root

$pyArgs = @("-m", "kbplatform.backup", "--config", "$root\config.toml", "--keep", $Keep)
if ($Dest) { $pyArgs += @("--dest", $Dest) }

& $py @pyArgs
# 失败要让计划任务看到（非 0 会触发重试），成功 0
exit $LASTEXITCODE
