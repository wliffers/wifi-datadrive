# 17 – Signierte Firmware-Updates

Ab Version 0.6.0 kann die Firmware so gebaut werden, dass sie über die Weboberfläche
(`POST /api/ota`) **nur noch Updates installiert, die mit dem Schlüssel des Besitzers signiert
sind**. Dieses Dokument erklärt, was das schützt und was nicht, wie man es einrichtet, wie ein
Release entsteht, was bei einem verlorenen Schlüssel zu tun ist und wie der Umstieg von 0.5.0
läuft. Am Ende stehen die Testschritte.

Werkzeuge: [tools/signing/README.md](../tools/signing/README.md). API-Felder und Fehlercodes:
[16_api_v6.md](16_api_v6.md).

---

## 1. Warum

Eine unabhängige Prüfung hat festgestellt: Wer Admin-Zugang zur Weboberfläche hat, konnte bisher
jede gültige ESP32-S3-Firmware einspielen – auch eine veränderte, die zum Beispiel das Passwort
der Laufwerksverschlüsselung beim nächsten Entsperren mitschreibt. Mit signierten Updates reicht
der Admin-Zugang dafür nicht mehr: Eine neue Firmware muss zusätzlich mit dem privaten Schlüssel
des Besitzers signiert sein, und der liegt nur auf dessen Rechner.

## 2. Was es schützt – und was nicht

**Geschützt:**

- Ein Update über die Weboberfläche oder die API wird nur installiert, wenn es mit dem Schlüssel
  signiert ist, mit dem auch die laufende Firmware signiert ist. Das gilt für jeden, der das Update
  schickt – auch für einen Admin mit richtigem Passwort, für ein gestohlenes Admin-Passwort und für
  jemanden, der im WLAN mithört oder sich dazwischenschaltet.
- Eine abgelehnte Datei ändert nichts am Gerät: Die laufende Firmware bleibt, das Gerät läuft
  einfach weiter (Einzelheiten in Abschnitt 3).

**Nicht geschützt:**

- **USB mit Zugriff auf das Gerät.** Wer den Dongle an einen eigenen Rechner steckt (BOOT-Taste
  beim Einstecken) oder den Rechner kontrolliert, an dem er steckt, kann per USB jede Firmware
  flashen – signiert oder nicht. Die Firmware lässt sich auch ohne Taste in den Download-Modus
  schicken (der „1200-Baud-Touch“ auf dem USB-Log-Port, den `tools\flash.ps1` benutzt). Wer also
  Software auf dem USB-Gastgeber ausführen kann, kann flashen. Am Drucker als Gastgeber ist das in
  der Praxis kein Weg; an einem fremden PC schon.
- **Kein Hardware-Secure-Boot, keine Flash-Verschlüsselung.** Der Bootloader prüft beim Start keine
  Signatur, und der Flash-Inhalt (Einstellungen, Benutzerdatenbank, WLAN-Passwörter) ist per USB
  lesbar. Beides wäre mit eFuses dauerhaft und unumkehrbar – das ist bewusst nicht eingeschaltet.
- **Der Schlüssel steckt in der laufenden Firmware.** Das Gerät vertraut dem öffentlichen
  Schlüssel, der im Signaturblock der gerade laufenden Firmware steht. Wer einmal (per USB) eine
  eigene Firmware aufgespielt hat, bestimmt damit auch, welcher Schlüssel künftig gilt. Eine solche
  Firmware kann im Status auch eine falsche Schlüssel-ID anzeigen.
- **Ältere signierte Versionen.** Jede Datei mit gültiger Signatur wird angenommen, auch eine
  ältere signierte Version (kein Schutz gegen Rückstufen; ESP-IDFs „Anti-Rollback“ bräuchte eFuses).
- **Alles andere, was ein Admin darf** (Einstellungen, Benutzer, Dateien, Wartungsmodus), bleibt
  wie bisher. Signierte Updates verhindern nur das Austauschen der Firmware übers Netz.

## 3. Wie es funktioniert

