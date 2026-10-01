$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PidFile = Join-Path $ProjectRoot "work\runtime\backend.pid"
$Port = 8787

if (-not (Test-Path -LiteralPath $PidFile)) {
    Write-Host "没有找到本项目的运行记录。"
    exit 0
}

$savedPidText = (Get-Content -LiteralPath $PidFile -Raw).Trim()
$savedPid = 0
if (-not [int]::TryParse($savedPidText, [ref]$savedPid)) { throw "运行记录不是有效 PID，拒绝停止。" }
$processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $savedPid" -ErrorAction SilentlyContinue
if (-not $processInfo) {
    Remove-Item -LiteralPath $PidFile -Force
    Write-Host "服务已经停止；已清理过期运行记录。"
    exit 0
}

$line = ([string]$processInfo.CommandLine).ToLowerInvariant()
$root = $ProjectRoot.ToLowerInvariant()
$isOwn = $line.Contains($root) -and $line.Contains("uvicorn") -and $line.Contains("backend.app") -and $line.Contains("--port $Port")
if (-not $isOwn) { throw "PID $savedPid 已被其他进程占用，拒绝停止。请手动检查 PID。" }

Stop-Process -Id $savedPid -ErrorAction Stop
Remove-Item -LiteralPath $PidFile -Force
Write-Host "童声配音台已停止；仅停止了本项目记录的后端进程。" -ForegroundColor Green
