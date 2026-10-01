$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$FrontendDir = Join-Path $ProjectRoot "frontend"

function Require-Command([string]$Name, [string]$Hint) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "缺少 $Name。$Hint"
    }
}

Require-Command "python" "请安装 Python 3.11 或更高版本，并重新打开此窗口。"
Require-Command "npm" "请安装 Node.js 20 或更高版本。"
Require-Command "ffmpeg" "请安装 FFmpeg，并把 ffmpeg.exe 加入 PATH。"
Require-Command "ffprobe" "请确认 ffprobe.exe 与 ffmpeg.exe 位于同一目录。"

$VersionText = & python -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
Write-Host "Python: $VersionText"
Write-Host "正在准备项目隔离环境…"

$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $VenvPython)) {
    & python -m venv (Join-Path $ProjectRoot ".venv")
}

if (Test-Path -LiteralPath (Join-Path $ProjectRoot "pyproject.toml")) {
    & $VenvPython -m pip install --upgrade pip
    & $VenvPython -m pip install -e $ProjectRoot
} elseif (Test-Path -LiteralPath (Join-Path $ProjectRoot "requirements.txt")) {
    & $VenvPython -m pip install -r (Join-Path $ProjectRoot "requirements.txt")
} else {
    throw "后端依赖文件尚未生成；请等待项目文件完整后再运行首次安装。"
}

Push-Location $FrontendDir
try {
    & npm install
    if ($LASTEXITCODE -ne 0) { throw "前端依赖安装失败。" }
    & npm run build
    if ($LASTEXITCODE -ne 0) { throw "前端构建失败。" }
} finally {
    Pop-Location
}

Write-Host "安装完成。现在可以双击“启动童声配音台.bat”。" -ForegroundColor Green
Read-Host "按 Enter 关闭"
