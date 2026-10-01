$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$RuntimeDir = Join-Path $ProjectRoot "work\runtime"
$PidFile = Join-Path $RuntimeDir "backend.pid"
$LogFile = Join-Path $RuntimeDir "backend.log"
$ErrorLog = Join-Path $RuntimeDir "backend-error.log"
$HostAddress = "127.0.0.1"
$Port = 8787
$Url = "http://${HostAddress}:${Port}"

if (-not (Test-Path -LiteralPath $VenvPython)) { throw "尚未完成首次安装。请先双击首次安装.bat。" }
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) { throw "未找到 FFmpeg。请先安装并加入 PATH。" }
if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot "frontend\dist\index.html"))) { throw "前端尚未构建。请先双击首次安装.bat。" }

$Module = if (Test-Path -LiteralPath (Join-Path $ProjectRoot "backend\app\main.py")) { "backend.app.main:app" }
    elseif (Test-Path -LiteralPath (Join-Path $ProjectRoot "backend\app.py")) { "backend.app:app" }
    elseif (Test-Path -LiteralPath (Join-Path $ProjectRoot "backend\main.py")) { "backend.main:app" }
    else { throw "找不到后端入口（backend/app/main.py、backend/app.py 或 backend/main.py）。" }

function Get-ListeningPids {
    $listeningResults = @()
    netstat.exe -ano -p tcp 2>$null | ForEach-Object {
        if ($_ -match "^\s*TCP\s+\S+:$Port\s+\S+\s+LISTENING\s+(\d+)\s*$") { $listeningResults += [int]$Matches[1] }
    }
    return $listeningResults | Select-Object -Unique
}

function Get-ProcessInfo([int]$ProcessId) {
    return Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
}

function Test-OwnProcess($ProcessInfo) {
    if (-not $ProcessInfo) { return $false }
    $line = ([string]$ProcessInfo.CommandLine).ToLowerInvariant()
    $root = $ProjectRoot.ToLowerInvariant()
    return $line.Contains($root) -and $line.Contains($Module.ToLowerInvariant()) -and $line.Contains("--port $Port")
}

function Test-Health {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "$Url/api/health" -TimeoutSec 2
        if ($response.StatusCode -ne 200) { return $false }
        $payload = $response.Content | ConvertFrom-Json
        return $null -ne $payload.overall -and $null -ne $payload.app -and $null -ne $payload.capabilities
    } catch {
        # 依赖服务的健康检查可能需要较长时间；API 已能返回设置时，前端可以先打开并显示降级状态。
        try {
            $settingsResponse = Invoke-WebRequest -UseBasicParsing -Uri "$Url/api/settings" -TimeoutSec 3
            if ($settingsResponse.StatusCode -ne 200) { return $false }
            $settingsPayload = $settingsResponse.Content | ConvertFrom-Json
            return $null -ne $settingsPayload.config -and $null -ne $settingsPayload.levels
        } catch { return $false }
    }
}

New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
$ownPid = $null
$listeningPids = @(Get-ListeningPids)
foreach ($listenerPid in $listeningPids) {
    $info = Get-ProcessInfo $listenerPid
    if (Test-OwnProcess $info) { $ownPid = $listenerPid; continue }
    throw "端口 $Port 已被其他进程占用（PID $listenerPid），不会停止或复用该进程。请更换端口或先处理占用者。"
}

if (Test-Path -LiteralPath $PidFile) {
    $savedPidText = (Get-Content -LiteralPath $PidFile -Raw).Trim()
    $savedPid = 0
    if ([int]::TryParse($savedPidText, [ref]$savedPid)) {
        $savedInfo = Get-ProcessInfo $savedPid
        if ($savedInfo -and (Test-OwnProcess $savedInfo)) { $ownPid = $savedPid }
        elseif ($savedInfo) { throw "运行记录 PID $savedPid 属于其他进程，拒绝停止。请手动检查 $PidFile。" }
    }
}

if ($ownPid) {
    if (-not (Test-Health)) { throw "本项目后端进程（PID $ownPid）存在但健康检查失败。请查看 $ErrorLog。" }
} else {
    $arguments = @("-m", "uvicorn", $Module, "--app-dir", $ProjectRoot, "--host", $HostAddress, "--port", "$Port")
    $process = Start-Process -FilePath $VenvPython -ArgumentList $arguments -WorkingDirectory $ProjectRoot -WindowStyle Hidden -RedirectStandardOutput $LogFile -RedirectStandardError $ErrorLog -PassThru
    $ownPid = $process.Id
    Set-Content -LiteralPath $PidFile -Value $ownPid -Encoding ascii
    $ready = $false
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        if (Test-Health) { $ready = $true; break }
        foreach ($listenerPid in @(Get-ListeningPids)) {
            if ($listenerPid -eq $ownPid) { continue }
            $info = Get-ProcessInfo $listenerPid
            if ($info -and -not (Test-OwnProcess $info)) { throw "启动期间端口 $Port 被其他进程占用（PID $listenerPid），未停止该进程。" }
        }
        Start-Sleep -Milliseconds 500
    }
    if (-not $ready) { throw "后端未能在 20 秒内启动。请查看 $ErrorLog" }
    $readyListener = @(Get-ListeningPids)
    if ($readyListener.Count -gt 0) {
        $ownPid = [int]$readyListener[0]
        Set-Content -LiteralPath $PidFile -Value $ownPid -Encoding ascii
    }
}

try {
    Start-Process $Url -ErrorAction Stop | Out-Null
} catch {
    Write-Warning "后端已经启动，但系统没有自动打开浏览器；请手动访问 $Url。"
}
Write-Host "童声配音台已启动：$Url" -ForegroundColor Green
Write-Host "停止服务请双击“停止童声配音台.bat”。"
