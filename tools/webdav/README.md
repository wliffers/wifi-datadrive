# DataDrive als Netzlaufwerk (WebDAV)

Ab Firmware 0.5.0 ist das DataDrive unter `http://<Adresse>/dav/` als WebDAV-Freigabe erreichbar. Dateien lassen sich dann im Windows-Explorer oder in einer Datei-App auf dem Handy hineinziehen, ohne Browser.

**Prüfstand (2026-11-12):** Am Gerät nur per API-Test (`tools/api_test.py`, Abschnitt `webdav`)
geprüft: Liste, Hochladen, Verschieben, Löschen, Anmeldung nötig, abgewiesen bei gesperrtem
Laufwerk. Mit echten Programmen (Windows-Explorer, macOS-Finder, Handy-Apps, Linux) ist es
**noch nicht geprüft**. Die Angaben zu den Programmen unten stammen aus Recherche, nicht aus
eigenen Tests. Auch `Connect-DataDrive.ps1` ist an einem echten Windows-PC **noch nicht geprüft**.

Anmeldung: derselbe Benutzername und dasselbe Passwort wie in der Weboberfläche (HTTP-Basic). Ein Konto, das noch das Auslieferungspasswort hat, wird abgewiesen, bis das Passwort in der Weboberfläche geändert ist.

Voraussetzungen:

- Die **Dateiverwaltung** muss aktiv sein. Gehört das Laufwerk gerade dem USB-Gerät (Drucker oder PC), antwortet das DataDrive mit „nicht verfügbar“ (503). WebDAV übernimmt das Laufwerk nie selbst vom Drucker.
- Ein verschlüsseltes Laufwerk muss in der Weboberfläche entsperrt sein (sonst 423).
- WebDAV ist in den Einstellungen eingeschaltet (Einstellungen → Netzwerk; Voreinstellung: ein; Administratoren können es abschalten).
- Es gelten dieselben Regeln wie beim Hochladen im Browser: erlaubte Dateitypen, höchstens 4 GB je Datei, `.3mf` muss eine gültige ZIP-Datei sein, keine leeren Dateien.

## Windows 10 und 11

Laut Recherche (nicht selbst getestet): Windows meldet sich über unverschlüsseltes `http` nur an, wenn in der Registry `BasicAuthLevel = 2` steht. Außerdem überträgt Windows per WebDAV nur Dateien bis 50.000.000 Bytes (etwa 47 MB), solange `FileSizeLimitInBytes` nicht erhöht ist. Beides erledigt das Skript, nach Rückfrage:

```powershell
.\tools\webdav\Connect-DataDrive.ps1 -Server 192.168.1.50
```

Das Skript in einer **normalen** PowerShell starten, nicht „Als Administrator“: Für die Registry fragt es selbst per UAC nach Administratorrechten, das Laufwerk verbindet es dann als normaler Benutzer (sonst sieht der Explorer es nicht). Weitere Aufrufe:

| Aufruf | Wirkung |
|---|---|
| `-Laufwerk Y: -Benutzer anna` | anderer Laufwerksbuchstabe, Benutzer vorgeben |
| `-NurRegistry` | nur die beiden Registry-Werte setzen |
| `-Laufwerk Z: -Trennen` | Laufwerk trennen |
| `-Rueckgaengig` | `BasicAuthLevel` wieder auf 1 (Windows-Voreinstellung) |

**Risiko:** Mit `BasicAuthLevel = 2` schickt Windows Benutzername und Passwort bei *jedem* WebDAV-Server über `http` lesbar mit (nur Base64-kodiert), nicht nur beim DataDrive. Wer im selben Netz mitschneidet, kann sie lesen. Deshalb nur in vertrauenswürdigen Netzen verwenden, für das DataDrive ein eigenes Passwort wählen und die Einstellung mit `-Rueckgaengig` zurücksetzen, wenn sie nicht mehr gebraucht wird. Digest-Anmeldung (ohne Klartext) kann das DataDrive nicht anbieten: Es speichert Passwörter nur als PBKDF2-Prüfwert.

Von Hand geht es so (Eingabeaufforderung als Administrator, danach das Laufwerk in einer normalen):

```bat
reg add HKLM\SYSTEM\CurrentControlSet\Services\WebClient\Parameters /v BasicAuthLevel /t REG_DWORD /d 2 /f
reg add HKLM\SYSTEM\CurrentControlSet\Services\WebClient\Parameters /v FileSizeLimitInBytes /t REG_DWORD /d 4294967295 /f
net stop webclient && net start webclient
net use Z: http://192.168.1.50/dav/ * /user:anna /persistent:yes
```

Im Explorer: „Dieser PC“ → „Netzlaufwerk verbinden“ → Ordner `http://192.168.1.50/dav/`. Windows zeigt Änderungen manchmal erst nach einigen Sekunden an (eigener Zwischenspeicher, F5 hilft).

