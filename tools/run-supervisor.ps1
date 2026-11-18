# 拉起知识库平台监督进程（无窗口后台运行）
#
# 计划任务 OpenCode-KB-Platform 就跑这个脚本。
#
# 为什么用 pythonw.exe：
#   pythonw.exe 没有控制台，服务启动时不会再弹出黑色命令框。
#   监督进程自己把 stdout/stderr 写进 kb\logs\supervisor.*.log
#   （见 kbplatform/supervisor.py 的 setup_log_files），
#   所以不依赖 cmd 重定向，也就不需要中间那个 cmd 进程。
#
# 计划任务本身已经带 -WindowStyle Hidden，这里是第二道保险。

$root = Split-Path -Parent $PSScriptRoot
$logs = "$root\kb\logs"
New-Item -ItemType Directory -Path $logs -Force | Out-Null

$pyBase = "$env:LOCALAPPDATA\Programs\Python\Python312"
$pyw = "$pyBase\pythonw.exe"
$py = "$pyBase\python.exe"
if (-not (Test-Path $pyw)) { $pyw = $py }

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"

Set-Location $root

& $pyw -m kbplatform.supervisor "$root\config.toml"
exit $LASTEXITCODE
