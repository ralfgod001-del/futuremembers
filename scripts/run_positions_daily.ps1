$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$OutputDir = Join-Path $Root "output\shfe_system"
$LogPath = Join-Path $OutputDir "daily_update.log"
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

$Python = "python"
if (Test-Path ".\.venv\Scripts\python.exe") {
    $Python = ".\.venv\Scripts\python.exe"
}

& $Python -m futures_positions daily `
    --db "data\shfe_positions.sqlite" `
    --start-date "2024-05-20" `
    --dashboard "output\shfe_system\index.html" *>> $LogPath

if ($LASTEXITCODE -ne 0) {
    throw "Daily positions update failed. Exit code: $LASTEXITCODE"
}
