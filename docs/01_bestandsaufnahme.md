# 01 – Hardware-Bestandsaufnahme

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

Folgerungen für die Firmware:

- Kein PSRAM → alle Puffer im internen RAM; Upload-Puffer 16 KB, Framebuffer 25,6 KB,
  keine grafische Bibliothek (kein LVGL).
- 16 MB Flash erlauben zwei App-Slots (OTA) plus ca. 11 MB Dateispeicher.
- Der native USB-Anschluss ist der einzige Datenanschluss am PC. Sobald TinyUSB läuft
  (Hauptfirmware), verschwindet die USB-Serial/JTAG-Schnittstelle (COM4) –
  siehe [04_build_und_flash.md](04_build_und_flash.md).

## 2. Aus Herstellerunterlagen (nicht unabhängig geprüft)

Quelle: <https://github.com/Xinyuan-LilyGO/T-Dongle-S3> (Pin-Doku `docs/en/t-dongle-s3/REAMDE.MD`,
Beispiel `examples/lcd/lcd.ino`, Board-Definition `boards/dongles3.json`).
Die Werte sind in `firmware/components/board/include/board.h` zentral hinterlegt.

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
| microSD (SDMMC 4 Bit) | D0 = 14, D1 = 17, D2 = 21, D3 = 18, CLK = 12, CMD = 16 – **erst ab Phase 5 genutzt** |
| Versorgung | USB 4,8–5,5 V, max. 800 mA |
| Flash-Konfiguration | QIO, 80 MHz (`boards/dongles3.json`) |

### Software-Empfehlungen des Herstellers

- Für USB-Beispiele wird Arduino-Core 3.3+ empfohlen.
- Die PlatformIO-Umgebung verwendet `espressif32@6.12.0`.

## 3. Annahmen

| Annahme | Begründung / Prüfung |
|---|---|
| UART0 (GPIO43/44) ist am QWIIC-Stecker herausgeführt | Kommentar in `firmware/printdrive/sdkconfig.defaults`; nicht nachgemessen. Die Hauptfirmware gibt ihre Konsole dort aus. |
| Nach Software-Neustart in den Download-Modus meldet sich der ROM-Bootloader wieder als 303A:1001 | Verhalten des ESP32-S3-ROM; mit dieser Hardware noch nicht getestet. |
| Die Pinbelegung gilt für die vorliegende Board-Revision | Smoke-Test (Farbbalken, LED, Taste) prüft das. |

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

## 6. Noch zu verifizieren (Smoke-Test, Phase 0)

- LCD-Pins, Orientierung, Gap und Farbreihenfolge (Farbbalken rot/grün/blau/weiß korrekt?)
- APA102-LED inkl. Farbreihenfolge
- BOOT-Taste an GPIO0
- Flash-Größe aus Firmware-Sicht (`SMOKE: flash size 16777216 bytes`)
- WLAN STA und Setup-AP
- Stromaufnahme unter WLAN-Last (offen, kein Messgerät eingeplant)

Ablauf und erwartete Ausgaben: [05_testprotokoll.md](05_testprotokoll.md).
