# 启动飞书机器人（Windows 计划任务用这个脚本）
#
# 计划任务配置见 tools/install-feishu-task.ps1。
#
# 机器人进程会自己拉起并守护一个独立的 opencode serve（端口 4599），
# 所以这里只需要登记这一条任务。
#
# 输出重定向到 kb\logs\feishu-bot.log{,.err}：直接跑 python 的话，
# 计划任务会把 stdout/stderr 丢进 Task Scheduler 自己的捕获区，出问题看不到。

$ErrorActionPreference = "Stop"

$root = "D:\kb-platform"
$logs = "$root\kb\logs"
New-Item -ItemType Directory -Path $logs -Force | Out-Null

$py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
if (-not (Test-Path $py)) {
    $py = (Get-Command python -ErrorAction SilentlyContinue).Source
}
if (-not $py) { throw "找不到 python 解释器" }

Set-Location $root
$env:PYTHONPATH = $root
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

Write-Host "[feishu-bot] 启动：$py -m kbplatform.feishu.runner"
# 这里必须用 Continue，且**不能**做 `2>&1 | Out-Null`：
#   * PowerShell 会把原生命令的 stderr 包成错误记录，Stop 模式下第一行 stderr
#     就把脚本打断（管道一断，python 写 stdout 时就炸 "_readerthread" 异常）；
#   * `>>` 重定向写的是 UTF-16，会把日志文件搞成两种编码混排。
# 日志由 Python 自己写进 kb\logs\feishu-bot.log（UTF-8），
# 未捕获的异常也由 runner.main() 里的 log.exception 记进去。
$ErrorActionPreference = "Continue"
& $py -m kbplatform.feishu.runner "$root\config.toml"
Write-Host "[feishu-bot] 已退出（代码 $LASTEXITCODE）"
