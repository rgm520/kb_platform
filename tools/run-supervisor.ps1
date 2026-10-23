# 监督进程包装：把 stdout/stderr 重定向进日志文件
#
# 计划任务直接跑 `python -m kbplatform.supervisor` 时，输出会被丢进
# Task Scheduler 自己的捕获区，**出问题时什么都看不到**（踩过）。
# 这个包装把输出落到 kb\logs\supervisor.*.log。
#
# 由 install-task.ps1 注册的计划任务调用。

$root = "D:\kb-platform"
$py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
$logs = "$root\kb\logs"
New-Item -ItemType Directory -Path $logs -Force | Out-Null

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"

Set-Location $root

# 用 cmd 的重定向而不是 PowerShell 的 `>>`：
# PowerShell 5.1 的 `>>` 会把输出转成 UTF-16LE，日志里就是乱码；
# cmd 的 `>>` 直接透传子进程的原始字节，UTF-8 得以保留。
& cmd /c "`"$py`" -m kbplatform.supervisor `"$root\config.toml`" 1>>`"$logs\supervisor.out.log`" 2>>`"$logs\supervisor.err.log`""
exit $LASTEXITCODE
