# 01 – Hardware-Bestandsaufnahme

> Hinweis: Das Projekt heißt seit Firmware 0.2.0 **WiFi DataDrive** (vorher WiFi PrintDrive). Die Messwerte unten sind so belassen, wie sie am 2026-11-11 mit Firmware 0.1.0 aufgenommen wurden; USB-Namen und Laufwerksbezeichnung lauten inzwischen „DataDrive“ bzw. `DATADRIVE`.

Gerät: **LILYGO T-Dongle-S3, LCD-Variante** (USB-A-Stecker, 0,96"-Farbdisplay,
microSD-Slot, RGB-LED, BOOT-Taste).

Jede Angabe ist einer von drei Kategorien zugeordnet:

| Kategorie | Bedeutung |
|---|---|
| **verifiziert** | am echten Gerät gemessen/ausgelesen (Datum angegeben) |
| **Hersteller** | aus den Herstellerunterlagen, nicht unabhängig geprüft |
| **Annahme** | plausibel, aber weder gemessen noch dokumentiert |

## 1. Verifiziert am 2026-11-11 (esptool, Werks-Firmware)

| Eigenschaft | Wert |
|---|---|
| Chip | ESP32-S3 (QFN56), Revision v0.2 |
| Features | WiFi, BLE |
| PSRAM | **keins** |
| Quarz | 40 MHz |
| MAC | `e0:72:a1:c1:54:d4` |
| Flash | Hersteller-ID `0xEF`, Device `0x4018` = Winbond W25Q128, **16 MB** |
| Flash-Modus | Quad-Modus in eFuse gesetzt, 3,3 V |
| USB (Werks-Firmware) | VID `303A` PID `1001` (ESP32-S3 USB-Serial/JTAG) → unter Windows **COM4** |

Mit eigener Firmware (Smoke-Test, `printdrive`) unter Windows 11, ebenfalls 2026-11-11
(Details: [05_testprotokoll.md](05_testprotokoll.md)):

| Eigenschaft | Wert |
|---|---|
| LCD | Pins, Querformat, Gap und Farbreihenfolge aus Abschnitt 2 korrekt (Farbbalken, Text nicht gespiegelt) |
| RGB-LED, BOOT-Taste | funktionieren mit den Werten aus Abschnitt 2 |
| Freier Heap | ca. 220 KB (Smoke-Test, WLAN-AP aktiv) |
| USB (`printdrive`) | Composite-Gerät `303A:4003`: MSC + CDC („Serielles USB-Gerät“, **COM5**), Seriennummer `E072A1C154D4` |
| Laufwerk (interner Flash) | „PrintDrv WiFi PrintDrive“, MBR, 512-B-Sektoren, Label `PRINTDRIVE`, 11.288.576 Byte; ursprünglich FAT16 (2-KiB-Cluster), inzwischen FAT12 mit 4-KiB-Clustern (siehe [02](02_architektur.md) Abschnitt 2) |
| Datenrate USB (Flash) | schreiben 47 KB/s, lesen 753 KB/s (1-MiB-Datei) |
| WLAN STA | Verbindung mit dem Heimnetz über Setup-AP + Weboberfläche (Handy), IP `192.168.2.152` |
| microSD-Slot (SDMMC 4 Bit, 40 MHz) | funktioniert mit den Pins aus Abschnitt 2; Testkarte „SDABC“, 59.645 MB, FAT32, Label `DATA` → Windows `E:` FAT32 62,5 GB |
| Datenrate microSD | USB schreiben 832 KB/s (10 MB), Web-Upload 1335–1578 KB/s (4 MB) |
| Lage des microSD-Slots | am USB-Stecker: Karte nur bei ausgestecktem Dongle wechselbar |
| ROM-Download-Modus nach Software-Neustart | erscheint wieder als `303A:1001` (COM4), aber nur, wenn die Firmware den USB-PHY vorher an USB-Serial/JTAG zurückgibt |
| Strapping | nach BOOT + Einstecken bleibt der Download-Modus bis zum echten Aus-/Einstecken gelatcht (`GPIO_STRAP_REG` = 0 trotz losgelassener Taste) |
| USB-Serial/JTAG an diesem PC | verliert bei Bursts Daten (Flash-Lesen, Log); Vollbackup nicht zuverlässig möglich |

Folgerungen für die Firmware:

