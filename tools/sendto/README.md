# WiFi DataDrive: "Senden an" für Windows

Rechtsklick auf Dateien oder Ordner im Explorer, **Senden an**, **WiFi DataDrive**: Die Dateien landen auf dem Stick. Es wird nur das in Windows eingebaute Windows PowerShell 5.1 gebraucht, nichts muss installiert werden.

Prüfstand (2026-11-12, Firmware 0.5.0): an einem echten PC mit dem Stick **noch nicht geprüft**.

## Einrichten

1. Ordner `tools\sendto` öffnen (der Pfad darf Leerzeichen enthalten).
2. Rechtsklick auf `Install-SendTo.ps1`, **Mit PowerShell ausführen**. Falls Windows das blockiert, in einer PowerShell:
   `powershell -NoProfile -ExecutionPolicy Bypass -File .\Install-SendTo.ps1`
3. Der Installer legt die Verknüpfung an und startet die Einrichtung: Adresse des Sticks (z. B. `http://datadrive.local` oder die IP wie `192.168.2.152`), Benutzername, Passwort, Zielordner (leer = Hauptverzeichnis), vorhandene Dateien überschreiben ja/nein. Die Anmeldung wird gleich getestet.

Das Skript muss an seinem Platz bleiben, die Verknüpfung zeigt darauf.

## Benutzen

Dateien und/oder Ordner markieren, Rechtsklick, Senden an, WiFi DataDrive. Ordner werden samt Unterordnern übertragen. Ein Fenster zeigt Fortschritt und Geschwindigkeit und am Ende "n gespeichert, n übersprungen, n fehlgeschlagen". Das Fenster bleibt bis Enter offen.

## Einstellungen ändern

`powershell -NoProfile -ExecutionPolicy Bypass -File "<Pfad>\Send-DataDrive.ps1" -Setup`
oder `Install-SendTo.ps1` erneut ausführen. Einmaliger anderer Zielordner: `-TargetFolder "Projekte/Drachen"`. Die Einstellungen liegen in `%APPDATA%\WiFiDataDrive\config.json`.

## Deinstallieren

`Install-SendTo.ps1 -Uninstall` entfernt die Verknüpfung und fragt, ob auch die Einstellungen gelöscht werden sollen.

## Was die Meldungen bedeuten

| Meldung | Bedeutung |
|---|---|
| Das Gerät ist nicht erreichbar | Stick nicht im WLAN, PC in einem anderen WLAN oder falsche Adresse. Bei `datadrive.local` hilft oft die IP-Adresse. |
| Das USB-Gerät nutzt das Laufwerk noch ... Trotzdem trennen? | Drucker oder PC am Stick hat das Laufwerk noch offen. "j" trennt es (unsauberes Trennen möglich), "n" überspringt die Datei. Die Antwort gilt für den ganzen Lauf. |
| Das USB-Gerät schreibt gerade | Das Gerät schreibt auf das Laufwerk; das lässt sich nicht erzwingen. Das Skript wartet bis zu 3 mal 5 Sekunden, dann gilt die Datei als fehlgeschlagen. |
| Die Datei gibt es auf dem Stick schon | Nur wenn "immer überschreiben" aus ist: überschreiben oder überspringen. |
| Benutzername oder Passwort falsch | Passwort neu eingeben (wird gespeichert). |
| Zu viele Fehlversuche | Der Stick sperrt die Anmeldung 30 Sekunden. |
| Nicht genug freier Speicher | Auf dem Stick ist zu wenig Platz. |
| Ungültig / Namensfehler | Der Stick lehnt Namen oder Datei ab (z. B. Name beginnt mit Punkt, Zeichen wie `: * ? " < > |`, leere Datei). |

## Grenzen

- Höchstens 4 GB pro Datei (kleiner als 4 GiB).
- Der PC muss im selben WLAN wie der Stick sein.
- Das Passwort wird nur für den aktuellen Windows-Benutzer geschützt gespeichert (DPAPI), nie im Klartext. Auf einem anderen Konto oder PC muss neu eingerichtet werden.
- Die Verbindung ist HTTP, also unverschlüsselt: im Heimnetz ok, nicht in fremden WLANs.
- **Mit eingeschaltetem HTTPS (seit Firmware 0.5.0, Voreinstellung aus):** Der Stick leitet `http://` auf `https://` um. Die Adresse in der Einrichtung (`-Setup`) muss dann mit `https://` beginnen, und das Zertifikat des Sticks muss in Windows unter „Vertrauenswürdige Stammzertifizierungsstellen“ installiert sein (siehe Haupt-README, Abschnitt „HTTPS einschalten“). Ob das Skript so über `https` sendet: **noch nicht geprüft**.
- Seit Firmware 0.5.0 sind alle Zeitangaben der API echte UTC-Sekunden ([docs/15_api_v5.md](../../docs/15_api_v5.md) Abschnitt 1.3). Ob das Änderungsdatum der gesendeten Dateien richtig ankommt: **noch nicht geprüft**.
- Leere Ordner werden nicht übertragen, versteckte Dateien nicht mitgesendet.
