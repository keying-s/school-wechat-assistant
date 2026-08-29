$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $projectDir ".venv\Scripts\python.exe"
$envFile = Join-Path $projectDir "config\.env.school"
$envExample = Join-Path $projectDir "config\.env.school.example"

if (-not (Get-Command py.exe -ErrorAction SilentlyContinue)) {
    throw "未找到 py.exe。请先安装 64 位 Python 3.11。"
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    & py.exe -3.11 -m venv (Join-Path $projectDir ".venv")
}
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r (Join-Path $projectDir "requirements.txt")

if (-not (Test-Path -LiteralPath $envFile)) {
    Copy-Item -LiteralPath $envExample -Destination $envFile
    Write-Host "已创建 config\.env.school，请填入你自己的 DeepSeek API key。"
}

Write-Host "安装完成。运行：.\.venv\Scripts\python.exe app.py"
