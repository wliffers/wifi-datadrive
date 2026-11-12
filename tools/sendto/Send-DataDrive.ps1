<#
.SYNOPSIS
  Sendet Dateien und Ordner an den WLAN-USB-Stick "WiFi DataDrive" (Windows PowerShell 5.1).

.DESCRIPTION
  Wird normalerweise ueber "Senden an" im Explorer gestartet (siehe Install-SendTo.ps1).
  Die Zugangsdaten stehen in %APPDATA%\WiFiDataDrive\config.json, das Passwort geschuetzt
  fuer den aktuellen Windows-Benutzer (DPAPI).

.PARAMETER Path
  Dateien und Ordner (restliche Argumente). Ordner werden samt Unterordnern gesendet.

.PARAMETER TargetFolder
  Zielordner auf dem Stick (Standard: aus der Konfiguration, sonst Hauptverzeichnis).

.PARAMETER Setup
  Einrichtung (Adresse, Benutzer, Passwort) interaktiv durchfuehren.

.PARAMETER NoPause
  Am Ende nicht auf eine Taste warten.
#>
[CmdletBinding(PositionalBinding = $false)]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Path,
    [string]$TargetFolder,
    [switch]$Setup,
    [switch]$NoPause,
    # --- versteckte Parameter (Tests, Automatisierung) ---
    [string]$ConfigDir,
    [switch]$AssumeYes,
    [string]$Address,
    [string]$User,
    [string]$PlainPassword
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$script:ConfigDir = $ConfigDir
if ([string]::IsNullOrEmpty($script:ConfigDir)) {
    $script:ConfigDir = Join-Path $env:APPDATA 'WiFiDataDrive'
}
$script:ConfigFile = Join-Path $script:ConfigDir 'config.json'
$script:Config = $null
$script:Password = $null
$script:ForceOk = $false
$script:TookOver = $false
$script:Abort = $false
$script:EndOfInput = $false
$script:DirCache = @{}
$script:MaxFile = [int64]4294967296
$script:Epoch = New-Object DateTime 1970, 1, 1, 0, 0, 0, ([DateTimeKind]::Utc)

# ---------------------------------------------------------------- Hilfsfunktionen

function Write-Info($text) { Write-Host $text }
function Write-Good($text) { Write-Host $text -ForegroundColor Green }
function Write-Warn($text) { Write-Host $text -ForegroundColor Yellow }
function Write-Bad($text) { Write-Host $text -ForegroundColor Red }

function Read-Line([string]$prompt) {
    # Read-Host, aber ohne Absturz bei Ende der Eingabe (z. B. umgeleitete Eingabe)
    $v = Read-Host $prompt
    if ($null -eq $v) { $script:EndOfInput = $true; return '' }
    return [string]$v
}

function Ask-YesNo([string]$question) {
    if ($AssumeYes) {
        Write-Info ($question + ' j (automatisch)')
        return $true
    }
    while ($true) {
        $a = (Read-Line $question).Trim().ToLower()
        if ($a -eq 'j' -or $a -eq 'ja' -or $a -eq 'y') { return $true }
        if ($a -eq 'n' -or $a -eq 'nein') { return $false }
        if ($script:EndOfInput) { return $false }
    }
}