- Verfahren: ESP-IDF **„Signed App Verification Without Hardware Secure Boot“**
  (`CONFIG_SECURE_SIGNED_APPS_NO_SECURE_BOOT`, `CONFIG_SECURE_SIGNED_ON_UPDATE_NO_SECURE_BOOT`,
  Schema `CONFIG_SECURE_SIGNED_APPS_RSA_SCHEME`). Der ESP32-S3 bietet in ESP-IDF 5.5.6 dafür nur
  **RSA-3072** (RSA-PSS, Secure-Boot-V2-Signaturblock); ECDSA gibt es auf diesem Chip nicht.
  Eingeschaltet wird alles über die Projektoption `CONFIG_PD_OTA_SIGNED` (Menü „DataDrive“).
- Die signierte Datei ist das Firmware-Abbild, aufgefüllt bis kurz vor eine 64-KiB-Grenze, plus ein
  4-KiB-Sektor mit dem Signaturblock. Sie wird dadurch bis zu 68 KiB größer – rund 1,4 MB bei
  2,5 MB Platz pro Firmware-Bereich.
- Beim Update schreibt das Gerät die Datei in den **freien** Firmware-Bereich. Erst
  `esp_ota_end()` prüft am Ende Prüfsumme, Hash und Signatur. Scheitert das, wird der Startbereich
  **nicht** umgeschaltet; die laufende Firmware bleibt unberührt und startet auch nach einem
  Neustart wieder. `esp_ota_set_boot_partition()` prüft vor dem Umschalten ein zweites Mal.
- Geprüft wird nur der **erste** Signaturblock, gegen den öffentlichen Schlüssel im ersten
  Signaturblock der laufenden Firmware.
- Ist eine Firmware mit dieser Option gebaut, aber nicht signiert, bricht sie beim Start ab
  (ESP-IDF: „This app is not signed…“), damit kein Gerät entsteht, das gar keine Updates mehr
  annehmen kann. Über das Web führt das zum Rücksprung auf die alte Firmware (siehe unten), per USB
  geflasht muss neu geflasht werden. Deshalb signiert der Release-Build beim Bauen, und
  `tools\flash.ps1` flasht keine unsignierte Datei aus einem solchen Build.
- **Bootloader:** muss nicht erneuert werden. Mit dem RSA-Verfahren prüft der Bootloader ohne
  Secure Boot keine Signaturen; der vorhandene Bootloader startet auch signierte Abbilder (der
  Signaturblock liegt hinter dem eigentlichen Abbild), und ein Web-Update reicht für den Umstieg. Der Rücksprung bei einem Absturz (`CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE`)
  funktioniert wie bisher: Eine neue Firmware startet „zur Prüfung“ und bestätigt sich nach 15 s
  selbst (`main/ota.c`); startet sie vorher neu, kehrt der Bootloader zur vorherigen zurück.
- Status (`GET /api/status`): `ota.signed_required` (true/false) und `ota.key_id` – die ersten
  8 Hex-Ziffern des SHA-256 des öffentlichen Schlüssels (leer, wenn nicht verlangt). Die
  Skripte in `tools/signing` zeigen dieselbe ID für Schlüssel und Dateien.
- Fehlermeldungen von `POST /api/ota` (nur wenn signierte Updates verlangt sind):
  - `422 UNSIGNED` – „Die Update-Datei ist nicht signiert. Dieses Gerät nimmt nur signierte Updates
    an.“ Dateien, deren Größe kein Vielfaches von 4096 ist, werden schon nach dem Kopf abgewiesen,
    bevor etwas gelöscht oder geschrieben wird.
  - `422 BAD_SIGNATURE` – „Die Update-Datei ist nicht mit dem Schlüssel dieses Geräts signiert.“
    Anderer Schlüssel, oder die Datei wurde nach dem Signieren verändert oder beschädigt. Das
    Geräte-Log nennt beide Schlüssel-IDs.
  - `422 INVALID_FILE` wie bisher, wenn der Kopf kein ESP32-S3-Abbild ist.

## 4. Voreinstellung im öffentlichen Repository: aus

`firmware/printdrive/sdkconfig.defaults` enthält `# CONFIG_PD_OTA_SIGNED is not set`.

