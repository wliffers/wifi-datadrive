# Flashes one of the firmware projects onto the dongle and finds the right port by itself.
#
# Port selection (VID 303A = Espressif):
#   1. 303A:1001 (USB-Serial/JTAG: smoke_test or ROM download mode) -> used directly.
#   2. other 303A port (TinyUSB CDC of printdrive, PID 4003/4002)     -> "1200 baud touch",
#      device restarts into download mode, then wait up to 15 s for 303A:1001.
#   3. nothing found -> ask for manual download mode (BOOT button), wait up to 60 s.
# Then: python -m esptool ... write_flash "@flash_args" inside firmware\<project>\build.
#
# Usage:
#   .\tools\flash.ps1 -Project printdrive
#   .\tools\flash.ps1 -Project printdrive -Build -Monitor
#   .\tools\flash.ps1 -Project smoke_test -Port COM4
#
# ESP-IDF location can be overridden with $env:PD_IDF_PATH.
param(
    [Parameter(Mandatory = $true, Position = 0)][ValidateSet('smoke_test', 'printdrive')][string]$Project,
    [string]$Port,
    [switch]$Build,
    [switch]$Monitor
)
$ErrorActionPreference = 'Stop'

$root = Split-Path $PSScriptRoot -Parent
$buildDir = Join-Path $root "firmware\$Project\build"
$idfPath = if ($env:PD_IDF_PATH) { $env:PD_IDF_PATH } else { Join-Path $env:USERPROFILE 'esp\esp-idf-v5.5.6' }

function Fail([string]$msg) {
    Write-Host "FEHLER: $msg" -ForegroundColor Red
    exit 1
}

# Returns all present COM ports as objects with Port, Vid, Pid, Name.
function Get-ComPorts {
    $result = @()
    $devs = @(Get-CimInstance -ClassName Win32_PnPEntity -Filter "Name LIKE '%(COM%'" -ErrorAction SilentlyContinue)
    foreach ($d in $devs) {
        if ($d.Name -notmatch '\((COM\d+)\)') { continue }
        $com = $Matches[1]
        $ids = @($d.PNPDeviceID) + @($d.HardwareID)
        $vid = ''
        $pid_ = ''
        foreach ($id in $ids) {
            if ($id -and ($id -match 'VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})')) {
                $vid = $Matches[1].ToUpper()
                $pid_ = $Matches[2].ToUpper()
                break
            }
        }
        $result += [pscustomobject]@{ Port = $com; Vid = $vid; Pid = $pid_; Name = $d.Name }
    }
    return $result
}

function Find-JtagPort {
    $p = @(Get-ComPorts | Where-Object { $_.Vid -eq '303A' -and $_.Pid -eq '1001' })
    if ($p.Count -gt 1) {
        Write-Host "Hinweis: mehrere 303A:1001-Ports gefunden ($(($p | ForEach-Object { $_.Port }) -join ', ')), nehme $($p[0].Port)." -ForegroundColor Yellow
    }
    if ($p.Count -gt 0) { return $p[0].Port }
    return $null
}

function Wait-JtagPort([int]$seconds) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        $p = Find-JtagPort
        if ($p) { return $p }
        Start-Sleep -Milliseconds 500
    }
    return $null
}

function Invoke-Touch1200([string]$com) {
    Write-Host "1200-Baud-Touch auf $com (Neustart in den Download-Modus) ..."
    $sp = New-Object System.IO.Ports.SerialPort $com, 1200
    $sp.DtrEnable = $true
    try {
        $sp.Open()
        Start-Sleep -Milliseconds 200
    } catch {
        Write-Host "Warnung: $com konnte nicht geöffnet werden: $($_.Exception.Message)" -ForegroundColor Yellow
        Write-Host "         (Port evtl. von einem anderen Programm belegt, z. B. Monitor - bitte schließen.)" -ForegroundColor Yellow
    } finally {
        # Closing drops DTR -> the firmware restarts into ROM download mode.
        try { $sp.Close() } catch { }
        $sp.Dispose()
    }
}

