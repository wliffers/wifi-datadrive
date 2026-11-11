# Runs tools\monitor.py (serial log reader with auto-reconnect) with the ESP-IDF Python env.
#
# Usage:
#   .\tools\monitor.ps1                      # first 303A port, CDC preferred
#   .\tools\monitor.ps1 COM5
#   .\tools\monitor.ps1 --seconds 30 --out log.txt
#
# ESP-IDF location can be overridden with $env:PD_IDF_PATH.
$ErrorActionPreference = 'Stop'

$idfPath = if ($env:PD_IDF_PATH) { $env:PD_IDF_PATH } else { Join-Path $env:USERPROFILE 'esp\esp-idf-v5.5.6' }
if (-not (Test-Path (Join-Path $idfPath 'export.ps1'))) {
    throw "ESP-IDF not found at $idfPath. See docs/04_build_und_flash.md."
}

# export.ps1 prints progress to stderr; that must not abort the script.
$ErrorActionPreference = 'Continue'
. (Join-Path $idfPath 'export.ps1') 2>&1 | Out-Null

python (Join-Path $PSScriptRoot 'monitor.py') @args
exit $LASTEXITCODE