Begründung: Das Repository ist öffentlich; wer es klont oder forkt, hat den privaten Schlüssel des
Besitzers nicht. Wäre die Option eingeschaltet, bräche entweder der Build ab (Schlüssel fehlt),
oder – mit nachträglichem Signieren – es entstünde eine unsignierte Firmware, die beim Start
abbricht. Ein frischer Klon muss aber bauen und per USB flashen können, und die CI
(`.github/workflows`) baut ebenfalls ohne Schlüssel. Deshalb:

- **Klone, Forks, CI:** bauen wie bisher, ohne Schlüssel. Diese Firmware prüft Updates nicht
  (wie 0.5.0). Ein Fork kann eigene signierte Builds machen – mit eigenem Schlüssel.
- **Der Besitzer:** baut seine Releases mit dem Overlay `sdkconfig.signed` und seinem Schlüssel
  (`tools\signing\build_signed.ps1`). Nur diese Firmware verlangt signierte Updates.

Dass ein Fork ohne den Schlüssel des Besitzers keine Updates erzeugen kann, die dessen Geräte
annehmen, ist genau der Zweck.

## 5. Einmalige Einrichtung (Besitzer)

1. ESP-IDF 5.5.6 wie in [04_build_und_flash.md](04_build_und_flash.md).
2. Schlüssel erzeugen:
   ```powershell
   .\tools\signing\new_key.ps1
   ```
   Legt `%USERPROFILE%\.datadrive\ota_signing_key.pem` (privat) und `ota_signing_key.pub.pem`
   (öffentlich) an, zeigt die Schlüssel-ID und überschreibt nie einen vorhandenen Schlüssel.
3. **Den privaten Schlüssel sichern** – an einem zweiten Ort, offline (USB-Stick im Schrank,
   Passwort-Manager). Nie ins Repository, in eine Cloud-Freigabe, in ein Issue oder einen Chat.
4. Die Schlüssel-ID notieren (z. B. in den Release-Notizen); der öffentliche Schlüssel darf
   veröffentlicht werden.

## 6. Ein Release machen

1. Version in `firmware/printdrive/CMakeLists.txt` (`PROJECT_VER`) setzen.
2. Signierter Build:
   ```powershell
   .\tools\signing\build_signed.ps1
   ```
   Baut von Grund auf in `firmware\printdrive\build_signed`, signiert beim Bauen, prüft die
   Signatur gegen den Schlüssel und legt `build_signed\printdrive-<Version>-signed.bin` an
   (Ausgabe: Größe, SHA-256, Schlüssel-ID).
3. Optional prüfen (z. B. auf einem anderen Rechner mit dem öffentlichen Schlüssel):
   ```powershell
   .\tools\signing\verify.ps1 -File firmware\printdrive\build_signed\printdrive-0.6.0-signed.bin
   ```
4. Einspielen: über die Weboberfläche („Gerätesoftware aktualisieren“) diese Datei – oder per USB
   `.\tools\flash.ps1 printdrive -Signed` (behält Einstellungen und Dateien).
5. GitHub-Release: Die CI erzeugt einen Entwurf mit **unsignierten** Dateien
   (`.github/workflows/release.yml`). Für Geräte mit signierten Updates im Entwurf ersetzen, unter
   denselben Namen:
   - `…-app.bin` durch `build_signed\printdrive-<Version>-signed.bin`,
   - `…-esp32s3-merged.bin` durch ein Gesamtabbild aus `build_signed`:
     `python .github/scripts/merge_image.py firmware/printdrive/build_signed <ziel>.bin`
     (in der ESP-IDF-Umgebung),
   - `SHA256SUMS.txt` neu berechnen. Erst dann veröffentlichen.

Nachträglich signieren (Datei von einem Rechner ohne Schlüssel, gebaut mit `sdkconfig.signed` und
`CONFIG_SECURE_BOOT_BUILD_SIGNED_BINARIES=n`): `.\tools\signing\sign.ps1 -In …\printdrive-unsigned.bin`.
Das Skript weigert sich, Abbilder ohne `CONFIG_PD_OTA_SIGNED` zu signieren – sie würden
angenommen, prüften danach aber selbst nichts mehr.

## 7. Umstieg von 0.5.0

Die Geräte laufen heute mit einer **unsignierten 0.5.0**, die jede gültige Firmware annimmt.

