"""End-to-end test of the WiFi DataDrive web API against a real dongle.

Usage (ESP-IDF Python env provides 'requests'):
    python tools/api_test.py 192.168.2.152 [--drive E:]

What it does (the drive content is modified: test files are created and deleted):
  - CSRF protection, file type / content validation, free-space check
  - taking the drive from the USB host (refused without confirmation, then forced)
  - upload (speed measured), list, download + SHA256 compare, overwrite, delete
  - aborted upload (connection dropped mid-way) leaves no temp file / no busy state
  - providing the drive to USB again; with --drive the uploaded file is read from
    the Windows drive letter and compared
"""
import argparse
import hashlib
import io
import os
import socket
import sys
import time
import zipfile

import requests

H = {"X-PrintDrive": "1"}
results = []


def check(name, cond, info=""):
    results.append((name, bool(cond)))
    print(f"[{'OK ' if cond else 'FAIL'}] {name} {info}")
    return cond


def make_3mf(size_kb):
    """A minimal valid ZIP (like a 3MF container) with incompressible payload."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("3D/3dmodel.model", os.urandom(size_kb * 1024))
    return buf.getvalue()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("--drive", help="Windows drive letter of the dongle, e.g. E:")
    ap.add_argument("--size-kb", type=int, default=512)
    a = ap.parse_args()
    base = f"http://{a.host}"
    s = requests.Session()

    st = s.get(f"{base}/api/status", timeout=5).json()
    print("status:", st["drive"]["owner"], "free", st["drive"]["free"], "fw", st["version"])
    check("status reachable", st["version"])

    # --- CSRF: state-changing request without header must be refused
    r = s.post(f"{base}/api/mode?target=app", timeout=10)
    check("CSRF header required", r.status_code == 403, r.status_code)

    # --- take over from USB host
    if st["drive"]["owner"] == "usb":
        r = s.post(f"{base}/api/mode?target=app", headers=H, timeout=10)
        if st["drive"]["usb_connected"] and not st["drive"]["host_ejected"]:
            check("take-over refused without confirmation", r.status_code == 409 and
                  r.json().get("code") == "HOST_ACTIVE", r.text)
            time.sleep(st["drive"].get("write_guard_ms", 3000) / 1000 + 0.5)
            r = s.post(f"{base}/api/mode?target=app&force=1", headers=H, timeout=15)
        check("take-over with confirmation", r.ok and r.json().get("ok"), r.text)
    st = s.get(f"{base}/api/status", timeout=5).json()
    check("owner is app", st["drive"]["owner"] == "app")

    # --- validation
    r = s.post(f"{base}/api/upload/check?name=notes.txt&size=10", headers=H, timeout=10)
    if st.get("types") == "all":
        check("any file type accepted (types=all)", r.ok, r.text)
    else:
        check("wrong extension refused (415)", r.status_code == 415, r.text)
    r = s.post(f"{base}/api/upload/check?name=..%2Fx.3mf&size=10", headers=H, timeout=10)
    check("path in name refused (400)", r.status_code == 400, r.text)
    big = st["drive"]["free"] + 1024 * 1024
    r = s.post(f"{base}/api/upload/check?name=big.3mf&size={big}", headers=H, timeout=10)
    if big < 4 * 1024 ** 3:
        check("larger than free space refused (507)", r.status_code == 507, r.text)
    else:
        check("file >= 4 GiB refused (413)", r.status_code == 413 and r.json().get("code") == "TOO_LARGE", r.text)
    r = s.post(f"{base}/api/upload/check?name=big.3mf&size={5 * 1024 ** 3}", headers=H, timeout=10)
    check("5 GiB size not wrapped to 32 bit (413)", r.status_code == 413, r.text)
    bad = os.urandom(20000)
    r = s.post(f"{base}/api/upload?name=kaputt.3mf", headers=H, data=bad, timeout=60)
    check("non-ZIP .3mf refused (422)", r.status_code == 422, r.text)
    truncated = make_3mf(64)[:-30]
    r = s.post(f"{base}/api/upload?name=abgeschnitten.3mf", headers=H, data=truncated, timeout=60)
    check("truncated ZIP refused (422)", r.status_code == 422, r.text)

    # --- upload + speed
    name = "Testdruck Würfel äöü.3mf"
    data = make_3mf(a.size_kb)
    s.post(f"{base}/api/delete?name={requests.utils.quote(name)}", headers=H, timeout=10)
    r = s.post(f"{base}/api/upload/check?name={requests.utils.quote(name)}&size={len(data)}", headers=H,
               timeout=10)
    check("upload check ok", r.ok, r.text)
    mtime = 1790000000   # fixed "local" timestamp: must come back unchanged in the listing
    t = time.time()
    r = s.post(f"{base}/api/upload?name={requests.utils.quote(name)}&mtime={mtime}&now={int(time.time() - time.timezone)}",
               headers=H, data=data, timeout=600)
    dt = time.time() - t
    check("upload ok", r.ok and r.json().get("ok"), f"{len(data) / 1024:.0f} kB in {dt:.1f} s = "
          f"{len(data) / 1024 / dt:.1f} kB/s")

    listing = s.get(f"{base}/api/files", timeout=10).json()
    files = listing["files"]
    check("file listed", any(f["name"] == name and f["size"] == len(data) for f in files),
          [f["name"] for f in files])
    got = next((f.get("mtime") for f in files if f["name"] == name), None)
    check("modification time stored (FAT: 2 s resolution)", got is not None and abs(got - mtime) <= 2, got)
    check("listing reports hidden folders and truncation", "dirs" in listing and "truncated" in listing)

    # --- rename
    new = "Umbenannt ÄÖ.3mf"
    s.post(f"{base}/api/delete?name={requests.utils.quote(new)}", headers=H, timeout=10)
    r = s.post(f"{base}/api/rename?name={requests.utils.quote(name)}&to={requests.utils.quote(new)}",
               headers=H, timeout=10)
    names = [f["name"] for f in s.get(f"{base}/api/files", timeout=10).json()["files"]]
    check("rename", r.ok and new in names and name not in names, r.text)
    r = s.post(f"{base}/api/rename?name={requests.utils.quote(new)}&to=a%2Fb.3mf", headers=H, timeout=10)
    check("rename to invalid name refused (400)", r.status_code == 400, r.text)
    r = s.post(f"{base}/api/rename?name=gibtsnicht.bin&to=x.bin", headers=H, timeout=10)
    check("rename of missing file refused (404)", r.status_code == 404, r.text)
    s.post(f"{base}/api/upload?name=zweite.3mf", headers=H, data=make_3mf(1), timeout=60)
    r = s.post(f"{base}/api/rename?name=zweite.3mf&to={requests.utils.quote(new)}", headers=H, timeout=10)
    check("rename onto existing file refused (409)", r.status_code == 409 and r.json().get("code") == "EXISTS",
          r.text)
    s.post(f"{base}/api/delete?name=zweite.3mf", headers=H, timeout=10)
    r = s.post(f"{base}/api/rename?name={requests.utils.quote(new)}&to={requests.utils.quote(name)}",
               headers=H, timeout=10)
    check("rename back", r.ok, r.text)
    dl = s.get(f"{base}/api/download?name={requests.utils.quote(name)}", timeout=120).content
    check("download identical", hashlib.sha256(dl).digest() == hashlib.sha256(data).digest())

    r = s.post(f"{base}/api/upload?name={requests.utils.quote(name)}", headers=H, data=data, timeout=600)
    check("existing file not overwritten silently (409)", r.status_code == 409 and
          r.json().get("code") == "EXISTS", r.text)
    data2 = make_3mf(32)
    r = s.post(f"{base}/api/upload?name={requests.utils.quote(name)}&overwrite=1", headers=H, data=data2,
               timeout=600)
    dl = s.get(f"{base}/api/download?name={requests.utils.quote(name)}", timeout=120).content
    check("overwrite replaces content", r.ok and dl == data2)

    # --- aborted upload: send headers + part of the body, then drop the connection
    sock = socket.create_connection((a.host, 80), timeout=10)
    total = 300 * 1024
    sock.sendall((f"POST /api/upload?name=abbruch.3mf HTTP/1.1\r\nHost: {a.host}\r\nX-PrintDrive: 1\r\n"
                  f"Content-Length: {total}\r\nContent-Type: application/octet-stream\r\n\r\n").encode())
    sock.sendall(make_3mf(100)[:100 * 1024])
    time.sleep(1.5)
    # The ESP-IDF HTTP server handles one request at a time, so the status cannot be
    # queried while the upload handler is busy - it answers once the abort is handled.
    sock.close()
    st = s.get(f"{base}/api/status", timeout=60).json()
    files = s.get(f"{base}/api/files", timeout=10).json()["files"]
    check("aborted upload cleaned up", not st["upload"]["active"] and
          not any(f["name"] == "abbruch.3mf" for f in files), st["upload"])

    # --- remove leftovers of manual speed tests
    s.post(f"{base}/api/delete?name=pd_speedtest_10mb.bin", headers=H, timeout=10)

    # --- provide to USB and verify on the host
    r = s.post(f"{base}/api/mode?target=usb", headers=H, timeout=15)
    check("provide to USB", r.ok and r.json().get("ok"), r.text)
    r = s.post(f"{base}/api/delete?name=x.3mf", headers=H, timeout=10)
    check("file operations refused while host owns drive", r.status_code == 409, r.text)
    if a.drive:
        path = os.path.join(a.drive + os.sep, name)
        for _ in range(40):
            if os.path.exists(path):
                break
            time.sleep(0.5)
        ok = os.path.exists(path) and open(path, "rb").read() == data2
        check("uploaded file visible and identical on USB drive", ok, path)

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
