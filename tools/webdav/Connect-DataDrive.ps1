# Verbindet die WebDAV-Freigabe des WiFi DataDrive als Netzlaufwerk (Windows 10/11).
#
# Windows schickt Benutzername und Passwort über unverschlüsseltes http nur, wenn in der
# Registry BasicAuthLevel = 2 steht (Voreinstellung 1 = nur über https). Das DataDrive kennt nur
# die Basic-Anmeldung. Dieses Skript
#   1. prüft BasicAuthLevel und FileSizeLimitInBytes (Windows-Grenze 50.000.000 Bytes je Datei),
#   2. ändert sie nach Rückfrage (braucht Administratorrechte, fragt per UAC nach) und startet den
#      Dienst WebClient neu,
#   3. verbindet http://<Server>/dav/ als Laufwerk (Passwort fragt "net use" selbst ab).
#
# RISIKO von BasicAuthLevel = 2: Windows sendet die Anmeldedaten dann bei JEDEM WebDAV-Server über
# http lesbar (nur Base64-kodiert) mit, nicht nur beim DataDrive. Wer im selben Netz mitschneidet,
# kann sie lesen. Nur in vertrauenswürdigen Netzen verwenden, für das DataDrive ein eigenes Passwort
# nehmen und die Einstellung mit -Rueckgaengig wieder zurücksetzen, wenn sie nicht mehr gebraucht wird.
#
# Aufruf (normale PowerShell, NICHT "Als Administrator" - sonst sieht der Explorer das Laufwerk nicht):
#   .\tools\webdav\Connect-DataDrive.ps1 -Server 192.168.1.50
#   .\tools\webdav\Connect-DataDrive.ps1 -Server datadrive.local -Laufwerk Y: -Benutzer anna
#   .\tools\webdav\Connect-DataDrive.ps1 -Laufwerk Z: -Trennen
#   .\tools\webdav\Connect-DataDrive.ps1 -Rueckgaengig
param(
    [Parameter(Position = 0)][string]$Server,
    [string]$Laufwerk = 'Z:',
    [string]$Benutzer,
    [switch]$NurRegistry,   # nur die Registry-Werte setzen, kein Laufwerk verbinden
    [switch]$Trennen,       # Laufwerk trennen
    [switch]$Rueckgaengig   # BasicAuthLevel wieder auf 1 (Windows-Voreinstellung) setzen
)
$ErrorActionPreference = 'Stop'

$regPath = 'HKLM\SYSTEM\CurrentControlSet\Services\WebClient\Parameters'
$psPath = "Registry::HKEY_LOCAL_MACHINE\SYSTEM\CurrentControlSet\Services\WebClient\Parameters"
$maxSize = 4294967295   # größter DWORD-Wert = FAT32-Grenze von 4 GB je Datei

function Fail([string]$msg) {
    Write-Host "FEHLER: $msg" -ForegroundColor Red
    exit 1
}

function Ask([string]$question) {
    $a = Read-Host "$question (j/n)"
    return $a -match '^(j|ja|y|yes)$'
}

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

# Führt PowerShell-Befehle mit Administratorrechten aus (UAC-Abfrage, falls nötig).
function Invoke-Elevated([string[]]$commands) {
    $script = $commands -join "`n"
    if (Test-Admin) {
        Invoke-Expression $script
        return
    }
    Write-Host 'Windows fragt jetzt nach Administratorrechten ...'
    $enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes("`$ErrorActionPreference = 'Stop'`n" + $script + "`nexit 0"))
    try {
        $p = Start-Process -FilePath 'powershell.exe' -Verb RunAs -Wait -PassThru `
            -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', $enc
    } catch {
        Fail 'Administratorrechte wurden nicht erteilt. Es wurde nichts geändert.'
    }
    if ($p.ExitCode -ne 0) {
        Fail "Die Änderung mit Administratorrechten ist fehlgeschlagen (Code $($p.ExitCode))."
    }
}

function Get-WebClientValue([string]$name, [int64]$default) {
    $item = Get-ItemProperty -Path $psPath -Name $name -ErrorAction SilentlyContinue
    if (-not $item) {
        return $default
    }
    $v = [int64]$item.$name
    if ($v -lt 0) {
        $v += 4294967296   # DWORD wird als Int32 gelesen
    }
    return $v
}

# Elevated command that sets one DWORD value; reg.exe takes the decimal value up to 4294967295.
function RegCmd([string]$name, [int64]$value) {
    return "& reg.exe add '$regPath' /v $name /t REG_DWORD /d $value /f | Out-Null; if (`$LASTEXITCODE -ne 0) { throw 'reg.exe: $name' }"
}

$restartCmds = @(
    "if ((Get-Service WebClient).StartType -eq 'Disabled') { Set-Service WebClient -StartupType Manual }",
    'Restart-Service WebClient -Force'
)

# ------------------------------------------------------------------ Laufwerksbuchstabe

if ($Laufwerk -notmatch '^[A-Za-z]:?$') {
    Fail "Ungültiger Laufwerksbuchstabe '$Laufwerk' (Beispiel: Z:)."
}
$Laufwerk = $Laufwerk.Substring(0, 1).ToUpper() + ':'