- **Über das Web:** Die signierte 0.6.0 (`printdrive-0.6.0-signed.bin`) einfach in der
  Weboberfläche von 0.5.0 einspielen. 0.5.0 prüft keine Signatur, die Datei ist aber ein gültiges
  Abbild und passt in den Bereich. Nach dem Neustart läuft 0.6.0, bestätigt sich nach 15 s, und ab
  jetzt nimmt das Gerät nur noch Dateien mit diesem Schlüssel an. Kontrolle: `/api/status` zeigt
  `"signed_required":true` und die eigene `key_id`.
- **Per USB:** `.\tools\flash.ps1 printdrive -Signed -Build`.
- Ein neuer Bootloader ist nicht nötig.
- Stürzt 0.6.0 in den ersten 15 s ab, kehrt der Bootloader zu 0.5.0 zurück – die nimmt dann wieder
  alles an (so wie vorher). Einfach erneut einspielen.
- Bis zum Umstieg ist ein Gerät mit 0.5.0 nicht geschützt. Unsignierte 0.6.0-Builds (Voreinstellung,
  CI) schalten den Schutz nicht ein – für die eigenen Geräte immer den signierten Build nehmen.
- Ein Gerät mit signierter Firmware wieder auf eine unsignierte Version bringen geht nur per USB
  (über das Web wird sie abgelehnt).

## 8. Schlüssel verloren, gewechselt oder verraten

**Verloren** (keine Sicherung): Die Geräte nehmen keine Web-Updates mehr an – für immer, denn der
alte Schlüssel lässt sich nicht wiederherstellen. Sie laufen aber normal weiter. Abhilfe pro Gerät
per USB:

1. neuen Schlüssel erzeugen (`new_key.ps1`; den alten Dateinamen vorher wegräumen oder
   `-KeyFile` mit neuem Namen),
2. `.\tools\flash.ps1 printdrive -Signed -Build` – Einstellungen und Dateien bleiben erhalten
   (es werden nur Bootloader, Partitionstabelle, Startdaten und die Firmware geschrieben).
   Das Gesamtabbild des Browser-Flashers setzt dagegen die Einstellungen zurück.

**Wechseln** (z. B. vorsorglich): geht in diesem Verfahren **nur per USB**. Das Gerät prüft nur den
ersten Signaturblock gegen den ersten Block der laufenden Firmware; eine Datei mit neuem Schlüssel
lehnt es übers Web ab. Also wie bei „verloren“: neuer Schlüssel, per USB flashen.

**Verraten** (jemand anderes hat den privaten Schlüssel): Der kann Updates bauen, die die Geräte
annehmen – mit Admin-Zugang. Sofort Admin-Passwörter ändern, neuen Schlüssel erzeugen und alle
Geräte per USB mit einem damit signierten Build flashen.

## 9. Testschritte (für den Koordinator)

Vorbereitung:

- Schlüssel vorhanden (`new_key.ps1`; für einen reinen Test mit `-KeyFile` an einem Testort).
- Signierter Build: `.\tools\signing\build_signed.ps1` → Schlüssel-ID notieren.
- Unsignierter Build derselben Version: `.\tools\idf.ps1 printdrive build` (Ordner `build`).
- Datei mit fremdem Schlüssel:
  ```powershell
  .\tools\signing\new_key.ps1 -KeyFile $env:TEMP\dd_fremd\fremd.pem
  .\tools\signing\sign.ps1 -In firmware\printdrive\build_signed\printdrive-unsigned.bin -KeyFile $env:TEMP\dd_fremd\fremd.pem -Out $env:TEMP\dd_fremd\fremd-signed.bin
  ```
- Veränderte Datei (ein Byte in der Mitte gekippt):
  ```powershell
  $src = (Resolve-Path firmware\printdrive\build_signed\printdrive.bin).Path
  $b = [IO.File]::ReadAllBytes($src); $b[[int]($b.Length / 2)] = $b[[int]($b.Length / 2)] -bxor 0xFF
  [IO.File]::WriteAllBytes("$env:TEMP\dd_fremd\veraendert.bin", $b)
  ```

Bei jedem abgewiesenen Update danach prüfen: Gerät läuft weiter, `uptime_s` läuft weiter,
`ota.partition` unverändert, `ota.pending_verify` false; nach einem Neustart startet dieselbe Version.

