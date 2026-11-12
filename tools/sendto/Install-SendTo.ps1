<#
.SYNOPSIS
  Richtet "Senden an > WiFi DataDrive" ein (oder entfernt es mit -Uninstall).
#>
[CmdletBinding(PositionalBinding = $false)]
param(
    [switch]$Uninstall,
    # --- versteckte Parameter (Tests) ---
    [string]$SendToDir,
    [string]$ConfigDir,
    [switch]$NoSetup,
    [switch]$AssumeYes
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrEmpty($SendToDir)) { $SendToDir = [Environment]::GetFolderPath('SendTo') }
if ([string]::IsNullOrEmpty($ConfigDir)) { $ConfigDir = Join-Path $env:APPDATA 'WiFiDataDrive' }
$lnk = Join-Path $SendToDir 'WiFi DataDrive.lnk'
$sendScript = Join-Path $PSScriptRoot 'Send-DataDrive.ps1'
$psExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'

function Ask-YesNo([string]$q) {
    if ($AssumeYes) { Write-Host ($q + ' j (automatisch)'); return $true }
    while ($true) {
        $a = (Read-Host $q).Trim().ToLower()
        if ($a -in @('j', 'ja', 'y')) { return $true }
        if ($a -in @('n', 'nein')) { return $false }
    }
}

if ($Uninstall) {
    if (Test-Path -LiteralPath $lnk) {
        Remove-Item -LiteralPath $lnk -Force
        Write-Host 'Die Verknüpfung "WiFi DataDrive" wurde aus "Senden an" entfernt.' -ForegroundColor Green
    }
    else {
        Write-Host 'Es gab keine Verknüpfung "WiFi DataDrive" in "Senden an".'
    }
    if (Test-Path -LiteralPath $ConfigDir) {
        if (Ask-YesNo 'Auch die gespeicherten Einstellungen (Adresse, Benutzer, Passwort) löschen? (j/n)') {
            Remove-Item -LiteralPath $ConfigDir -Recurse -Force
            Write-Host 'Einstellungen gelöscht.' -ForegroundColor Green
        }
        else {
            Write-Host ('Einstellungen bleiben erhalten: ' + $ConfigDir)
        }
    }
    exit 0
}

if (-not (Test-Path -LiteralPath $sendScript)) {
    Write-Host ('Send-DataDrive.ps1 nicht gefunden neben diesem Skript: ' + $sendScript) -ForegroundColor Red
    exit 1
}
if (-not (Test-Path -LiteralPath $SendToDir)) {
    [void](New-Item -ItemType Directory -Path $SendToDir -Force)
}

$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($lnk)
$sc.TargetPath = $psExe
$sc.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $sendScript + '"'
$sc.WorkingDirectory = $PSScriptRoot
$sc.WindowStyle = 1
$sc.Description = 'Dateien an den WLAN-USB-Stick WiFi DataDrive senden'
$sc.IconLocation = (Join-Path $env:SystemRoot 'System32\shell32.dll') + ',7'
$sc.Save()
[void][Runtime.InteropServices.Marshal]::ReleaseComObject($shell)
Write-Host ('Verknüpfung angelegt: ' + $lnk) -ForegroundColor Green

if (-not $NoSetup) {
    Write-Host ''
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $sendScript, '-Setup', '-NoPause')
    if ($PSBoundParameters.ContainsKey('ConfigDir')) { $argList += @('-ConfigDir', $ConfigDir) }
    & $psExe @argList
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'Die Einrichtung wurde nicht abgeschlossen. Sie startet beim ersten Senden erneut.' -ForegroundColor Yellow
    }
}
Write-Host ''
Write-Host 'Fertig. Dateien im Explorer markieren, rechte Maustaste, Senden an, WiFi DataDrive.'