function ConvertTo-PlainText([System.Security.SecureString]$secure) {
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

function Normalize-Address([string]$a) {
    $a = $a.Trim()
    if ($a -eq '') { return '' }
    if ($a -notmatch '^[a-zA-Z][a-zA-Z0-9+.-]*://') { $a = 'http://' + $a }
    return $a.TrimEnd('/')
}

function Normalize-Folder([string]$f) {
    if ($null -eq $f) { return '' }
    $f = $f.Trim().Replace('\', '/')
    return $f.Trim('/')
}

function Format-Size([double]$bytes) {
    if ($bytes -ge 1GB) { return ('{0:N2} GB' -f ($bytes / 1GB)) }
    if ($bytes -ge 1MB) { return ('{0:N1} MB' -f ($bytes / 1MB)) }
    if ($bytes -ge 1KB) { return ('{0:N0} KB' -f ($bytes / 1KB)) }
    return ('{0:N0} Byte' -f $bytes)
}

function Get-LocalSeconds([datetime]$local) {
    # Ortszeit als Sekunden seit 1970 (ohne Zeitzonenumrechnung), wie im API-Vertrag
    $u = [DateTime]::SpecifyKind($local, [DateTimeKind]::Utc)
    return [int64][Math]::Floor(($u - $script:Epoch).TotalSeconds)
}

function Escape-Path([string]$p) {
    return ([Uri]::EscapeDataString($p)).Replace('%2F', '/')
}

# ---------------------------------------------------------------- Konfiguration

function Load-Config {
    if (-not (Test-Path -LiteralPath $script:ConfigFile)) { return $false }
    try {
        $raw = Get-Content -LiteralPath $script:ConfigFile -Raw -Encoding UTF8
        $c = $raw | ConvertFrom-Json
        if ([string]::IsNullOrEmpty($c.address) -or [string]::IsNullOrEmpty($c.user) -or [string]::IsNullOrEmpty($c.passwordProtected)) { return $false }
        $sec = ConvertTo-SecureString -String $c.passwordProtected
        $script:Password = ConvertTo-PlainText $sec
        $tf = ''
        if ($c.PSObject.Properties['targetFolder']) { $tf = [string]$c.targetFolder }
        $ow = $true
        if ($c.PSObject.Properties['overwrite']) { $ow = [bool]$c.overwrite }
        $script:Config = [pscustomobject]@{
            address = [string]$c.address; user = [string]$c.user
            targetFolder = $tf; overwrite = $ow
        }
        return $true
    }
    catch {
        Write-Warn ('Die Konfiguration konnte nicht gelesen werden (' + $_.Exception.Message + '). Sie wird neu eingerichtet.')
        return $false
    }
}

function Save-Config {
    if (-not (Test-Path -LiteralPath $script:ConfigDir)) {
        [void](New-Item -ItemType Directory -Path $script:ConfigDir -Force)
    }
    $sec = ConvertTo-SecureString -String $script:Password -AsPlainText -Force
    $obj = [ordered]@{
        address           = $script:Config.address
        user              = $script:Config.user
        passwordProtected = (ConvertFrom-SecureString -SecureString $sec)
        targetFolder      = $script:Config.targetFolder
        overwrite         = $script:Config.overwrite
    }
    $json = $obj | ConvertTo-Json
    [IO.File]::WriteAllText($script:ConfigFile, $json, (New-Object Text.UTF8Encoding($true)))
}

# ---------------------------------------------------------------- HTTP

function New-AuthHeader {
    $bytes = [Text.Encoding]::UTF8.GetBytes($script:Config.user + ':' + $script:Password)
    return 'Basic ' + [Convert]::ToBase64String($bytes)
}

function Read-Response($resp) {
    $status = [int]$resp.StatusCode
    $text = ''
    try {
        $s = $resp.GetResponseStream()
        $sr = New-Object IO.StreamReader($s, [Text.Encoding]::UTF8)
        $text = $sr.ReadToEnd()
        $sr.Close()
    }
    catch { }
    $resp.Close()
    $json = $null
    try { if ($text.Trim().StartsWith('{')) { $json = $text | ConvertFrom-Json } } catch { }
    $code = ''
    $err = ''
    if ($null -ne $json) {
        if ($json.PSObject.Properties['code']) { $code = [string]$json.code }
        if ($json.PSObject.Properties['error']) { $err = [string]$json.error }
        if ($code -eq '' -and $json.PSObject.Properties['ok'] -and $json.ok -eq $true) { $code = 'OK' }
    }
    if ($code -eq '' -and $status -ge 200 -and $status -lt 300) { $code = 'OK' }
    if ($code -eq '') { $code = 'HTTP_' + $status }
    return [pscustomobject]@{ Status = $status; Code = $code; Error = $err; Json = $json; Net = $null }
}

function Net-Failure([string]$msg) {
    return [pscustomobject]@{ Status = 0; Code = 'NET'; Error = ''; Json = $null; Net = $msg }
}

function Send-Request([string]$Method, [string]$PathAndQuery, [int]$TimeoutMs = 15000) {
    # kleine Anfragen ohne Body
    try {
        $req = [Net.HttpWebRequest]::Create($script:Config.address + $PathAndQuery)
        $req.Method = $Method
        $req.Proxy = $null
        $req.KeepAlive = $false
        $req.Timeout = $TimeoutMs
        $req.ReadWriteTimeout = $TimeoutMs
        $req.UserAgent = 'WiFiDataDrive-SendTo/1.0'
        $req.Headers['Authorization'] = New-AuthHeader
        if ($Method -eq 'POST') {
            $req.Headers['X-PrintDrive'] = '1'
            $req.ContentLength = 0
        }
        $resp = $req.GetResponse()
        return Read-Response $resp
    }
    catch [System.Net.WebException] {
        if ($null -ne $_.Exception.Response) { return Read-Response $_.Exception.Response }
        return Net-Failure $_.Exception.Message
    }
    catch {
        return Net-Failure $_.Exception.Message
    }
}

function Show-NetHint($r) {
    Write-Bad 'Das Gerät ist nicht erreichbar.'
    if ($r.Net) { Write-Info ('  (' + $r.Net + ')') }
    Write-Info '  - Ist der Stick eingesteckt und mit dem WLAN verbunden?'
    Write-Info '  - Ist dieser PC im selben WLAN wie der Stick?'
    Write-Info ('  - Stimmt die Adresse? Aktuell: ' + $script:Config.address)
    Write-Info '    (Adresse ändern: Einrichtung erneut starten, siehe README)'
}

function Test-Login {
    $r = Send-Request 'GET' '/api/session'
    return $r
}

# ---------------------------------------------------------------- Einrichtung

function Read-PasswordPrompt([string]$prompt) {
    $s = Read-Host $prompt -AsSecureString
    if ($null -eq $s) { $script:EndOfInput = $true; return '' }
    return (ConvertTo-PlainText $s)
}

function Invoke-Setup {
    Write-Info ''
    Write-Info '=== WiFi DataDrive: Einrichtung ==='
    $cur = $script:Config
    $defAddr = 'http://datadrive.local'
    $defUser = 'admin'
    $defFolder = ''
    $defOver = $true
    if ($null -ne $cur) {
        $defAddr = $cur.address; $defUser = $cur.user
        $defFolder = $cur.targetFolder; $defOver = $cur.overwrite
    }
    $nonInteractive = (-not [string]::IsNullOrEmpty($Address))
    while ($true) {
        if ($nonInteractive) {
            $addr = Normalize-Address $Address
            $usr = $User; if ([string]::IsNullOrEmpty($usr)) { $usr = $defUser }
            $pw = $PlainPassword
            $folder = Normalize-Folder $TargetFolder
            $over = $defOver
        }
        else {
            $a = Read-Line ('Adresse des Sticks (Name oder IP) [' + $defAddr + ']')
            if ([string]::IsNullOrWhiteSpace($a)) { $a = $defAddr }
            $addr = Normalize-Address $a
            $u = Read-Line ('Benutzername [' + $defUser + ']')
            if ([string]::IsNullOrWhiteSpace($u)) { $u = $defUser }
            $usr = $u.Trim()
            $pw = Read-PasswordPrompt 'Passwort'
            $f = Read-Line ('Zielordner auf dem Stick, leer = Hauptverzeichnis [' + $defFolder + ']')
            if ([string]::IsNullOrWhiteSpace($f)) { $f = $defFolder }
            $folder = Normalize-Folder $f
            $ovText = 'n'; if ($defOver) { $ovText = 'j' }
            $o = Read-Line ('Vorhandene Dateien immer überschreiben? (j/n) [' + $ovText + ']')
            $over = $defOver
            if ($o.Trim().ToLower() -in @('j', 'ja', 'y')) { $over = $true }
            elseif ($o.Trim().ToLower() -in @('n', 'nein')) { $over = $false }
        }
        $script:Config = [pscustomobject]@{ address = $addr; user = $usr; targetFolder = $folder; overwrite = $over }
        $script:Password = $pw
        Write-Info ('Teste die Anmeldung bei ' + $addr + ' ...')
        $r = Test-Login
        if ($r.Code -eq 'OK') {
            Save-Config
            Write-Good ('Anmeldung erfolgreich (Benutzer ' + $usr + '). Die Einstellungen wurden gespeichert.')
            if ($r.Json -and $r.Json.PSObject.Properties['default_password'] -and $r.Json.default_password -eq $true) {
                Write-Warn 'Hinweis: Auf dem Stick gilt noch das Standard-Passwort. Bitte in der Weboberfläche ändern.'
            }
            return $true
        }
        if ($r.Code -eq 'NET') { Show-NetHint $r }
        elseif ($r.Code -eq 'LOCKED') { Write-Bad 'Zu viele Fehlversuche. Der Stick sperrt die Anmeldung für 30 Sekunden.' }
        elseif ($r.Code -eq 'BAD_LOGIN' -or $r.Code -eq 'UNAUTHORIZED') { Write-Bad 'Anmeldung fehlgeschlagen: Benutzername oder Passwort falsch.' }
        else { Write-Bad ('Anmeldung fehlgeschlagen: ' + $r.Code + ' ' + $r.Error) }
        if ($nonInteractive) { return $false }
        if (-not (Ask-YesNo 'Eingaben ändern und erneut versuchen? (j/n)')) {
            if (Ask-YesNo 'Trotzdem speichern? (j/n)') { Save-Config; Write-Warn 'Gespeichert, aber ungetestet.'; return $true }
            return $false
        }
        $defAddr = $addr; $defUser = $usr; $defFolder = $folder; $defOver = $over
    }
}

# ---------------------------------------------------------------- Aktionen am Gerät

function Wait-Seconds([int]$s) { Start-Sleep -Seconds $s }

function Enter-NewPassword {
    # true, wenn ein neues Passwort eingegeben und gespeichert wurde
    if ($AssumeYes) { return $false }
    if (-not (Ask-YesNo 'Passwort neu eingeben? (j/n)')) { return $false }
    $script:Password = Read-PasswordPrompt 'Neues Passwort'
    $r = Test-Login
    if ($r.Code -eq 'OK') {
        Save-Config
        Write-Good 'Anmeldung erfolgreich, Passwort gespeichert.'
        return $true
    }
    Write-Bad 'Auch das neue Passwort wurde nicht akzeptiert.'
    return $false
}

function Take-OverDrive {
    # Laufwerk fuer die Dateiverwaltung holen (noetig fuer Ordner anlegen)
    $tries = 0
    while ($true) {
        $q = '/api/mode?target=app'
        if ($script:ForceOk) { $q += '&force=1' }
        $r = Send-Request 'POST' $q 30000
        if ($r.Code -eq 'OK') { $script:TookOver = $true; return $null }
        if ($r.Code -eq 'NET') { return $r }
        if ($r.Code -eq 'HOST_ACTIVE' -and -not $script:ForceOk) {
            Write-Warn 'Das USB-Gerät nutzt das Laufwerk noch (z. B. der Drucker oder PC, an dem der Stick steckt).'
            if (Ask-YesNo 'Trotzdem trennen? (j/n)') { $script:ForceOk = $true; continue }
            return $r
        }
        if ($r.Code -eq 'HOST_WRITING' -and $tries -lt 3) {
            $tries++
            Write-Warn ('Das USB-Gerät schreibt gerade auf das Laufwerk. Warte 5 Sekunden (Versuch ' + $tries + ' von 3) ...')
            Wait-Seconds 5
            continue
        }
        return $r
    }
}

function Ensure-RemoteDir([string]$dir) {
    # $null = ok, sonst Antwortobjekt mit dem Fehler
    if ([string]::IsNullOrEmpty($dir)) { return $null }
    $cum = ''
    foreach ($seg in $dir.Split('/')) {
        if ($seg -eq '') { continue }
        if ($cum -eq '') { $cum = $seg } else { $cum = $cum + '/' + $seg }
        if ($script:DirCache.ContainsKey($cum)) { continue }
        $r = Send-Request 'POST' ('/api/mkdir?path=' + (Escape-Path $cum)) 30000
        if ($r.Code -eq 'WRONG_OWNER') {
            $t = Take-OverDrive
            if ($null -ne $t) { return $t }
            $r = Send-Request 'POST' ('/api/mkdir?path=' + (Escape-Path $cum)) 30000
        }
        if ($r.Code -eq 'OK' -or $r.Code -eq 'EXISTS') { $script:DirCache[$cum] = $true; continue }
        return $r
    }
    return $null
}

function Push-File($item, [bool]$overwrite, [bool]$force) {
    # Streamt eine Datei zu /api/push; liefert ein Antwortobjekt
    $fi = New-Object IO.FileInfo($item.Local)
    $len = $fi.Length
    $now = Get-LocalSeconds (Get-Date)
    $mt = Get-LocalSeconds $fi.LastWriteTime
    $ow = '0'; if ($overwrite) { $ow = '1' }
    $fo = '0'; if ($force) { $fo = '1' }
    $url = $script:Config.address + '/api/push?path=' + (Escape-Path $item.Remote) +
    '&overwrite=' + $ow + '&force=' + $fo + '&mtime=' + $mt + '&now=' + $now
    $fs = $null
    $rs = $null
    try {
        $req = [Net.HttpWebRequest]::Create($url)
        $req.Method = 'POST'
        $req.Proxy = $null
        $req.KeepAlive = $false
        $req.AllowWriteStreamBuffering = $false
        $req.SendChunked = $false
        $req.ContentLength = $len
        $req.ContentType = 'application/octet-stream'
        $req.Timeout = 900000
        $req.ReadWriteTimeout = 120000
        $req.UserAgent = 'WiFiDataDrive-SendTo/1.0'
        $req.Headers['Authorization'] = New-AuthHeader
        $req.Headers['X-PrintDrive'] = '1'
        $req.ServicePoint.Expect100Continue = $false
        $fs = New-Object IO.FileStream($item.Local, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite, 65536)
        $buf = New-Object byte[] 65536
        $sent = [int64]0
        $sw = [Diagnostics.Stopwatch]::StartNew()
        $lastShow = -1000
        $writeFailed = $null
        try {
            $rs = $req.GetRequestStream()
            while ($sent -lt $len) {
                $n = $fs.Read($buf, 0, $buf.Length)
                if ($n -le 0) { break }
                $rs.Write($buf, 0, $n)
                $sent += $n
                if (($sw.ElapsedMilliseconds - $lastShow) -ge 250) {
                    $lastShow = $sw.ElapsedMilliseconds
                    Show-Progress $sent $len $sw
                }
            }
            $rs.Close()
            $rs = $null
        }
        catch [System.Net.WebException] {
            if ($null -ne $_.Exception.Response) { Write-Host ''; return Read-Response $_.Exception.Response }
            $writeFailed = $_.Exception.Message
        }
        catch [System.IO.IOException] {
            $writeFailed = $_.Exception.Message
        }
        if ($null -ne $writeFailed) {
            Write-Host ''
            # Der Stick hat evtl. schon mit einem Fehler geantwortet und die Verbindung geschlossen
            try {
                $resp = $req.GetResponse()
                return Read-Response $resp
            }
            catch [System.Net.WebException] {
                if ($null -ne $_.Exception.Response) { return Read-Response $_.Exception.Response }
            }
            catch { }
            return Net-Failure $writeFailed
        }
        Show-Progress $sent $len $sw
        Write-Host ''
        $resp = $req.GetResponse()
        return Read-Response $resp
    }
    catch [System.Net.WebException] {
        Write-Host ''
        if ($null -ne $_.Exception.Response) { return Read-Response $_.Exception.Response }
        return Net-Failure $_.Exception.Message
    }
    catch {
        Write-Host ''
        return Net-Failure $_.Exception.Message
    }
    finally {
        if ($null -ne $rs) { try { $rs.Close() } catch { } }
        if ($null -ne $fs) { $fs.Close() }
    }
}

function Show-Progress([int64]$sent, [int64]$total, $sw) {
    $pct = 100
    if ($total -gt 0) { $pct = [int][Math]::Floor(100.0 * $sent / $total) }
    $sec = [Math]::Max($sw.Elapsed.TotalSeconds, 0.001)
    $speed = $sent / $sec
    $line = ('  {0,3}%  {1} von {2}  ({3}/s)' -f $pct, (Format-Size $sent), (Format-Size $total), (Format-Size $speed))
    Write-Host ("`r" + $line.PadRight(70)) -NoNewline
}

# Ergebnis je Datei: 'saved' | 'skipped' | 'failed' (Abbruch ueber $script:Abort)
function Send-OneFile($item) {
    $overwrite = [bool]$script:Config.overwrite
    $writingTries = 0
    if ($item.Size -ge $script:MaxFile) {
        Write-Bad '  Zu groß: Der Stick nimmt höchstens 4 GB pro Datei an (kleiner als 4 GiB).'
        return 'failed'
    }
    while ($true) {
        $r = Push-File $item $overwrite $script:ForceOk
        switch ($r.Code) {
            'OK' {
                Write-Good '  gespeichert.'
                return 'saved'
            }
            'NET' {
                Show-NetHint $r
                $script:Abort = $true
                return 'failed'
            }
            'HOST_ACTIVE' {
                Write-Warn '  Das USB-Gerät nutzt das Laufwerk noch (z. B. der Drucker oder ein PC).'
                if (Ask-YesNo '  Das USB-Gerät nutzt das Laufwerk noch ... Trotzdem trennen? (j/n)') {
                    $script:ForceOk = $true
                    continue
                }
                Write-Info '  übersprungen (Laufwerk nicht getrennt).'
                return 'skipped'
            }
            'HOST_WRITING' {
                if ($writingTries -lt 3) {
                    $writingTries++
                    Write-Warn ('  Das USB-Gerät schreibt gerade auf das Laufwerk (das lässt sich nicht erzwingen). Warte 5 Sekunden (Versuch ' + $writingTries + ' von 3) ...')
                    Wait-Seconds 5
                    continue
                }
                Write-Bad '  Das USB-Gerät schreibt weiterhin. Bitte warten, bis es fertig ist, und es dann noch einmal versuchen.'
                return 'failed'
            }
            'EXISTS' {
                if (Ask-YesNo '  Die Datei gibt es auf dem Stick schon. Überschreiben? (j/n)') {
                    $overwrite = $true
                    continue
                }
                Write-Info '  übersprungen (Datei vorhanden).'
                return 'skipped'
            }
            { $_ -eq 'UNAUTHORIZED' -or $_ -eq 'BAD_LOGIN' } {
                Write-Bad '  Anmeldung abgelehnt: Benutzername oder Passwort stimmen nicht (mehr).'
                if (Enter-NewPassword) { continue }
                $script:Abort = $true
                return 'failed'
            }
            'LOCKED' {
                Write-Bad '  Zu viele Fehlversuche. Der Stick sperrt die Anmeldung 30 Sekunden. Bitte später noch einmal versuchen.'
                $script:Abort = $true
                return 'failed'
            }
            'NO_SPACE' {
                Write-Bad '  Nicht genug freier Speicher auf dem Stick.'
                return 'failed'
            }
            'TOO_LARGE' {
                Write-Bad '  Die Datei ist zu groß (höchstens 4 GB pro Datei).'
                return 'failed'
            }
            'INVALID_FILE' {
                Write-Bad ('  Der Stick lehnt die Datei als ungültig ab. ' + $r.Error)
                return 'failed'
            }
            default {
                $msg = $r.Error; if (-not $msg) { $msg = 'Unbekannter Fehler' }
                Write-Bad ('  Fehlgeschlagen [' + $r.Code + ']: ' + $msg)
                return 'failed'
            }
        }
    }
}

# ---------------------------------------------------------------- Dateiliste

function Join-Remote([string]$a, [string]$b) {
    if ([string]::IsNullOrEmpty($a)) { return $b }
    if ([string]::IsNullOrEmpty($b)) { return $a }
    return $a + '/' + $b
}

function Collect-Items([string[]]$paths, [string]$target) {
    $items = New-Object System.Collections.ArrayList
    $problems = 0
    foreach ($p in $paths) {
        if ([string]::IsNullOrWhiteSpace($p)) { continue }
        if (-not (Test-Path -LiteralPath $p)) {
            Write-Bad ('Nicht gefunden: ' + $p)
            $problems++
            continue
        }
        $it = Get-Item -LiteralPath $p
        if ($it.PSIsContainer) {
            $base = $it.FullName.TrimEnd('\')
            $top = Join-Remote $target $it.Name
            $files = @(Get-ChildItem -LiteralPath $it.FullName -Recurse -File)
            foreach ($f in $files) {
                $rel = $f.FullName.Substring($base.Length).TrimStart('\').Replace('\', '/')
                [void]$items.Add([pscustomobject]@{ Local = $f.FullName; Remote = (Join-Remote $top $rel); Size = [int64]$f.Length; Name = $f.Name })
            }
            if ($files.Count -eq 0) { Write-Warn ('Ordner ist leer, nichts zu senden: ' + $it.Name) }
        }
        else {
            [void]$items.Add([pscustomobject]@{ Local = $it.FullName; Remote = (Join-Remote $target $it.Name); Size = [int64]$it.Length; Name = $it.Name })
        }
    }
    return [pscustomobject]@{ Items = $items; Problems = $problems }
}

# ---------------------------------------------------------------- Hauptablauf

function Finish([int]$code) {
    if (-not $NoPause) {
        Write-Host ''
        Write-Host 'Zum Schließen Enter drücken ...'
        try { [void][Console]::ReadLine() } catch { }
    }
    exit $code
}

function Main {
    Write-Info '=== WiFi DataDrive ==='
    $haveConfig = Load-Config
    if ($Setup -or -not $haveConfig) {
        if (-not $haveConfig -and -not $Setup) { Write-Info 'Noch nicht eingerichtet – die Einrichtung startet jetzt.' }
        if (-not (Invoke-Setup)) {
            Write-Bad 'Einrichtung nicht abgeschlossen.'
            Finish 1
        }
        if ($Setup -and (-not $Path -or $Path.Count -eq 0)) { Finish 0 }
    }
    if (-not $Path -or $Path.Count -eq 0) {
        Write-Warn 'Keine Dateien angegeben. Dateien im Explorer markieren, rechte Maustaste, Senden an, WiFi DataDrive.'
        Finish 0
    }

    $target = $script:Config.targetFolder
    if ($PSBoundParameters.ContainsKey('TargetFolder') -and -not $nonInteractiveSetup) { $target = $TargetFolder }
    $target = Normalize-Folder $target

    $col = Collect-Items $Path $target
    $items = $col.Items
    $saved = 0; $skipped = 0; $failed = [int]$col.Problems
    if ($items.Count -eq 0) {
        Write-Warn 'Es gibt nichts zu senden.'
        Write-Info ''
        Write-Info ('Ergebnis: {0} gespeichert, {1} übersprungen, {2} fehlgeschlagen.' -f $saved, $skipped, $failed)
        if ($failed -gt 0) { Finish 1 } else { Finish 0 }
    }

    # Erreichbarkeit und Anmeldung vorab pruefen
    $pre = $null
    while ($true) {
        $pre = Test-Login
        if ($pre.Code -eq 'OK') { break }
        if ($pre.Code -eq 'NET') { Show-NetHint $pre; Finish 1 }
        if ($pre.Code -eq 'LOCKED') {
            Write-Bad 'Zu viele Fehlversuche. Der Stick sperrt die Anmeldung 30 Sekunden. Bitte später noch einmal versuchen.'
            Finish 1
        }
        if ($pre.Code -eq 'BAD_LOGIN' -or $pre.Code -eq 'UNAUTHORIZED') {
            Write-Bad 'Anmeldung fehlgeschlagen: Benutzername oder Passwort falsch.'
            if (Enter-NewPassword) { continue }
            Finish 1
        }
        Write-Bad ('Der Stick antwortet unerwartet: ' + $pre.Code + ' ' + $pre.Error)
        Finish 1
    }

    $total = $items.Count
    $idx = 0
    Write-Info ('Sende {0} Datei(en) an {1} ...' -f $total, $script:Config.address)
    foreach ($item in $items) {
        $idx++
        if ($script:Abort) { $failed++; continue }
        Write-Info ''
        $shown = $item.Remote
        Write-Info ('[{0}/{1}] {2}  ({3})' -f $idx, $total, $shown, (Format-Size $item.Size))
        $dir = ''
        $slash = $item.Remote.LastIndexOf('/')
        if ($slash -gt 0) { $dir = $item.Remote.Substring(0, $slash) }
        $d = Ensure-RemoteDir $dir
        if ($null -ne $d) {
            if ($d.Code -eq 'NET') { Show-NetHint $d; $script:Abort = $true }
            elseif ($d.Code -eq 'HOST_ACTIVE') { Write-Info '  übersprungen (Laufwerk nicht getrennt).'; $skipped++; continue }
            else {
                $m = $d.Error; if (-not $m) { $m = $d.Code }
                Write-Bad ('  Ordner konnte nicht angelegt werden: ' + $m)
            }
            $failed++
            continue
        }
        $res = Send-OneFile $item
        if ($res -eq 'saved') { $saved++ }
        elseif ($res -eq 'skipped') { $skipped++ }
        else { $failed++ }
    }

    if ($script:TookOver) {
        $b = Send-Request 'POST' '/api/mode?target=usb' 30000
        if ($b.Code -ne 'OK') { Write-Warn 'Hinweis: Das Laufwerk konnte nicht wieder an das USB-Gerät zurückgegeben werden.' }
    }

    Write-Info ''
    $line = 'Ergebnis: {0} gespeichert, {1} übersprungen, {2} fehlgeschlagen.' -f $saved, $skipped, $failed
    if ($failed -gt 0) { Write-Bad $line } else { Write-Good $line }
    if ($failed -gt 0) { Finish 1 }
    Finish 0
}

$nonInteractiveSetup = $false
Main