- Kein PSRAM → alle Puffer im internen RAM; Upload-Puffer 16 KB, Framebuffer 25,6 KB,
  keine grafische Bibliothek (kein LVGL).
- 16 MB Flash erlauben zwei App-Slots (OTA) plus ca. 11 MB Dateispeicher.
- Der native USB-Anschluss ist der einzige Datenanschluss am PC. Sobald TinyUSB läuft
  (Hauptfirmware), verschwindet die USB-Serial/JTAG-Schnittstelle (COM4) –
  siehe [04_build_und_flash.md](04_build_und_flash.md). Die PHY-Zuordnung von TinyUSB
  übersteht einen Software-Reset; vor dem Neustart in den Download-Modus muss die
  Firmware sie zurücksetzen.
- Schreiben ins interne Flash ist durch das Löschen der 4-KiB-Sektoren begrenzt (grob
  90 KB/s, siehe Abschnitt 2 und [06_risiken.md](06_risiken.md)); das FAT muss daher auf
  4-KiB-Grenzen ausgerichtet sein und 4-KiB-Cluster verwenden. Schnelleres Schreiben und
  mehr Platz mit microSD (Phase 5, verifiziert: ca. 18–30-mal schneller).

## 2. Aus Herstellerunterlagen (nicht unabhängig geprüft)

Quelle: <https://github.com/Xinyuan-LilyGO/T-Dongle-S3> (Pin-Doku `docs/en/t-dongle-s3/REAMDE.MD`,
Beispiel `examples/lcd/lcd.ino`, Board-Definition `boards/dongles3.json`).
Die Werte sind in `firmware/components/board/include/board.h` zentral hinterlegt.
LCD, LED, BOOT-Taste und microSD-Pins sind inzwischen bestätigt (Abschnitt 1);
die Versorgung (Stromaufnahme) ist weiterhin ungeprüft.

### LCD

| Eigenschaft | Wert |
|---|---|
| Controller | ST7735, 0,96", 160 × 80 Pixel |
| Bus | SPI2: MOSI = GPIO3, SCLK = GPIO5, CS = GPIO4, DC = GPIO2, RST = GPIO1 |
| Hintergrundbeleuchtung | GPIO38, **aktiv LOW** |
| Querformat | Gap (x = 1, y = 26), `swap_xy = true`, `mirror(false, true)` |
| Farbe | invertiert, Reihenfolge BGR |

### Weitere Peripherie

| Funktion | Pins / Daten |
|---|---|
| RGB-LED | APA102, DATA = GPIO40, CLK = GPIO39, Farbreihenfolge BGR |
| BOOT-Taste | GPIO0 (LOW = gedrückt; zugleich Strapping-Pin für den Download-Modus) |
| microSD (SDMMC 4 Bit) | D0 = 14, D1 = 17, D2 = 21, D3 = 18, CLK = 12, CMD = 16 – seit Phase 5 genutzt, **verifiziert** 2026-11-11 |
| Versorgung | USB 4,8–5,5 V, max. 800 mA |
| Flash-Konfiguration | QIO, 80 MHz (`boards/dongles3.json`) |
| Flash W25Q128, Sektor löschen (4 KiB) | typ. ca. 45 ms (Winbond-Datenblatt, typischer Wert; nicht gemessen) |

### Software-Empfehlungen des Herstellers

- Für USB-Beispiele wird Arduino-Core 3.3+ empfohlen.
- Die PlatformIO-Umgebung verwendet `espressif32@6.12.0`.

## 3. Annahmen

| Annahme | Begründung / Prüfung |
|---|---|
| UART0 (GPIO43/44) ist am QWIIC-Stecker herausgeführt | Kommentar in `firmware/printdrive/sdkconfig.defaults`; nicht nachgemessen. Die Hauptfirmware gibt ihre Konsole dort aus. |
Die früheren Annahmen „ROM meldet sich nach Software-Neustart als 303A:1001“,
„Pinbelegung gilt für diese Revision“ (LCD, LED, Taste) und „microSD-Pins gelten für die
vorliegende Board-Revision“ sind seit 2026-11-11 verifiziert (Abschnitt 1).

## 4. Technologieentscheidung

**Entscheidung: ESP-IDF v5.5.6** (installiert unter `%USERPROFILE%\esp\esp-idf-v5.5.6`)
statt Arduino/PlatformIO.

Begründung:

