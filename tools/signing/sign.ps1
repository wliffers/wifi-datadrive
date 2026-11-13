# Signs a firmware file afterwards ("build unsigned, sign later", e.g. a build from CI or from
# another computer). The usual way is build_signed.ps1, which signs during the build.
#
#   .\tools\signing\sign.ps1 -In firmware\printdrive\build_x\printdrive-unsigned.bin
#   .\tools\signing\sign.ps1 -In printdrive.bin -Out printdrive-0.6.0-signed.bin -KeyFile D:\keys\dd.pem
#
# Only images built WITH CONFIG_PD_OTA_SIGNED make sense: an image without it is accepted once,
# but then accepts any file again. The script reads the sdkconfig next to the input file to
# check this; -Force skips the check (e.g. when there is no sdkconfig next to the file).
param(
    [Parameter(Mandatory = $true)][string]$In,
    [string]$Out,
    [string]$KeyFile,
    [switch]$Force
)
. (Join-Path $PSScriptRoot 'common.ps1')

$inFull = Get-FullPath $In
if (-not (Test-Path $inFull)) { Fail "$inFull nicht gefunden." }

$info = Get-ImageInfo $inFull
if (-not $info.IsEsp32S3) { Fail "$inFull ist keine Firmware für den ESP32-S3." }
if ($info.Signed) { Fail "$inFull ist bereits signiert (Schlüssel-ID $($info.KeyId)). Prüfen mit: .\tools\signing\verify.ps1 -File `"$inFull`"" }

$sdkconfig = Join-Path (Split-Path $inFull -Parent) 'sdkconfig'
if (Test-Path $sdkconfig) {
    if (-not (Select-String -Path $sdkconfig -SimpleMatch -Pattern 'CONFIG_SECURE_SIGNED_ON_UPDATE_NO_SECURE_BOOT=y' -Quiet)) {
        if (-not $Force) {
            Fail ("Laut $sdkconfig wurde diese Firmware OHNE CONFIG_PD_OTA_SIGNED gebaut. Signiert würde sie zwar " +
                  "angenommen, danach nähme das Gerät aber wieder jede Datei an. Mit sdkconfig.signed bauen " +
                  "(build_signed.ps1) - oder -Force, wenn das Absicht ist.")
        }
        Write-Host "Warnung: Firmware ohne CONFIG_PD_OTA_SIGNED - nach dem Einspielen sind Updates wieder ungeprüft." -ForegroundColor Yellow
    }
} elseif (-not $Force) {
    Fail "Keine sdkconfig neben $inFull gefunden, die Build-Einstellungen sind unbekannt. Mit -Force trotzdem signieren."
}

$key = Resolve-SigningKey $KeyFile
if (-not (Test-Path $key)) { Fail "Signierschlüssel $key nicht gefunden (-KeyFile oder `$env:PD_OTA_SIGNING_KEY)." }

if (-not $Out) {
    $base = [System.IO.Path]::GetFileNameWithoutExtension($inFull) -replace '-unsigned$', ''
    $Out = Join-Path (Split-Path $inFull -Parent) "$base-signed.bin"
}
$outFull = Get-FullPath $Out
if ($outFull -eq $inFull) { Fail "Ausgabe und Eingabe dürfen nicht dieselbe Datei sein." }
if ((Test-Path $outFull) -and -not $Force) { Fail "$outFull existiert bereits (-Force zum Überschreiben)." }

Write-Host "== Signiere $inFull =="
$rc = Invoke-Espsecure @('sign_data', '--version', '2', '--keyfile', $key, '--output', $outFull, $inFull)
if ($rc -ne 0) { Fail "espsecure sign_data fehlgeschlagen (Exit-Code $rc)." }

Write-Host "== Prüfe $outFull =="
if (-not (Test-SignedImage $outFull $key)) { Fail "Die signierte Datei besteht die Prüfung nicht. NICHT einspielen." }

$signed = Get-ImageInfo $outFull
Write-Host ""
Write-Host "Signiert: $outFull" -ForegroundColor Green
Write-Host "  Größe:        $($signed.Size) Bytes (Platz im Gerät: 2621440)"
Write-Host "  SHA-256:      $((Get-FileHash $outFull -Algorithm SHA256).Hash.ToLower())"
Write-Host "  Schlüssel-ID: $($signed.KeyId)"
exit 0