# ---------------------------------------------------------------- build
if ($Build) {
    Write-Host "== Build $Project =="
    & (Join-Path $PSScriptRoot 'idf.ps1') $Project build
    if ($LASTEXITCODE -ne 0) { Fail "Build fehlgeschlagen (Exit-Code $LASTEXITCODE)." }
}

$flashArgs = Join-Path $buildDir 'flash_args'
if (-not (Test-Path $flashArgs)) {
    Fail "$flashArgs fehlt. Erst bauen: .\tools\flash.ps1 -Project $Project -Build"
}
if (-not (Test-Path (Join-Path $idfPath 'export.ps1'))) {
    Fail "ESP-IDF nicht gefunden unter $idfPath (Override: `$env:PD_IDF_PATH)."
}

# ---------------------------------------------------------------- port
$target = $null
$ports = @(Get-ComPorts)
if ($Port) {
    $Port = $Port.ToUpper()
    $info = @($ports | Where-Object { $_.Port -eq $Port })
    if ($info.Count -gt 0 -and $info[0].Vid -eq '303A' -and $info[0].Pid -ne '1001') {
        Write-Host "$Port ist ein TinyUSB-CDC-Port (303A:$($info[0].Pid))."
        Invoke-Touch1200 $Port
        Write-Host "Warte bis zu 15 s auf den Download-Modus-Port (303A:1001) ..."
        $target = Wait-JtagPort 15
        if (-not $target) { Fail "Kein 303A:1001-Port nach dem 1200-Baud-Touch erschienen. BOOT-Taste gedrückt halten, Dongle einstecken, loslassen - dann erneut versuchen." }
    } else {
        if ($info.Count -eq 0) {
            Write-Host "Hinweis: $Port ist derzeit nicht vorhanden, versuche es trotzdem." -ForegroundColor Yellow
        }
        $target = $Port
    }
} else {
    $target = Find-JtagPort
    if ($target) {
        Write-Host "USB-Serial/JTAG-Port gefunden: $target (303A:1001)."
    } else {
        $cdc = @($ports | Where-Object { $_.Vid -eq '303A' -and $_.Pid -ne '1001' })
        if ($cdc.Count -gt 0) {
            Write-Host "TinyUSB-CDC-Port gefunden: $($cdc[0].Port) (303A:$($cdc[0].Pid))."
            Invoke-Touch1200 $cdc[0].Port
            Write-Host "Warte bis zu 15 s auf den Download-Modus-Port (303A:1001) ..."
            $target = Wait-JtagPort 15
            if (-not $target) {
                Write-Host "Kein Download-Modus-Port erschienen." -ForegroundColor Yellow
            }
        }
        if (-not $target) {
            Write-Host ""
            Write-Host "Kein Dongle im Download-Modus gefunden." -ForegroundColor Yellow
            Write-Host ">>> BOOT-Taste gedrückt halten, Dongle einstecken, loslassen <<<" -ForegroundColor Cyan
            Write-Host "Warte bis zu 60 s auf 303A:1001 ..."
            $target = Wait-JtagPort 60
            if (-not $target) { Fail "Kein Dongle im Download-Modus gefunden (Timeout 60 s)." }
            Write-Host "Download-Modus-Port gefunden: $target"
        }
    }
}

# ---------------------------------------------------------------- flash
# export.ps1 prints progress to stderr; that must not abort the script.
$ErrorActionPreference = 'Continue'
. (Join-Path $idfPath 'export.ps1') 2>&1 | Out-Null

Write-Host "== Flashe $Project über $target =="
Push-Location $buildDir
try {
    python -m esptool --chip esp32s3 -p $target -b 460800 --before default_reset --after hard_reset write_flash "@flash_args"
    $rc = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($rc -ne 0) { Fail "esptool fehlgeschlagen (Exit-Code $rc)." }
Write-Host "Flashen erfolgreich." -ForegroundColor Green

# ---------------------------------------------------------------- monitor
if ($Monitor) {
    # Let the device reboot / re-enumerate; monitor.py picks the port itself and reconnects.
    Start-Sleep -Seconds 2
    python (Join-Path $PSScriptRoot 'monitor.py')
    exit $LASTEXITCODE
}
exit 0
