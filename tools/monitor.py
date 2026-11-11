#!/usr/bin/env python3
"""Serial log reader for the dongle (needs pyserial, e.g. from the ESP-IDF Python env).

Usage:
    python tools/monitor.py [PORT] [--seconds N] [--out FILE] [--baud 115200]

- PORT omitted: first Espressif (VID 303A) port is used, a TinyUSB CDC port
  (printdrive) is preferred over the USB-Serial/JTAG port 303A:1001 (smoke_test).
- Reconnects automatically when the port disappears (device reboot, re-flash).
  With PORT omitted the port is searched again on every reconnect.
- Every line is prefixed with the local time; --out appends the lines (without
  ANSI colour codes) to a file as well.
- --seconds N stops after N seconds (for non-interactive use). Exit code 1 if no
  port could be opened at all during that time.

The chip is never reset by opening the port: DTR/RTS are set before open()
(both low on 303A:1001, the ESP32-S3 USB-Serial/JTAG reset lines). On the TinyUSB
CDC port DTR is set high because the printdrive firmware only forwards logs while
DTR is asserted; the 1200 baud download-mode touch is impossible since 1200 baud
is never used.
"""
import argparse
import datetime
import re
import sys
import time

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.stderr.write("pyserial fehlt - ESP-IDF-Umgebung laden (tools\\monitor.ps1 benutzen).\n")
    sys.exit(2)

ESPRESSIF_VID = 0x303A
JTAG_PID = 0x1001
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
PARTIAL_FLUSH_S = 0.5


def espressif_ports():
    ports = [p for p in list_ports.comports() if p.vid == ESPRESSIF_VID]
    ports.sort(key=lambda p: (len(p.device), p.device))
    return ports


def auto_port():
    ports = espressif_ports()
    cdc = [p for p in ports if p.pid != JTAG_PID]
    jtag = [p for p in ports if p.pid == JTAG_PID]
    if cdc:
        return cdc[0].device
    if jtag:
        return jtag[0].device
    return None


def port_present(name):
    return any(p.device.upper() == name.upper() for p in list_ports.comports())


def port_pid(name):
    for p in list_ports.comports():
        if p.device.upper() == name.upper():
            return p.vid, p.pid
    return None, None


def open_port(name, baud):
    vid, pid = port_pid(name)
    s = serial.Serial()
    s.port = name
    s.baudrate = baud
    s.timeout = 0.2
    if vid == ESPRESSIF_VID and pid != JTAG_PID:
        # TinyUSB CDC: firmware sends logs only while DTR is set; RTS has no meaning.
        s.dtr = True
        s.rts = False
    else:
        # USB-Serial/JTAG (or unknown adapter): keep both lines low -> no reset.
        s.dtr = False
        s.rts = False
    s.open()
    return s, vid, pid


def enable_ansi_console():
    if sys.platform != "win32":
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if k32.GetConsoleMode(h, ctypes.byref(mode)):
            k32.SetConsoleMode(h, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass


class Output:
    def __init__(self, out_path):
        self.file = open(out_path, "a", encoding="utf-8") if out_path else None

    def line(self, text, ts=None):
        ts = ts or datetime.datetime.now()
        stamp = ts.strftime("%H:%M:%S.%f")[:-3]
        sys.stdout.write("[%s] %s\n" % (stamp, text))
        sys.stdout.flush()
        if self.file:
            self.file.write("[%s] %s\n" % (ts.strftime("%Y-%m-%d ") + stamp, ANSI_RE.sub("", text)))
            self.file.flush()

    def info(self, text):
        self.line("-- " + text)

    def close(self):
        if self.file:
            self.file.close()


def main():
    ap = argparse.ArgumentParser(description="Serieller Log-Monitor fuer den Dongle (mit Auto-Reconnect).")
    ap.add_argument("port", nargs="?", help="z. B. COM5; ohne Angabe: erster 303A-Port (CDC bevorzugt)")
    ap.add_argument("--seconds", type=float, default=None, help="nach N Sekunden beenden")
    ap.add_argument("--out", default=None, help="Ausgabe zusaetzlich an diese Datei anhaengen")
    ap.add_argument("--baud", type=int, default=115200, help="Baudrate (Standard 115200, 1200 verboten)")
    args = ap.parse_args()

    if args.baud == 1200:
        ap.error("1200 Baud ist verboten (loest den Download-Modus-Touch aus).")

    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    enable_ansi_console()

    out = Output(args.out)
    deadline = time.monotonic() + args.seconds if args.seconds is not None else None

    def expired():
        return deadline is not None and time.monotonic() >= deadline

    ever_connected = False
    waiting_msg_shown = False
    last_open_error = None
    try:
        while not expired():
            name = args.port if args.port else auto_port()
            if not name or not port_present(name):
                if not waiting_msg_shown:
                    out.info("warte auf Port %s ..." % (name or args.port or "(303A, automatisch)"))
                    waiting_msg_shown = True
                time.sleep(0.5)
                continue

            try:
                ser, vid, pid = open_port(name, args.baud)
            except (serial.SerialException, OSError) as e:
                msg = str(e)
                if msg != last_open_error:
                    out.info("%s kann nicht geoeffnet werden (belegt?): %s - neuer Versuch ..." % (name, msg))
                    last_open_error = msg
                time.sleep(1.0)
                continue

            ever_connected = True
            waiting_msg_shown = False
            last_open_error = None
            kind = "USB-Serial/JTAG" if pid == JTAG_PID else ("CDC" if vid == ESPRESSIF_VID else "seriell")
            ids = "%04X:%04X" % (vid, pid) if vid is not None else "?"
            out.info("verbunden mit %s (%s, %s) @ %d Baud" % (name, ids, kind, args.baud))

            buf = ""
            line_start = None
            next_check = time.monotonic() + 1.0
            try:
                while not expired():
                    data = ser.read(ser.in_waiting or 1)
                    now = datetime.datetime.now()
                    if not data and time.monotonic() >= next_check:
                        # Some drivers keep returning b"" after a USB unplug instead of raising.
                        next_check = time.monotonic() + 1.0
                        if not port_present(name):
                            raise serial.SerialException("Port verschwunden")
                    if data:
                        if not buf:
                            line_start = now
                        buf += data.decode("utf-8", errors="replace")
                        while "\n" in buf:
                            line, buf = buf.split("\n", 1)
                            out.line(line.rstrip("\r"), line_start)
                            line_start = now
                    if buf and line_start and (now - line_start).total_seconds() > PARTIAL_FLUSH_S:
                        out.line(buf.rstrip("\r"), line_start)
                        buf = ""
            except (serial.SerialException, OSError) as e:
                if buf:
                    out.line(buf.rstrip("\r"), line_start)
                out.info("Verbindung zu %s verloren (%s) - warte auf Reconnect ..." % (name, e.__class__.__name__))
                waiting_msg_shown = True
            finally:
                try:
                    ser.close()
                except Exception:
                    pass
            time.sleep(0.3)
    except KeyboardInterrupt:
        pass
    finally:
        out.close()

    if deadline is not None and not ever_connected:
        sys.stderr.write("Kein Port geoeffnet innerhalb von %g s.\n" % args.seconds)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
