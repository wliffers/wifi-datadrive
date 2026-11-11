# Runs idf.py for one of the firmware projects with the ESP-IDF environment loaded.
#
# Usage (from any directory):
#   .\tools\idf.ps1 smoke_test build
#   .\tools\idf.ps1 smoke_test -p COM4 flash monitor
#   .\tools\idf.ps1 printdrive build
#
# ESP-IDF location can be overridden with $env:PD_IDF_PATH.
param(
    [Parameter(Mandatory = $true, Position = 0)][ValidateSet('smoke_test', 'printdrive')][string]$Project,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$IdfArgs
)
$ErrorActionPreference = 'Stop'

$idfPath = if ($env:PD_IDF_PATH) { $env:PD_IDF_PATH } else { Join-Path $env:USERPROFILE 'esp\esp-idf-v5.5.6' }
if (-not (Test-Path (Join-Path $idfPath 'export.ps1'))) {
    throw "ESP-IDF not found at $idfPath. See docs/04_build_und_flash.md."
}

$projectDir = Join-Path (Split-Path $PSScriptRoot -Parent) "firmware\$Project"
# export.ps1 prints progress to stderr; that must not abort the script.
$ErrorActionPreference = 'Continue'
. (Join-Path $idfPath 'export.ps1') 2>&1 | Out-Null

Push-Location $projectDir
try {
    idf.py @IdfArgs
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