Kleinigkeiten, die das DataDrive für Windows abfängt: Windows legt eine neue Datei erst leer an und schickt den Inhalt danach; die leere Datei wird nur im Speicher vorgemerkt (5 Minuten) und erst mit dem Inhalt wirklich geschrieben. `desktop.ini` und `Thumbs.db` werden nicht gespeichert. Datum und Attribute, die Windows nachträglich setzen will, werden bestätigt, aber nicht übernommen: Eine Datei trägt die Zeit des Hochladens nach der Uhr des DataDrive. Ist die Uhr unbekannt (nach einem Stromausfall, solange kein Netz da ist), bekommt die Datei kein Datum.

## iPhone und iPad

Die eingebaute App **Dateien** kann sich nur mit SMB-Servern verbinden („Mit Server verbinden“), nicht mit WebDAV. Es braucht eine zusätzliche App, die WebDAV spricht, zum Beispiel **Documents von Readdle**, **Owlfiles** oder **FE File Explorer** (Beispiele aus Recherche; keine davon mit dem DataDrive getestet). Dort eine WebDAV-Verbindung anlegen: Adresse `http://192.168.1.50/dav/`, Benutzer und Passwort wie in der Weboberfläche. Manche dieser Apps blenden die Verbindung auch in der Dateien-App ein.

## Android

Android hat keinen eingebauten WebDAV-Zugang. Apps mit WebDAV (aus Recherche, nicht mit dem DataDrive getestet): **Solid Explorer**, **CX Datei-Explorer**, **Material Files**, **Total Commander** (mit WebDAV-Plugin). Adresse `http://192.168.1.50/dav/`, kein SSL (mit eingeschaltetem HTTPS siehe unten).

## macOS

Nicht mit einem Mac getestet. Finder: „Gehe zu“ → „Mit Server verbinden“ → `http://192.168.1.50/dav/`. **Anzeigen und Herunterladen sollten gehen (Recherche), Hineinkopieren aus dem Finder nicht:** Der Finder schickt Dateien ohne Längenangabe (chunked), und der Webserver des DataDrive kann das nicht annehmen (Antwort 411). Zum Hochladen ein WebDAV-Programm wie **Cyberduck**, **ForkLift** oder **Transmit** verwenden, oder die Weboberfläche. Die `._*`- und `.DS_Store`-Dateien des Finders werden nicht gespeichert.

## Linux

Nicht getestet. Dateimanager (GNOME, KDE): `dav://192.168.1.50/dav/`. Oder `davfs2`: `sudo mount -t davfs http://192.168.1.50/dav/ /mnt/datadrive`.

## Mit eingeschaltetem HTTPS

Seit 0.5.0 lässt sich HTTPS in den Einstellungen einschalten (Voreinstellung aus). Dann leitet das DataDrive `http://` auf `https://` um, und die Adresse lautet `https://192.168.1.50/dav/`. Das Zertifikat des DataDrive muss auf dem Gerät installiert sein (siehe Haupt-README, Abschnitt „HTTPS einschalten“).

**WebDAV über `https` ist noch nicht geprüft**, mit keinem Programm. `Connect-DataDrive.ps1` verbindet laut Quelltext immer `http://<Server>/dav/`; ob Windows der Umleitung folgt, ist ebenfalls noch nicht geprüft. Ob Windows über `https` ohne `BasicAuthLevel = 2` auskommt: noch nicht geprüft.

## Test mit curl

```sh
curl -u anna -X PROPFIND -H "Depth: 1" http://192.168.1.50/dav/
curl -u anna -T Benchy.gcode.3mf http://192.168.1.50/dav/Benchy.gcode.3mf
curl -u anna -o kopie.3mf http://192.168.1.50/dav/Benchy.gcode.3mf
curl -u anna -X MKCOL http://192.168.1.50/dav/Neuer%20Ordner
curl -u anna -X MOVE -H "Destination: /dav/Neuer%20Ordner/Benchy.gcode.3mf" http://192.168.1.50/dav/Benchy.gcode.3mf
curl -u anna -X DELETE http://192.168.1.50/dav/Neuer%20Ordner
```

## Wenn es nicht klappt

| Meldung | Ursache |
|---|---|
| 401, Windows fragt immer wieder nach dem Passwort | `BasicAuthLevel` nicht 2, oder Passwort falsch |
| 403 „WebDAV ist … ausgeschaltet“ | in den Einstellungen des DataDrive einschalten |
| 403 „Passwort ändern“ | Konto hat noch das Auslieferungspasswort |
| 429 | zu viele falsche Passwörter: etwas warten |
| 503 „dem USB-Gerät zugeordnet“ | in der Weboberfläche „Dateiverwaltung aktivieren“ |
| 423 | verschlüsseltes Laufwerk: erst entsperren |
| 0x800700DF „Dateigröße überschreitet …“ (Windows) | `FileSizeLimitInBytes` erhöhen (Skript) |
| 411 (macOS Finder) | Finder lädt chunked hoch: anderes Programm nehmen |
