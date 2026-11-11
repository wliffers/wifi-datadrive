# Reads the complete 16 MB flash of the dongle into backup\ (read-only operation).
# Only works while the dongle runs firmware with the USB-Serial/JTAG console
# (factory firmware, smoke_test) or is in download mode.
#
# Restore later with:
#   python -m esptool --chip esp32s3 -p COM4 write_flash 0x0 backup\<file>.bin
#
# Usage: .\tools\backup_flash.ps1 -Port COM4
param([string]$Port = 'COM4')
$ErrorActionPreference = 'Continue'

$idfPath = if ($env:PD_IDF_PATH) { $env:PD_IDF_PATH } else { Join-Path $env:USERPROFILE 'esp\esp-idf-v5.5.6' }
. (Join-Path $idfPath 'export.ps1') 2>&1 | Out-Null

$root = Split-Path $PSScriptRoot -Parent
$dir = Join-Path $root 'backup'
New-Item -ItemType Directory -Force $dir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$file = Join-Path $dir "flash_backup_$stamp.bin"

python -m esptool --chip esp32s3 -p $Port flash_id
python (Join-Path $PSScriptRoot 'backup_flash.py') $Port "$file"
if ($LASTEXITCODE -eq 0) {
    $hash = (Get-FileHash $file -Algorithm SHA256).Hash
    "$hash  $(Split-Path $file -Leaf)" | Out-File -Encoding ascii -Append (Join-Path $dir 'SHA256SUMS.txt')
    Write-Host "Backup written: $file"
    Write-Host "SHA256: $hash"
}
exit $LASTEXITCODE
