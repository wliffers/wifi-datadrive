# Signierte Firmware-Updates – Werkzeuge

Diese Skripte erzeugen den Signierschlüssel, bauen und signieren die Firmware und prüfen
signierte Dateien. Hintergrund, Grenzen des Schutzes und der Umstieg von 0.5.0 stehen in
[docs/17_signierte_updates.md](../../docs/17_signierte_updates.md).

Kurz: Eine Firmware, die mit `CONFIG_PD_OTA_SIGNED` gebaut ist, installiert über die
Weboberfläche nur noch Dateien, die mit **demselben Schlüssel** signiert sind wie sie selbst.
Verfahren: ESP-IDF „Signed App Verification Without Hardware Secure Boot“, RSA-3072
(Secure Boot V2-Signaturblock; das ist das einzige Verfahren, das der ESP32-S3 anbietet).

| Skript | Zweck |
|---|---|
| `new_key.ps1` | Schlüsselpaar einmalig erzeugen (überschreibt nie) |
| `build_signed.ps1` | Release-Build in `firmware\printdrive\build_signed`, beim Bauen signiert und geprüft |
| `sign.ps1` | eine fertig gebaute, unsignierte Datei nachträglich signieren |
| `verify.ps1` | eine Datei prüfen: signiert? mit welchem Schlüssel? passt die Signatur? |
| `common.ps1` | gemeinsame Teile (wird von den anderen geladen, nicht selbst starten) |

Alle Skripte laden die ESP-IDF-Umgebung selbst (`$env:PD_IDF_PATH`, Standard
`%USERPROFILE%\esp\esp-idf-v5.5.6`).

## Der private Schlüssel

- Standardort: `%USERPROFILE%\.datadrive\ota_signing_key.pem` (außerhalb des Repositorys).
  Anderer Ort: Parameter `-KeyFile` oder Umgebungsvariable `PD_OTA_SIGNING_KEY`.
- Er gehört **nie** ins Repository, in eine Cloud-Freigabe, in ein Issue oder in einen Chat.
  Die Skripte verweigern einen Ort innerhalb des Repositorys; `.gitignore` sperrt zusätzlich
  `*.pem` und `*.key`.
- **Sichern!** Wer den Schlüssel verliert, kann den eigenen Geräten keine Web-Updates mehr
  schicken. Sie lassen sich dann nur noch per USB neu flashen (siehe docs/17, „Schlüssel
  verloren“). Eine Kopie gehört an einen zweiten Ort (USB-Stick im Schrank, Passwort-Manager).
- Neben dem privaten liegt der öffentliche Schlüssel `ota_signing_key.pub.pem`. Er ist nicht
  geheim; mit ihm kann jeder eine Release-Datei prüfen (`verify.ps1 -KeyFile …pub.pem`).
- Die **Schlüssel-ID** (8 Hex-Ziffern) zeigen die Skripte und das Gerät unter
  `/api/status` → `ota.key_id`. Stimmen sie überein, passt eine Datei zum Gerät.

## Einmalig: Schlüssel erzeugen

```powershell
.\tools\signing\new_key.ps1
```

Danach die Datei sichern (siehe oben).

## Release bauen (signiert beim Bauen)

```powershell
.\tools\signing\build_signed.ps1
```

- baut immer von Grund auf in `firmware\printdrive\build_signed` aus
  `sdkconfig.defaults` + `sdkconfig.signed` + einer erzeugten Datei
  `build_signed\sdkconfig.signing_key`, die nur den Pfad des Schlüssels enthält
  (`CONFIG_SECURE_BOOT_SIGNING_KEY`, liegt im ignorierten Build-Ordner);
- ESP-IDF signiert `build_signed\printdrive.bin` beim Bauen (`espsecure.py sign_data --version 2`);
  fehlt der Schlüssel, bricht der Build ab, statt eine unsignierte Datei zu erzeugen;
- danach prüft das Skript die Signatur gegen den Schlüssel und legt
  `build_signed\printdrive-<Version>-signed.bin` an.

Einspielen: diese Datei über die Weboberfläche („Gerätesoftware aktualisieren“), oder per USB:

```powershell
.\tools\flash.ps1 printdrive -Signed          # flasht build_signed
.\tools\flash.ps1 printdrive -Signed -Build   # baut vorher mit build_signed.ps1
```

`build_signed\printdrive-unsigned.bin` ist dasselbe Abbild ohne Signatur – **nie einspielen**:
eine Firmware mit `CONFIG_PD_OTA_SIGNED` ohne Signatur bricht beim Start ab (ESP-IDF-Prüfung).
`flash.ps1` weigert sich deshalb, eine solche Datei zu flashen.

Von Hand (ohne Skript) geht derselbe Build so – der Schlüsselpfad muss dann per `menuconfig`
oder eigener dritter Defaults-Datei gesetzt werden:

```powershell
. $env:USERPROFILE\esp\esp-idf-v5.5.6\export.ps1
cd firmware\printdrive
idf.py -B build_signed "-DSDKCONFIG=build_signed/sdkconfig" "-DSDKCONFIG_DEFAULTS=sdkconfig.defaults;sdkconfig.signed;build_signed/sdkconfig.signing_key" build
```

## Ohne Schlüssel bauen (Klone, Forks, CI)

Nichts zu tun: `sdkconfig.defaults` lässt signierte Updates aus. `.\tools\idf.ps1 printdrive build`
und `.\tools\flash.ps1 printdrive -Build` funktionieren wie immer, ohne Schlüssel. Solche
Firmware prüft Updates nicht (wie 0.5.0).

## Nachträglich signieren

Für eine Datei, die mit `sdkconfig.signed`, aber ohne Signieren gebaut wurde
(`CONFIG_SECURE_BOOT_BUILD_SIGNED_BINARIES=n`, z. B. auf einem Rechner ohne Schlüssel):

```powershell
.\tools\signing\sign.ps1 -In <Ordner>\printdrive-unsigned.bin [-Out datei.bin] [-KeyFile …]
```

Das Skript liest die `sdkconfig` neben der Datei und verweigert Abbilder, die ohne
`CONFIG_PD_OTA_SIGNED` gebaut wurden (sie würden angenommen, prüften danach aber nichts mehr),
und bereits signierte Dateien. `-Force` übergeht die `sdkconfig`-Prüfung.

## Prüfen

```powershell
.\tools\signing\verify.ps1 -File <datei.bin> [-KeyFile <privat oder öffentlich .pem>]
```

Exit-Code: `0` passt, `1` passt nicht, `2` unsigniert, `3` signiert, aber kein Schlüssel zum
Vergleich da (nur die Schlüssel-ID wird gezeigt).
