# Checks a firmware file before it is installed: signed? with which key? does the signature match?
#
#   .\tools\signing\verify.ps1 -File firmware\printdrive\build_signed\printdrive-0.6.0-signed.bin
#   .\tools\signing\verify.ps1 -File printdrive.bin -KeyFile C:\Users\me\.datadrive\ota_signing_key.pub.pem
#
# Without -KeyFile the default private key is used, else its public key (<name>.pub.pem) if only
# that one is present. Compare the key id with /api/status -> ota.key_id of the device.
# Exit code: 0 = signed and matches the key, 1 = does not match / error, 2 = not signed,
#            3 = signed, but no key here to check it against (only the key id is shown).
param(
    [Parameter(Mandatory = $true)][string]$File,
    [string]$KeyFile
)
. (Join-Path $PSScriptRoot 'common.ps1')

$fileFull = Get-FullPath $File
if (-not (Test-Path $fileFull)) { Fail "$fileFull nicht gefunden." }

$info = Get-ImageInfo $fileFull
Write-Host "Datei:        $fileFull"
Write-Host "Größe:        $($info.Size) Bytes (Platz im Gerät: 2621440)"
Write-Host "ESP32-S3:     $(if ($info.IsEsp32S3) { 'ja' } else { 'NEIN' })"
if (-not $info.IsEsp32S3) { Fail "Keine Firmware für den ESP32-S3." }
if (-not $info.Signed) {
    Write-Host "Signatur:     keine" -ForegroundColor Yellow
    Write-Host "Ein Gerät, das signierte Updates verlangt, lehnt diese Datei ab (UNSIGNED)."
    exit 2
}
Write-Host "Schlüssel-ID: $($info.KeyId)"

$key = $null
if ($KeyFile) {
    $key = Get-FullPath $KeyFile
    if (-not (Test-Path $key)) { Fail "$key nicht gefunden." }
} else {
    $priv = Resolve-SigningKey ''
    $pub = Get-PublicKeyPath $priv
    if (Test-Path $priv) { $key = $priv } elseif (Test-Path $pub) { $key = $pub }
}
if (-not $key) {
    Write-Host "Kein Schlüssel zum Prüfen gefunden - nur die Schlüssel-ID oben ist bekannt." -ForegroundColor Yellow
    Write-Host "Mit -KeyFile <öffentlicher oder privater Schlüssel> vollständig prüfen."
    exit 3
}

Write-Host "Prüfe gegen:  $key"
if (-not (Test-SignedImage $fileFull $key)) {
    Write-Host "ERGEBNIS: Signatur passt NICHT zu diesem Schlüssel. Ein Gerät mit diesem Schlüssel lehnt die Datei ab (BAD_SIGNATURE)." -ForegroundColor Red
    exit 1
}
Write-Host "ERGEBNIS: korrekt signiert. Geräte mit Schlüssel-ID $($info.KeyId) nehmen die Datei an." -ForegroundColor Green
exit 0
