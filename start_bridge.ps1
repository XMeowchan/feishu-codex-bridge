$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

if (-not (Get-Command psmux -ErrorAction SilentlyContinue)) {
    Write-Host "psmux not found，please install first："
    Write-Host "  winget install psmux"
    exit 1
}

$pythonCommand = $null
if (Get-Command py -ErrorAction SilentlyContinue) {
    $pythonCommand = @("py", "-3")
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $pythonCommand = @("python")
}

if (-not $pythonCommand) {
    Write-Host "Could not find py or python. Please install Python first."
    exit 1
}

$previousPythonUtf8 = $env:PYTHONUTF8
$env:PYTHONUTF8 = "1"

try {
    if ($pythonCommand.Length -gt 1) {
        & $pythonCommand[0] @($pythonCommand[1..($pythonCommand.Length - 1)]) (Join-Path $scriptDir "start_bridge.py")
    } else {
        & $pythonCommand[0] (Join-Path $scriptDir "start_bridge.py")
    }
    $exitCode = $LASTEXITCODE
} finally {
    if ($null -eq $previousPythonUtf8) {
        Remove-Item Env:PYTHONUTF8 -ErrorAction SilentlyContinue
    } else {
        $env:PYTHONUTF8 = $previousPythonUtf8
    }
}

Write-Host ""
if ($exitCode -ne 0) {
    Write-Host "Bridge service exited with code: $exitCode"
} else {
    Write-Host "Bridge service exited."
}

Write-Host ""
Read-Host "Press Enter to close this window"

exit $exitCode
