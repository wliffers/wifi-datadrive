# Creates the key pair for signed firmware updates (RSA-3072, Secure Boot V2 scheme).
#
#   .\tools\signing\new_key.ps1                      -> %USERPROFILE%\.datadrive\ota_signing_key.pem
#   .\tools\signing\new_key.ps1 -KeyFile D:\keys\dd.pem
#
# Writes the private key and next to it the public key (<name>.pub.pem). Never overwrites an
# existing key and refuses any location inside the repository. See tools/signing/README.md.
param(
    [string]$KeyFile
)
. (Join-Path $PSScriptRoot 'common.ps1')

$key = Resolve-SigningKey $KeyFile
$pub = Get-PublicKeyPath $key

if (Test-InsideRepo $key) {
    Fail "$key liegt im Repository. Der private Schlüssel gehört NIE ins Repository - bitte einen Ort außerhalb wählen (Standard: $DefaultKeyFile)."
}
if (Test-Path $key) {
    Fail "$key existiert bereits und wird nicht überschrieben. Ein neuer Schlüssel würde bedeuten, dass Geräte mit dem alten Schlüssel keine Web-Updates mehr annehmen."
}
if (Test-Path $pub) {
    Fail "$pub existiert bereits (öffentlicher Schlüssel ohne privaten?). Bitte prüfen und ggf. selbst wegräumen."
}

$dir = Split-Path $key -Parent
if (-not (Test-Path $dir)) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
}

Write-Host "== Erzeuge RSA-3072-Schlüssel für signierte Updates =="
$rc = Invoke-Espsecure @('generate_signing_key', '--version', '2', '--scheme', 'rsa3072', $key)
if ($rc -ne 0 -or -not (Test-Path $key)) { Fail "espsecure generate_signing_key fehlgeschlagen (Exit-Code $rc)." }

# Only the current user may read the private key (best effort; NTFS only).
$ErrorActionPreference = 'Continue'
& icacls $key /inheritance:r /grant:r "$($env:USERNAME):F" 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Hinweis: Zugriffsrechte von $key konnten nicht eingeschränkt werden (icacls Exit-Code $LASTEXITCODE)." -ForegroundColor Yellow
}
$ErrorActionPreference = 'Stop'

$rc = Invoke-Espsecure @('extract_public_key', '--version', '2', '--keyfile', $key, $pub)
if ($rc -ne 0) { Write-Host "Hinweis: öffentlicher Schlüssel konnte nicht geschrieben werden (Exit-Code $rc)." -ForegroundColor Yellow }

$keyId = Get-KeyIdFromPem $key

Write-Host ""
Write-Host "Privater Schlüssel:     $key" -ForegroundColor Green
if (Test-Path $pub) { Write-Host "Öffentlicher Schlüssel: $pub" }
Write-Host "Schlüssel-ID:           $keyId   (zeigt das Gerät unter /api/status -> ota.key_id)"
Write-Host ""
Write-Host "JETZT SICHERN:" -ForegroundColor Yellow
Write-Host "  Den privaten Schlüssel an mindestens einem zweiten Ort aufbewahren (USB-Stick im Schrank,"
Write-Host "  Passwort-Manager). Geht er verloren, nehmen Geräte mit diesem Schlüssel KEINE Web-Updates"
Write-Host "  mehr an - sie lassen sich dann nur noch per USB neu flashen."
Write-Host "  Niemals ins Repository, in eine Cloud-Freigabe oder in einen Chat kopieren."
Write-Host ""
Write-Host "Nächster Schritt: .\tools\signing\build_signed.ps1"
exit 0
