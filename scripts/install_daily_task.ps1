$ErrorActionPreference = "Stop"

$TaskName = "SHFE Daily Positions Update"
$RunScript = Join-Path $PSScriptRoot "run_positions_daily.ps1"
$TaskCommand = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "' + $RunScript + '"'

schtasks.exe /Create `
    /TN $TaskName `
    /TR $TaskCommand `
    /SC WEEKLY `
    /D MON,TUE,WED,THU,FRI `
    /ST 18:30 `
    /F

if ($LASTEXITCODE -ne 0) {
    throw "Failed to create scheduled task. Exit code: $LASTEXITCODE"
}

Write-Host "Installed scheduled task: $TaskName (Monday-Friday 18:30)"
