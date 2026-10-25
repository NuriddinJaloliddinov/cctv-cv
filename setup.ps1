<#
.SYNOPSIS
  Set up CCTV CV on Windows; all work is done by the cross-platform bootstrap.
.EXAMPLE
  .\setup.ps1
  .\setup.ps1 --check
#>
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Get-Command python -ErrorAction SilentlyContinue
if ($python) {
    & $python.Source (Join-Path $root "bootstrap.py") @args
} else {
    & py -3 (Join-Path $root "bootstrap.py") @args
}
exit $LASTEXITCODE
