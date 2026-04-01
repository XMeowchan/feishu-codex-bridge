@echo off
setlocal

set "SCRIPT_DIR=%~dp0"

powershell.exe -NoExit -ExecutionPolicy Bypass -Command ^
  "& {" ^
  "  $ErrorActionPreference = 'Stop'" ^
  "  $scriptDir = [System.IO.Path]::GetFullPath('%SCRIPT_DIR%')" ^
  "  Set-Location -LiteralPath $scriptDir" ^
  "  $exitCode = 1" ^
  "  $previousPythonUtf8 = $null" ^
  "" ^
  "  if (-not (Get-Command psmux -ErrorAction SilentlyContinue)) {" ^
  "      Write-Host 'psmux not found, please install first:'" ^
  "      Write-Host '  winget install psmux'" ^
  "  } else {" ^
  "      $pythonCommand = $null" ^
  "      if (Get-Command py -ErrorAction SilentlyContinue) {" ^
  "          $pythonCommand = @('py', '-3')" ^
  "      } elseif (Get-Command python -ErrorAction SilentlyContinue) {" ^
  "          $pythonCommand = @('python')" ^
  "      }" ^
  "" ^
  "      if (-not $pythonCommand) {" ^
  "          Write-Host 'Could not find py or python. Please install Python first.'" ^
  "      } else {" ^
  "          try {" ^
  "              $previousPythonUtf8 = $env:PYTHONUTF8" ^
  "              $env:PYTHONUTF8 = '1'" ^
  "" ^
  "              if ($pythonCommand.Length -gt 1) {" ^
  "                  & $pythonCommand[0] $pythonCommand[1] (Join-Path $scriptDir 'start_bridge.py')" ^
  "              } else {" ^
  "                  & $pythonCommand[0] (Join-Path $scriptDir 'start_bridge.py')" ^
  "              }" ^
  "" ^
  "              $exitCode = $LASTEXITCODE" ^
  "          } catch {" ^
  "              Write-Host ''" ^
  "              Write-Host ('Unexpected error: ' + $_.Exception.Message)" ^
  "              $exitCode = 1" ^
  "          } finally {" ^
  "              if ($null -eq $previousPythonUtf8) {" ^
  "                  Remove-Item Env:PYTHONUTF8 -ErrorAction SilentlyContinue" ^
  "              } else {" ^
  "                  $env:PYTHONUTF8 = $previousPythonUtf8" ^
  "              }" ^
  "          }" ^
  "      }" ^
  "  }" ^
  "" ^
  "  Write-Host ''" ^
  "  if ($exitCode -ne 0) {" ^
  "      Write-Host ('Bridge service exited with code: ' + $exitCode)" ^
  "  } else {" ^
  "      Write-Host 'Bridge service exited.'" ^
  "  }" ^
  "  Write-Host ''" ^
  "  Write-Host 'PowerShell will stay open. Close this window when you are done.'" ^
  "}"