if ($Trennen) {
    & net.exe use $Laufwerk /delete /y
    exit $LASTEXITCODE
}

if (-not (Get-Service -Name WebClient -ErrorAction SilentlyContinue)) {
    Fail 'Der Windows-Dienst "WebClient" fehlt (auf Windows Server muss die Funktion "WebDAV-Redirector" erst installiert werden).'
}

if ($Rueckgaengig) {
    Invoke-Elevated (@(RegCmd 'BasicAuthLevel' 1) + $restartCmds)
    Write-Host 'BasicAuthLevel ist wieder 1: Basic-Anmeldung nur noch über https.' -ForegroundColor Green
    exit 0
}

# ------------------------------------------------------------------ 1. Registry

$basic = Get-WebClientValue 'BasicAuthLevel' 1
$limit = Get-WebClientValue 'FileSizeLimitInBytes' 50000000
$cmds = @()

if ($basic -lt 2) {
    Write-Host ''
    Write-Host "BasicAuthLevel ist $basic. Damit lehnt Windows die Anmeldung am DataDrive über http ab." -ForegroundColor Yellow
    Write-Host 'Mit BasicAuthLevel = 2 sendet Windows Benutzername und Passwort bei JEDEM WebDAV-Server'
    Write-Host 'über http lesbar mit (nicht nur beim DataDrive). Wer im selben Netz mitschneidet, kann sie lesen.'
    Write-Host 'Nur in vertrauenswürdigen Netzen verwenden; zurücksetzen mit: Connect-DataDrive.ps1 -Rueckgaengig'
    if (Ask 'BasicAuthLevel jetzt auf 2 setzen?') {
        $cmds += RegCmd 'BasicAuthLevel' 2
    } else {
        Write-Host 'Ohne diese Einstellung kann Windows sich über http nicht anmelden.' -ForegroundColor Yellow
        if (-not (Ask 'Trotzdem weitermachen?')) {
            exit 1
        }
    }
}

if ($limit -lt $maxSize) {
    Write-Host ''
    Write-Host ("Windows überträgt per WebDAV höchstens {0:N0} Bytes je Datei (FileSizeLimitInBytes)." -f $limit) -ForegroundColor Yellow
    if (Ask 'Auf 4 GB (Grenze des Dateisystems FAT32) erhöhen?') {
        $cmds += RegCmd 'FileSizeLimitInBytes' $maxSize
    }
}

if ($cmds.Count -gt 0) {
    Invoke-Elevated ($cmds + $restartCmds)
    Write-Host 'Registry geändert, Dienst WebClient neu gestartet.' -ForegroundColor Green
}

if ($NurRegistry) {
    exit 0
}

# ------------------------------------------------------------------ 2. Laufwerk verbinden

if (-not $Server) {
    $Server = Read-Host 'Adresse des DataDrive (IP oder Name, z. B. 192.168.1.50)'
}
$Server = ($Server -replace '^[a-zA-Z]+://', '').TrimEnd('/')
if ($Server -match '/') {
    $Server = $Server.Substring(0, $Server.IndexOf('/'))   # nur der Rechnername, der Pfad ist immer /dav/
}
if ($Server -notmatch '^[A-Za-z0-9.\-]+$') {
    Fail "Ungültige Adresse '$Server'."
}
if (-not $Benutzer) {
    $Benutzer = Read-Host 'Benutzername (wie in der Weboberfläche)'
}
$url = "http://$Server/dav/"

Write-Host ''
Write-Host "Verbinde $Laufwerk mit $url - net use fragt jetzt nach dem Passwort ..."
& net.exe use $Laufwerk $url '*' "/user:$Benutzer" '/persistent:yes'
if ($LASTEXITCODE -ne 0) {
    Write-Host ''
    Write-Host 'Das Verbinden ist fehlgeschlagen. Häufige Ursachen:' -ForegroundColor Yellow
    Write-Host ' - Systemfehler 67 / 53: Adresse falsch, DataDrive nicht im selben Netz, WebDAV in den'
    Write-Host '   Einstellungen des DataDrive ausgeschaltet, oder BasicAuthLevel ist nicht 2.'
    Write-Host ' - Systemfehler 1244 / 86 / 5: Benutzername oder Passwort falsch, oder das Konto hat noch'
    Write-Host '   das Auslieferungspasswort (erst in der Weboberfläche ändern).'
    Write-Host " - Laufwerk $Laufwerk ist schon belegt: mit -Laufwerk einen anderen Buchstaben wählen."
    exit $LASTEXITCODE
}

try {
    (New-Object -ComObject Shell.Application).NameSpace("$Laufwerk\").Self.Name = 'DataDrive'
} catch {
    # nur die Beschriftung im Explorer, nicht wichtig
}
Write-Host "Fertig: Laufwerk $Laufwerk = DataDrive. Dateien lassen sich jetzt im Explorer hineinziehen." -ForegroundColor Green
Write-Host 'Hinweis: Das DataDrive muss dafür die Dateiverwaltung aktiv haben (nicht "USB-Laufwerk bereitstellen").'