- Kern des Projekts ist das sichere Teilen eines Dateisystems zwischen USB-Host und
  Firmware. Die Komponente `esp_tinyusb` bietet dafür eine MSC-Storage-API mit
  **expliziter Besitzer-Umschaltung** (App ↔ USB-Host) und lässt sich auf manuelle
  Steuerung stellen (`auto_mount_off`).
- Direkter Zugriff auf TinyUSB-Callbacks, Wear Levelling und FatFs (für eigenes
  Formatieren, Host-I/O-Sperre und Barrier, siehe [02_architektur.md](02_architektur.md)).
- Reproduzierbare Abhängigkeiten über den Component Manager (`dependencies.lock`).

Gepinnte Abhängigkeiten (`firmware/printdrive/main/idf_component.yml`,
`firmware/components/pd_wifi/idf_component.yml`, aufgelöst in `dependencies.lock`):

| Komponente | Version | Hinweis |
|---|---|---|
| ESP-IDF | 5.5.6 | Manifest erlaubt `>=5.5,<6.0` |
| espressif/esp_tinyusb | 2.4.0 (`~2.4.0`) | Firmware nutzt interne Details, siehe Risiken |
| espressif/tinyusb | 0.21.0~2 (exakt) | |
| espressif/mdns | 1.14.1 (`~1.14.1`) | |

## 5. Übernommener Fremdcode

| Code | Herkunft | Lizenz | Ort |
|---|---|---|---|
| ST7735-Paneltreiber (`esp_lcd`) | LILYGO T-Dongle-S3, `examples/lcd/bsp_lcd` | MIT | `firmware/components/st7735/` (`LICENSE.LILYGO`) |
| 5×7-Bitmap-Font (glcdfont) | Adafruit GFX, via TFT_eSPI im LILYGO-Repo | BSD | `firmware/components/display/font5x7.c` (`LICENSE.Adafruit_GFX`) |

Lokale Änderungen am ST7735-Treiber: `rgb_ele_order` statt des in ESP-IDF 5.x
veralteten `color_space`, Init-Log von INFO auf DEBUG.

## 6. Noch zu verifizieren

LCD, LED, BOOT-Taste, Flash-Größe, Setup-AP, WLAN STA und microSD-Slot sind seit
2026-11-11 bestätigt. Offen:

- RSSI am Drucker-Standort
- UART0 an GPIO43/44
- microSD mit weiteren Karten (Qualität, exFAT-Karte nach Web-Formatierung)
- Stromaufnahme unter WLAN-Last, mit microSD (offen, kein Messgerät eingeplant)

Ablauf und erwartete Ausgaben: [05_testprotokoll.md](05_testprotokoll.md).

## 7. Nachtrag Firmware 0.3.0 und 0.4.0 (Hardware-Befunde)

Die Hardware ist unverändert (gleicher Dongle, gleiche Pins). Neu sind diese Befunde aus der
Firmware-Entwicklung:

| Punkt | Stand | Quelle |
|---|---|---|
| Kein PSRAM auf diesem Board | **verifiziert** (Board-Revision wie in Abschnitt 1) | Folgen: HTTPS-Puffer passt nicht, siehe [14](14_vormerkliste.md) |
| AES-Beschleuniger des ESP32-S3 für die Laufwerks-Verschlüsselung | **verifiziert**: Selbsttest mit IEEE-1619-Prüfwerten besteht beim Start; Rechenleistung 4,7 MB/s über die Hardware, 0,5 MB/s über die Bibliothek | [05](05_testprotokoll.md) Phase 8 |
| Schlüsselableitung (PBKDF2-HMAC-SHA256) | Kconfig-Ziel 2 s pro Ableitung (`CONFIG_PD_CRYPT_KDF_TARGET_MS`); beim Entsperren etwa 2 bis 3,5 s | [12](12_api_v4.md) Abschnitt 2.2; Ableitung im Code `firmware/components/pd_crypt` |
| microSD 64 GB verschlüsselt: Einrichten 51 s | **verifiziert** | [05](05_testprotokoll.md) Phase 8 |
| Stromaufnahme mit Verschlüsselung und Karte | **noch nicht geprüft** | – |
| Verschlüsselung des internen Flashs (ohne Karte) | **noch nicht geprüft** | – |

Weiter offen aus Abschnitt 6: RSSI am Drucker-Standort, UART0 an GPIO43/44 (nicht nachgemessen).
