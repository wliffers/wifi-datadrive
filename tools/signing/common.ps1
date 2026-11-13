# Shared part of the scripts in tools/signing (dot-sourced, not run on its own).
# Loads the ESP-IDF environment (python with espsecure) into the calling script.
#
# ESP-IDF location can be overridden with $env:PD_IDF_PATH,
# the key location with -KeyFile or $env:PD_OTA_SIGNING_KEY.
$ErrorActionPreference = 'Stop'

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$DefaultKeyFile = Join-Path $env:USERPROFILE '.datadrive\ota_signing_key.pem'

function Fail([string]$msg) {
    Write-Host "FEHLER: $msg" -ForegroundColor Red
    exit 1
}

# Absolute path, relative paths against the current PowerShell location.
function Get-FullPath([string]$path) {
    return $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($path)
}

function Resolve-SigningKey([string]$keyFile) {
    if (-not $keyFile) {
        $keyFile = if ($env:PD_OTA_SIGNING_KEY) { $env:PD_OTA_SIGNING_KEY } else { $DefaultKeyFile }
    }
    return Get-FullPath $keyFile
}

# Public key written next to the private key by new_key.ps1 (may be passed on freely).
function Get-PublicKeyPath([string]$keyFile) {
    return Join-Path (Split-Path $keyFile -Parent) ([System.IO.Path]::GetFileNameWithoutExtension($keyFile) + '.pub.pem')
}

function Test-InsideRepo([string]$fullPath) {
    $root = $RepoRoot.TrimEnd('\') + '\'
    return $fullPath.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)
}

function Write-Utf8NoBom([string]$path, [string]$text) {
    [System.IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding $false))
}

# Runs "python -m espsecure <args>", shows its output indented, returns the exit code.
function Invoke-Espsecure([string[]]$EsArgs) {
    $ErrorActionPreference = 'Continue'   # espsecure may write notes to stderr
    & python -m espsecure @EsArgs 2>&1 | ForEach-Object { Write-Host "    $_" }
    return $LASTEXITCODE
}

# Key id as shown by the device (/api/status ota.key_id): first 4 bytes (8 hex digits) of the
# SHA-256 of the public key in Secure Boot V2 form (espsecure digest_sbv2_public_key).
function Get-KeyIdFromPem([string]$keyFile) {
    $tmp = [System.IO.Path]::GetTempFileName()
    try {
        $rc = Invoke-Espsecure @('digest_sbv2_public_key', '--keyfile', $keyFile, '--output', $tmp)
        if ($rc -ne 0) { Fail "Schlüssel-ID von $keyFile konnte nicht berechnet werden." }
        $b = [System.IO.File]::ReadAllBytes($tmp)
        return ('{0:x2}{1:x2}{2:x2}{3:x2}' -f $b[0], $b[1], $b[2], $b[3])
    } finally {
        Remove-Item $tmp -ErrorAction SilentlyContinue
    }
}

# Looks at a firmware file: ESP32-S3 app image? Secure Boot V2 signature block (RSA) in the last
# 4 KiB? Key id of block 0 (the only block the device uses). Does not check the signature itself.
function Get-ImageInfo([string]$file) {
    $b = [System.IO.File]::ReadAllBytes($file)
    $info = [pscustomobject]@{ Size = $b.Length; IsEsp32S3 = $false; Signed = $false; KeyId = '' }
    # esp_image_header_t: magic 0xE9 at 0, chip_id (uint16, ESP32-S3 = 9) at 12
    if ($b.Length -ge 24 -and $b[0] -eq 0xE9 -and $b[12] -eq 9 -and $b[13] -eq 0) {
        $info.IsEsp32S3 = $true
    }
    if ($b.Length -ge 8192 -and ($b.Length % 4096) -eq 0) {
        $o = $b.Length - 4096
        # ets_secure_boot_sig_block_t: magic 0xE7, version 2 (RSA), ..., public key at +36 (776 bytes)
        if ($b[$o] -eq 0xE7 -and $b[$o + 1] -eq 2) {
            $sha = [System.Security.Cryptography.SHA256]::Create()
            $d = $sha.ComputeHash($b, $o + 36, 776)
            $info.Signed = $true
            $info.KeyId = ('{0:x2}{1:x2}{2:x2}{3:x2}' -f $d[0], $d[1], $d[2], $d[3])
        }
    }
    return $info
}

# Full check of a signed file against a key (private or public PEM): espsecure verifies the
# signature, and block 0 must carry exactly this key (the device checks only block 0).
function Test-SignedImage([string]$file, [string]$keyFile) {
    $info = Get-ImageInfo $file
    if (-not $info.Signed) {
        Write-Host "  $file trägt keine Signatur." -ForegroundColor Red
        return $false
    }
    $rc = Invoke-Espsecure @('verify_signature', '--version', '2', '--keyfile', $keyFile, $file)
    if ($rc -ne 0) {
        Write-Host "  Die Signatur passt nicht zu $keyFile." -ForegroundColor Red
        return $false
    }
    $keyId = Get-KeyIdFromPem $keyFile
    if ($keyId -ne $info.KeyId) {
        Write-Host "  Block 0 trägt Schlüssel $($info.KeyId), erwartet $keyId (das Gerät prüft nur Block 0)." -ForegroundColor Red
        return $false
    }
    return $true
}

# ---------------------------------------------------------------- ESP-IDF environment
$idfPath = if ($env:PD_IDF_PATH) { $env:PD_IDF_PATH } else { Join-Path $env:USERPROFILE 'esp\esp-idf-v5.5.6' }
if (-not (Test-Path (Join-Path $idfPath 'export.ps1'))) {
    Fail "ESP-IDF nicht gefunden unter $idfPath (Override: `$env:PD_IDF_PATH). Siehe docs/04_build_und_flash.md."
}
# export.ps1 prints progress to stderr; that must not abort the script.
$ErrorActionPreference = 'Continue'
. (Join-Path $idfPath 'export.ps1') 2>&1 | Out-Null
$ErrorActionPreference = 'Stop'