| Nr. | Schritt | Erwartet |
|---|---|---|
| 1 | Gerät mit 0.5.0: `printdrive-0.6.0-signed.bin` über die Weboberfläche einspielen | angenommen, Neustart; `/api/status`: Version 0.6.0, `ota.signed_required:true`, `ota.key_id` = notierte ID, `pending_verify` erst true, nach 15 s false; Log „web updates must be signed, key id …“ |
| 2 | `build\printdrive.bin` (unsigniert) einspielen | `422 UNSIGNED` (bei einer Größe, die kein Vielfaches von 4096 ist, sofort nach dem Kopf) |
| 3 | `build_signed\printdrive-unsigned.bin` einspielen (Größe ist Vielfaches von 4096) | wird ganz geschrieben, dann `422 UNSIGNED`; Gerät läuft weiter |
| 4 | `fremd-signed.bin` einspielen | `422 BAD_SIGNATURE`; Log nennt Bild-ID ≠ Geräte-ID |
| 5 | `veraendert.bin` einspielen | `422 BAD_SIGNATURE` |
| 6 | eine Text- oder Bilddatei einspielen | `422 INVALID_FILE` wie bisher |
| 7 | `printdrive-0.6.0-signed.bin` erneut einspielen | angenommen, Neustart in den anderen Bereich (`ota.partition` wechselt) |
| 8 | Rücksprung: signierte Datei einspielen und das Gerät **innerhalb von 15 s nach dem Neustart** abziehen und wieder einstecken | Gerät startet mit der vorherigen Firmware (`ota.partition` wie vor Schritt 8, `pending_verify:false`) |
| 9 | Per USB: `.\tools\flash.ps1 printdrive -Signed` | flasht, startet, `signed_required:true`, gleiche `key_id`; Einstellungen und Dateien erhalten |
| 10 | Schutz von `flash.ps1`: `build_signed\printdrive.bin` testweise durch `printdrive-unsigned.bin` ersetzen, `flash.ps1 printdrive -Signed` | bricht vor dem Flashen ab („trägt aber keine Signatur“); danach `build_signed.ps1` neu laufen lassen |
| 11 | Ohne Schlüssel: `.\tools\idf.ps1 printdrive build` in einem frischen Klon | baut; `/api/status` dieser Firmware: `signed_required:false`, `key_id:""` |
| 12 | Update über HTTPS (wenn eingeschaltet) wie Schritt 4 und 7 | gleiche Ergebnisse (Stack der HTTPS-Aufgabe reicht für die RSA-Prüfung) |

Skript-Prüfungen ohne Gerät: `verify.ps1` auf die Dateien aus der Vorbereitung – signiert/passt
(Exit 0), fremd (1), unsigniert (2).

## 10. Dateien

| Datei | Inhalt |
|---|---|
| `firmware/printdrive/main/Kconfig.projbuild` | Option `PD_OTA_SIGNED` (wählt die ESP-IDF-Optionen aus) |
| `firmware/printdrive/sdkconfig.defaults` | Voreinstellung: aus |
| `firmware/printdrive/sdkconfig.signed` | Overlay für den Release-Build |
| `firmware/printdrive/main/ota.c`, `ota.h` | Schlüssel-ID, Erkennung „unsigniert/falsche Signatur“ |
| `firmware/printdrive/main/web_ota.c` | `UNSIGNED` / `BAD_SIGNATURE` |
| `firmware/printdrive/main/web_system.c` | `ota.signed_required`, `ota.key_id` im Status |
| `tools/signing/*` | Schlüssel, Build, Signieren, Prüfen |
| `tools/flash.ps1` | `-Signed`, Schutz gegen unsignierte Abbilder mit Signaturpflicht |

Quellen in ESP-IDF 5.5.6: `docs/en/security/secure-boot-v2.rst` (Abschnitt „Signed App Verification
Without Hardware Secure Boot“), `components/bootloader/Kconfig.projbuild`,
`components/bootloader_support/src/secure_boot_v2/secure_boot_signatures_app.c`,
`components/bootloader_support/src/secure_boot.c` (Startprüfung), `components/app_update/esp_ota_ops.c`.
