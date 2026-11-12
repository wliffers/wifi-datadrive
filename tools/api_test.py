"""End-to-end test of the WiFi DataDrive web API (version 3, docs/09_api_v3.md).

Works against the mock (tools/mock_server.py) and a real dongle.

Usage (the ESP-IDF Python env and the system Python provide 'requests'):
    python tools/api_test.py HOST[:PORT] --user admin --password PW
                             [--drive E:] [--size-kb 512] [--skip-destructive]
                             [--wifi-test] [--ota-probe]

Sections: 1 authentication, 2 users and roles, 3 trusted devices, 4 mode and files,
5 Wi-Fi, 6 status (+ optional OTA probe), 7 Windows drive letter (--drive).

The target is left as found: owner mode restored, everything created (test folder
_apitest_<random>, temporary user apitest_<random>, trusted devices, Wi-Fi entry) removed,
own sessions logged out. Never formats, reboots or flashes; only touches what it created.

Options
  --skip-destructive  do not take the drive from a USB host and do not switch the mode
                      (file sections only run if the file manager already owns the drive;
                      the USB-mode checks and the take-over push are skipped).
  --wifi-test         also add and remove a dummy Wi-Fi network (a real device tries to
                      reconnect after an add, so this is opt-in).
  --ota-probe         POST a 16 byte invalid body to /api/ota (expects INVALID_FILE).
  --drive E:          after providing the drive to USB, compare the pushed file on the
                      Windows drive letter.

Wrong passwords: at most 2 (one wrong login, one wrong `old` password), so the lockout
(5 in a row) of a real device is never reached. If the account still has the delivery
password (`default_password: true`) the test stops and asks to change it first.

Exit code: 0 all checks passed, 1 failed checks, 2 aborted (login / default password).
"""
import argparse
import hashlib
import io
import os
import re
import socket
import sys
import time
import zipfile
from urllib.parse import quote, urlencode

import requests

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

results = []
skipped = []
DEF = object()          # "use the main session"


class Abort(Exception):
    pass


class S:                # run state (also used by the cleanup)
    base = ""
    host = ""
    port = 80
    user = ""
    password = ""
    main = None
    open_tokens = set()
    orig_owner = None
    took = False
    T = None
    T_made = False
    extra_paths = []
    tmp_user = None
    extra_users = []
    device_ids = []
    wifi_ssid = None
    files_ok = False
    pushed = None       # (path, data) pushed while the drive was taken over from USB
    guard = 3.0
    skip_destructive = False
    ota_part = None


def check(name, cond, info=""):
    results.append((name, bool(cond)))
    print(f"[{'OK ' if cond else 'FAIL'}] {name} {info}".rstrip(), flush=True)
    return bool(cond)


def skip(name, why):
    skipped.append(name)
    print(f"[SKIP] {name} ({why})", flush=True)


def note(msg):
    print(f"       {msg}", flush=True)


def head(title):
    print(f"\n=== {title} ===", flush=True)


# ------------------------------------------------------------------ HTTP helpers

def qs(params):
    return urlencode(params, quote_via=quote)


def call(method, path, params=None, form=None, body=None, tok=DEF, basic=None, csrf=True,
         timeout=20, retry=True):
    """One request with timeout and one retry on connection errors (Wi-Fi hiccups)."""
    url = S.base + path + ("?" + qs(params) if params else "")
    h = {}
    if method == "POST" and csrf:
        h["X-PrintDrive"] = "1"
    if tok is DEF:
        tok = S.main
    if tok:
        h["Cookie"] = f"dd_session={tok}"
    if form is not None:
        body = qs(form).encode()
        h["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        h["Content-Type"] = "application/octet-stream"
    for attempt in (0, 1):
        try:
            return requests.request(method, url, headers=h, data=body, auth=basic, timeout=timeout)
        except requests.ConnectionError:
            if attempt or not retry:
                raise
            time.sleep(2)
    raise RuntimeError("unreachable")


def jr(r):
    try:
        v = r.json()
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


def jcode(r):
    return jr(r).get("code")


def okr(r):
    return r.status_code == 200 and jr(r).get("ok") is True


def expect(name, r, status, code=None):
    good = r.status_code == status and (code is None or jcode(r) == code)
    return check(name, good, f"-> {r.status_code} {jcode(r) or ''}" if good else
                 f"expected {status} {code or ''}, got {r.status_code} {r.text[:120]!r}")


def cookie_token(r):
    m = re.search(r"dd_session=([0-9a-f]{64})", r.headers.get("Set-Cookie", ""))
    return m.group(1) if m else None


def login(user, pw, remember=False, device=None):
    form = {"user": user, "pass": pw}
    if remember:
        form["remember"] = "1"
        form["device"] = device or "apitest"
    r = call("POST", "/api/login", form=form, tok=None)
    tok = cookie_token(r)
    if tok:
        S.open_tokens.add(tok)
    return r, tok


def logout(tok):
    r = call("POST", "/api/logout", tok=tok)
    S.open_tokens.discard(tok)
    return r


def status():
    return jr(call("GET", "/api/status"))


def ls(d):
    r = call("GET", "/api/files", params={"dir": d})
    return r, {e["name"]: e for e in jr(r).get("entries", [])}


def get_users():
    r = call("GET", "/api/users")
    return jr(r).get("users", []), jr(r).get("max", 8)


def make_3mf(size_kb):
    """A minimal valid ZIP (like a 3MF container) with incompressible payload."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("3D/3dmodel.model", os.urandom(size_kb * 1024))
    return buf.getvalue()


def sha(b):
    return hashlib.sha256(b).hexdigest()


def up_timeout(n):
    return 30 + n / 20000


def upload(path, data, overwrite=False, mtime=None, push=False, force=False, ep=None):
    p = {"path": path, "now": str(int(time.time() - time.timezone))}
    if overwrite:
        p["overwrite"] = "1"
    if mtime:
        p["mtime"] = str(mtime)
    if push and force:
        p["force"] = "1"
    return call("POST", ep or ("/api/push" if push else "/api/upload"), params=p, body=data,
                timeout=up_timeout(len(data)))


def download(path):
    r = call("GET", "/api/download", params={"path": path}, timeout=up_timeout(5_000_000))
    return r


def mode(target, force=False):
    p = {"target": target}
    if force:
        p["force"] = "1"
    return call("POST", "/api/mode", params=p, timeout=30)


def ensure_app():
    """Take the drive over (waits the write guard, forces after HOST_ACTIVE). Returns bool."""
    st = status()
    if st.get("drive", {}).get("owner") == "app":
        return True
    force = False
    for _ in range(6):
        r = mode("app", force)
        if okr(r):
            return True
        c = jcode(r)
        if c == "HOST_ACTIVE":
            time.sleep(S.guard + 0.5)
            force = True
        elif c == "HOST_WRITING":
            time.sleep(S.guard + 1)
        else:
            return False
    return False


def wait_online(limit=45):
    t = time.time()
    while time.time() - t < limit:
        try:
            if call("GET", "/api/status", timeout=5, retry=False).status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(1.5)
    return False


# ------------------------------------------------------------------ sections

def sec_auth(a):
    head("1. Authentication")
    for p in ("/api/status", "/api/files", "/api/users", "/api/devices", "/api/log", "/api/wifi/list",
              "/api/download?path=x"):
        path, _, q = p.partition("?")
        r = call("GET", path, params={"path": "x"} if q else None, tok=None)
        expect(f"401 without login: GET {path}", r, 401, "UNAUTHORIZED")
    expect("401 without login: POST /api/mkdir", call("POST", "/api/mkdir", params={"path": "x"}, tok=None),
           401, "UNAUTHORIZED")
    expect("401 without login: POST /api/logout", call("POST", "/api/logout", tok=None), 401, "UNAUTHORIZED")
    expect("401 for an unknown session cookie", call("GET", "/api/status", tok="a" * 64), 401, "UNAUTHORIZED")
    for p, ctype in (("/", "text/html"), ("/index.html", "text/html"),
                     ("/manifest.webmanifest", "manifest"), ("/icon-180.png", "image/png"),
                     ("/icon-192.png", "image/png"), ("/icon-512.png", "image/png")):
        r = call("GET", p, tok=None)
        check(f"allow-list without login: GET {p}", r.status_code == 200 and ctype in r.headers.get("Content-Type", ""),
              f"{r.status_code} {r.headers.get('Content-Type', '')}")
    r = call("GET", "/api/session", tok=None)
    expect("allow-list: GET /api/session answers 401 UNAUTHORIZED without login", r, 401, "UNAUTHORIZED")

    r, tok = login(a.user, "wrong_" + os.urandom(3).hex())
    expect("login with wrong password (BAD_LOGIN)", r, 401, "BAD_LOGIN")
    check("wrong login sets no cookie", tok is None)

    r, tok = login(a.user, a.password)
    if r.status_code != 200 or not tok:
        raise Abort(f"login as '{a.user}' failed ({r.status_code} {jcode(r)}). Check --user / --password.")
    S.main = tok
    j = jr(r)
    if j.get("default_password"):
        logout(tok)
        S.main = None
        raise Abort("Account still has the delivery password (default_password: true). Change the password "
                    "first (web UI or POST /api/users/password) and run the test again. Nothing was changed.")
    if j.get("role") != "admin":
        raise Abort(f"User '{a.user}' is not an administrator; the test needs admin rights.")
    check("login ok (admin session)", j.get("ok") and j.get("user", "").lower() == a.user.lower(), j)
    sc = r.headers.get("Set-Cookie", "")
    check("cookie dd_session = 64 hex characters, HttpOnly", "HttpOnly" in sc, sc[:40] + "...")
    check("cookie SameSite=Strict", "samesite=strict" in sc.lower())
    check("session cookie without Max-Age (not remembered)", "max-age" not in sc.lower())
    r = call("GET", "/api/session")
    j = jr(r)
    check("/api/session with cookie", r.status_code == 200 and j.get("user", "").lower() == a.user.lower()
          and j.get("role") == "admin" and j.get("default_password") is False, j)

    r = call("GET", "/api/session", tok=None, basic=(a.user, a.password))
    check("HTTP Basic: /api/session", r.status_code == 200 and jr(r).get("role") == "admin")
    r = call("GET", "/api/status", tok=None, basic=(a.user, a.password))
    check("HTTP Basic: /api/status, no cookie set", r.status_code == 200 and "Set-Cookie" not in r.headers)

    st = status()
    S.orig_owner = st.get("drive", {}).get("owner")
    S.guard = st.get("drive", {}).get("write_guard_ms", 3000) / 1000
    r = call("POST", "/api/mode", params={"target": "app"}, csrf=False)
    expect("POST without X-PrintDrive header refused (403 FORBIDDEN)", r, 403, "FORBIDDEN")
    check("... and the mode did not change", status().get("drive", {}).get("owner") == S.orig_owner)

    r, t2 = login(a.user, a.password)
    check("second login", okr(r) and t2 and t2 != S.main)
    if t2:
        expect("logout", logout(t2), 200)
        expect("logged-out cookie no longer valid", call("GET", "/api/status", tok=t2), 401, "UNAUTHORIZED")
        check("main session still valid", call("GET", "/api/status").status_code == 200)


def sec_users(a):
    head("2. Users and roles")
    users, mx = get_users()
    check("user list", any(u["name"].lower() == a.user.lower() for u in users), f"{len(users)} of {mx}")
    if len(users) >= mx - 1:
        skip("user tests", "user table (almost) full")
        return
    rid = os.urandom(3).hex()
    name = S.tmp_user = f"apitest_{rid}"
    pw1, pw2, pw3 = (f"Pw{i}-{os.urandom(4).hex()}" for i in (1, 2, 3))
    form = {"user": name, "pass": pw1, "role": "user"}
    expect("add user (role user)", call("POST", "/api/users/add", form=form), 200)
    users, _ = get_users()
    check("new user listed with role user", any(u["name"] == name and u["role"] == "user" for u in users))
    expect("add again -> EXISTS", call("POST", "/api/users/add", form=form), 409, "EXISTS")
    expect("add with other case -> EXISTS", call("POST", "/api/users/add", form={**form, "user": name.upper()}),
           409, "EXISTS")
    for bad in ("bad name!", "x" * 25):
        S.extra_users.append(bad)
        expect(f"BAD_USER for {bad!r}", call("POST", "/api/users/add", form={**form, "user": bad}), 400, "BAD_USER")
    n2 = name + "b"
    S.extra_users.append(n2)
    for label, pw in (("4 bytes", "abcd"), ("65 bytes", "p" * 65)):
        expect(f"BAD_PASSWORD ({label})", call("POST", "/api/users/add", form={**form, "user": n2, "pass": pw}),
               400, "BAD_PASSWORD")
    users, _ = get_users()
    check("rejected users were not created", not any(u["name"] in S.extra_users for u in users))

    r, u1 = login(name, pw1)
    check("login as new user", okr(r) and u1 and jr(r).get("role") == "user" and jr(r).get("default_password") is False)
    r, u2 = login(name, pw1)
    if not (u1 and u2):
        raise RuntimeError("user login failed, cannot continue the user section")
    check("user may read status and devices", call("GET", "/api/status", tok=u1).status_code == 200
          and call("GET", "/api/devices", tok=u1).status_code == 200)
    n3 = name + "c"
    S.extra_users.append(n3)
    role_checks = [
        ("GET wifi/list", call("GET", "/api/wifi/list", tok=u1)),
        ("GET users", call("GET", "/api/users", tok=u1)),
        ("GET log", call("GET", "/api/log", tok=u1)),
        ("POST users/add", call("POST", "/api/users/add", form={**form, "user": n3}, tok=u1)),
        ("POST users/delete", call("POST", "/api/users/delete", params={"user": name + "q"}, tok=u1)),
        ("POST users/role", call("POST", "/api/users/role", params={"user": name, "role": "admin"}, tok=u1)),
        ("POST settings/host_writable", call("POST", "/api/settings/host_writable", tok=u1)),
        ("POST format (no confirm)", call("POST", "/api/format", tok=u1)),
    ]
    for label, r in role_checks:
        expect(f"FORBIDDEN_ROLE for user: {label}", r, 403, "FORBIDDEN_ROLE")
    pwtmp = "Tmp-" + os.urandom(4).hex()
    r = call("POST", "/api/users/password", form={"user": a.user, "pass": pwtmp, "old": "x"}, tok=u1)
    if okr(r):       # must never happen; put the password back
        call("POST", "/api/users/password", form={"user": a.user, "pass": a.password, "old": pwtmp})
        note("WARNING: user changed the admin password; restored")
    expect("FORBIDDEN_ROLE for user: foreign password", r, 403, "FORBIDDEN_ROLE")
    users, _ = get_users()
    check("role-protected calls created nothing", not any(u["name"] == n3 for u in users))

    # role change
    expect("role change user -> admin", call("POST", "/api/users/role", params={"user": name, "role": "admin"}), 200)
    users, _ = get_users()
    check("... listed as admin", any(u["name"] == name and u["role"] == "admin" for u in users))
    expect("role change back to user", call("POST", "/api/users/role", params={"user": name, "role": "user"}), 200)
    users, _ = get_users()
    check("... listed as user", any(u["name"] == name and u["role"] == "user" for u in users))
    expect("role of unknown user -> NOT_FOUND",
           call("POST", "/api/users/role", params={"user": name + "zz", "role": "user"}), 404, "NOT_FOUND")

    # LAST_ADMIN
    admins = [u for u in users if u["role"] == "admin"]
    if len(admins) == 1:
        r = call("POST", "/api/users/role", params={"user": admins[0]["name"], "role": "user"})
        if r.status_code == 409:
            expect("demote the only admin -> LAST_ADMIN", r, 409, "LAST_ADMIN")
            expect("delete the only admin -> LAST_ADMIN",
                   call("POST", "/api/users/delete", params={"user": admins[0]["name"]}), 409, "LAST_ADMIN")
        else:
            check("demote the only admin -> LAST_ADMIN", False, f"{r.status_code} {r.text[:100]!r}")
            call("POST", "/api/users/role", params={"user": admins[0]["name"], "role": "admin"})
            note("WARNING: the only admin was demoted; tried to restore")
    else:
        skip("LAST_ADMIN protection", f"{len(admins)} admins exist")

    # passwords
    expect("own password with wrong old -> BAD_LOGIN",
           call("POST", "/api/users/password", form={"user": name, "pass": pw2, "old": "wrong-" + rid}, tok=u1),
           401, "BAD_LOGIN")
    expect("own password, BAD_PASSWORD (4 bytes)",
           call("POST", "/api/users/password", form={"user": name, "pass": "abcd", "old": pw1}, tok=u1),
           400, "BAD_PASSWORD")
    expect("own password change with correct old",
           call("POST", "/api/users/password", form={"user": name, "pass": pw2, "old": pw1}, tok=u1), 200)
    check("changing session stays valid", call("GET", "/api/status", tok=u1).status_code == 200)
    expect("other session of that user ended", call("GET", "/api/status", tok=u2), 401, "UNAUTHORIZED")
    S.open_tokens.discard(u2)
    r, u3 = login(name, pw2)
    check("login with the new password", okr(r) and u3)
    expect("admin sets password of another user",
           call("POST", "/api/users/password", form={"user": name, "pass": pw3}), 200)
    expect("... BAD_PASSWORD for admin-set (4 bytes)",
           call("POST", "/api/users/password", form={"user": name, "pass": "abcd"}), 400, "BAD_PASSWORD")
    expect("... NOT_FOUND for unknown user",
           call("POST", "/api/users/password", form={"user": name + "zz", "pass": pw3}), 404, "NOT_FOUND")
    expect("user's sessions ended by admin password change (1)", call("GET", "/api/status", tok=u1), 401)
    expect("user's sessions ended by admin password change (2)", call("GET", "/api/status", tok=u3), 401)
    S.open_tokens.discard(u1)
    S.open_tokens.discard(u3)
    r, u4 = login(name, pw3)
    check("login with admin-set password", okr(r) and u4)

    # delete
    expect("delete user", call("POST", "/api/users/delete", params={"user": name}), 200)
    S.tmp_user = None
    expect("deleted user's session ended", call("GET", "/api/status", tok=u4), 401, "UNAUTHORIZED")
    S.open_tokens.discard(u4)
    users, _ = get_users()
    check("user no longer listed", not any(u["name"] == name for u in users))
    expect("delete again -> NOT_FOUND", call("POST", "/api/users/delete", params={"user": name}), 404, "NOT_FOUND")


def sec_devices(a):
    head("3. Trusted devices")
    r = call("GET", "/api/devices")
    j = jr(r)
    devs, mx = j.get("devices", []), j.get("max", 8)
    check("device list", r.status_code == 200 and isinstance(j.get("devices"), list) and mx == 8,
          f"{len(devs)} of {mx}")
    if len(devs) >= mx:
        skip("trusted device tests", "device table full (adding would displace a real device)")
        return
    r, d1 = login(a.user, a.password, remember=True, device="apitest")
    sc = r.headers.get("Set-Cookie", "")
    check("login remember=1", okr(r) and d1)
    check("cookie has Max-Age=31536000", "max-age=31536000" in sc.lower(), sc[-40:])
    r = call("GET", "/api/devices", tok=d1)
    mine = [d for d in jr(r).get("devices", []) if d.get("label") == "apitest" and d.get("current")]
    check("device listed with current=true", len(mine) == 1 and mine[0].get("user", "").lower() == a.user.lower()
          and re.fullmatch(r"[0-9a-f]{8}", mine[0].get("id", "")), mine)
    if not mine:
        logout(d1)
        return
    did = mine[0]["id"]
    S.device_ids.append(did)
    r = call("GET", "/api/devices")
    other = [d for d in jr(r).get("devices", []) if d.get("id") == did]
    check("main session sees it with current=false", len(other) == 1 and other[0].get("current") is False)
    expect("delete device", call("POST", "/api/devices/delete", params={"id": did}), 200)
    S.device_ids.remove(did)
    S.open_tokens.discard(d1)
    expect("device cookie no longer valid", call("GET", "/api/status", tok=d1), 401, "UNAUTHORIZED")
    expect("delete again -> NOT_FOUND", call("POST", "/api/devices/delete", params={"id": did}), 404, "NOT_FOUND")
    r, d2 = login(a.user, a.password, remember=True, device="apitest2")
    if d2:
        mine = [d for d in jr(call("GET", "/api/devices", tok=d2)).get("devices", []) if d.get("current")]
        if mine:
            S.device_ids.append(mine[0]["id"])
        expect("logout removes the trusted device", logout(d2), 200)
        left = [d for d in jr(call("GET", "/api/devices")).get("devices", []) if d.get("label") == "apitest2"]
        check("... and it is gone from the list", not left)
        if left:
            call("POST", "/api/devices/delete", params={"id": left[0]["id"]})
        S.device_ids.clear()
    else:
        check("second trusted login", False)


def sec_files(a):
    head("4. Mode and files")
    st = status()
    drive = st.get("drive", {})
    owner = drive.get("owner")
    host_active = drive.get("usb_connected") and not drive.get("host_ejected")
    if owner == "usb":
        if S.skip_destructive:
            skip("file tests", "drive belongs to the USB device and --skip-destructive is set")
            return
        r = mode("app")
        if not okr(r) and jcode(r) == "HOST_WRITING":
            time.sleep(S.guard + 1)
            r = mode("app")
        if not okr(r) and jcode(r) == "HOST_ACTIVE":
            check("take-over refused without confirmation (HOST_ACTIVE)", True)
            time.sleep(S.guard + 0.5)
            r = mode("app", True)
            for _ in range(4):
                if jcode(r) != "HOST_WRITING":
                    break
                time.sleep(S.guard + 1)
                r = mode("app", True)
        elif okr(r) and host_active:
            check("take-over refused without confirmation (HOST_ACTIVE)", False, "host connected, but accepted")
        check("take over the drive from USB", okr(r), r.text[:100])
        if not okr(r):
            return
        S.took = True
    check("owner is app", status().get("drive", {}).get("owner") == "app")

    rid = os.urandom(3).hex()
    T = S.T = f"_apitest_{rid}"
    NAME = "Würfel Test äöü.3mf"
    SUB, UNT = f"{T}/Ordner Ä", f"{T}/Ordner Ä/Unter"
    S.extra_paths.append(f"_apitest_escape_{rid}")

    # path validation (upload/check never creates anything)
    def chk(path, size=10):
        return call("POST", "/api/upload/check", params={"path": path, "size": str(size), "overwrite": "0"})
    deep = "/".join(["d"] * 9) + "/x.3mf"
    long_total = "/".join(["l" * 100] * 3) + "/x.3mf"
    for label, p in (("..", f"{T}/../x.3mf"), ("leading dot", f"{T}/.hidden.3mf"), ("backslash", f"{T}\\x.3mf"),
                     ("empty segment", f"{T}//x.3mf"), ("too deep (9 levels)", deep),
                     ("too long (>240 bytes)", long_total), ("segment > 120 bytes", f"{T}/{'s' * 121}.3mf"),
                     ("trailing dot", f"{T}/x.3mf."), ("forbidden character *", f"{T}/a*b.3mf"),
                     ("leading slash", f"/{T}/x.3mf")):
        expect(f"BAD_NAME for {label}", chk(p), 400, "BAD_NAME")
    expect("BAD_NAME for mkdir with '..'", call("POST", "/api/mkdir", params={"path": f"{T}/../_apitest_escape_{rid}"}),
           400, "BAD_NAME")

    # mkdir
    expect("mkdir test folder", call("POST", "/api/mkdir", params={"path": T}), 200)
    S.T_made = True
    expect("mkdir again -> EXISTS", call("POST", "/api/mkdir", params={"path": T}), 409, "EXISTS")
    expect("mkdir with missing parent -> NOT_FOUND", call("POST", "/api/mkdir", params={"path": f"{T}/nope/sub"}),
           404, "NOT_FOUND")
    expect("mkdir sub-folder", call("POST", "/api/mkdir", params={"path": SUB}), 200)
    expect("mkdir sub-sub-folder", call("POST", "/api/mkdir", params={"path": UNT}), 200)

    # upload
    drive = status().get("drive", {})
    data = make_3mf(a.size_kb)
    if drive.get("free", 1 << 40) < 6 * len(data) + (1 << 20):
        skip("upload tests", f"only {drive.get('free')} bytes free")
        return
    P = f"{T}/{NAME}"
    expect("upload/check ok", call("POST", "/api/upload/check", params={"path": P, "size": str(len(data))}), 200)
    expect("upload/check into missing folder -> NOT_FOUND",
           call("POST", "/api/upload/check", params={"path": f"{T}/nope/x.3mf", "size": "10"}), 404, "NOT_FOUND")
    expect("upload/check 5 GiB -> TOO_LARGE",
           call("POST", "/api/upload/check", params={"path": f"{T}/big.3mf", "size": str(5 * 1024 ** 3)}), 413, "TOO_LARGE")
    mtime = 1790000000
    t = time.time()
    r = upload(P, data, mtime=mtime)
    dt = max(time.time() - t, 1e-6)
    check("upload into test folder (umlauts, spaces)", okr(r),
          f"{len(data) / 1024:.0f} kB in {dt:.1f} s = {len(data) / 1024 / dt:.1f} kB/s" if okr(r) else r.text[:120])
    r, ents = ls(T)
    e = ents.get(NAME, {})
    check("listing: file with type=file and size", e.get("type") == "file" and e.get("size") == len(data), e)
    check("listing: mtime kept (FAT 2 s)", abs(e.get("mtime", 0) - mtime) <= 2, e.get("mtime"))
    check("listing: sub-folder has type=dir", ents.get("Ordner Ä", {}).get("type") == "dir")
    r = download(P)
    check("download identical (SHA-256)", r.status_code == 200 and sha(r.content) == sha(data))
    expect("download of a folder -> IS_DIR", download(SUB), 400, "IS_DIR")
    expect("download of missing file -> NOT_FOUND", download(f"{T}/gibtsnicht.3mf"), 404, "NOT_FOUND")
    expect("list of missing folder -> NOT_FOUND", ls(f"{T}/gibtsnicht")[0], 404, "NOT_FOUND")
    expect("upload into missing folder -> NOT_FOUND", upload(f"{T}/nope/x.3mf", make_3mf(1)), 404, "NOT_FOUND")

    # invalid files
    expect("non-ZIP .3mf refused (INVALID_FILE)", upload(f"{T}/kaputt.3mf", os.urandom(20000)), 422, "INVALID_FILE")
    expect("truncated ZIP refused (INVALID_FILE)", upload(f"{T}/abgeschnitten.3mf", make_3mf(64)[:-30]), 422,
           "INVALID_FILE")
    r, ents = ls(T)
    check("refused uploads left nothing behind", "kaputt.3mf" not in ents and "abgeschnitten.3mf" not in ents)

    # EXISTS / overwrite
    expect("upload onto existing file -> EXISTS", upload(P, data), 409, "EXISTS")
    expect("upload/check onto existing file -> EXISTS",
           call("POST", "/api/upload/check", params={"path": P, "size": "10", "overwrite": "0"}), 409, "EXISTS")
    expect("upload/check overwrite=1 ok",
           call("POST", "/api/upload/check", params={"path": P, "size": "10", "overwrite": "1"}), 200)
    data2 = make_3mf(32)
    r = upload(P, data2, overwrite=True)
    check("overwrite replaces content", okr(r) and sha(download(P).content) == sha(data2))

    # move
    R1, R2 = f"{T}/Umbenannt ÄÖ.3mf", f"{SUB}/Umbenannt ÄÖ.3mf"

    def move(f, t):
        return call("POST", "/api/move", params={"from": f, "to": t})
    expect("move: rename", move(P, R1), 200)
    r, ents = ls(T)
    check("... new name listed, old gone", "Umbenannt ÄÖ.3mf" in ents and NAME not in ents)
    expect("move: into sub-folder", move(R1, R2), 200)
    check("... content intact", sha(download(R2).content) == sha(data2))
    expect("upload second file", upload(f"{T}/zweite.3mf", make_3mf(1)), 200)
    expect("move onto existing -> EXISTS", move(f"{T}/zweite.3mf", R2), 409, "EXISTS")
    expect("move: case-only rename allowed", move(f"{T}/zweite.3mf", f"{T}/ZWEITE.3mf"), 200)
    r, ents = ls(T)
    check("... new case listed", "ZWEITE.3mf" in ents and "zweite.3mf" not in ents, list(ents))
    expect("move of missing source -> NOT_FOUND", move(f"{T}/gibtsnicht.3mf", f"{T}/x.3mf"), 404, "NOT_FOUND")
    expect("move into missing folder -> NOT_FOUND", move(f"{T}/ZWEITE.3mf", f"{T}/nope/x.3mf"), 404, "NOT_FOUND")
    expect("move to invalid name -> BAD_NAME", move(f"{T}/ZWEITE.3mf", f"{T}/a:b.3mf"), 400, "BAD_NAME")
    expect("move folder into itself -> BAD_NAME", move(SUB, f"{UNT}/Ordner Ä"), 400, "BAD_NAME")

    # copy
    def copy(f, t):
        return call("POST", "/api/copy", params={"from": f, "to": t}, timeout=60)
    expect("copy file", copy(R2, f"{T}/Kopie.3mf"), 200)
    check("... copy identical", sha(download(f"{T}/Kopie.3mf").content) == sha(data2))
    expect("copy onto existing -> EXISTS", copy(R2, f"{T}/Kopie.3mf"), 409, "EXISTS")
    expect("copy of missing source -> NOT_FOUND", copy(f"{T}/gibtsnicht.3mf", f"{T}/y.3mf"), 404, "NOT_FOUND")
    expect("copy folder into itself -> BAD_NAME", copy(SUB, f"{UNT}/x"), 400, "BAD_NAME")
    expect("copy folder (recursive)", copy(SUB, f"{T}/Ordner Kopie"), 200)
    r, ents = ls(f"{T}/Ordner Kopie")
    check("... copied folder has file and sub-folder", ents.get("Umbenannt ÄÖ.3mf", {}).get("type") == "file"
          and ents.get("Unter", {}).get("type") == "dir", list(ents))
    check("... copied file identical", sha(download(f"{T}/Ordner Kopie/Umbenannt ÄÖ.3mf").content) == sha(data2))
    check("... source folder untouched", "Umbenannt ÄÖ.3mf" in ls(SUB)[1])

    # delete
    def delete(p, rec=False):
        return call("POST", "/api/delete", params={"path": p, "recursive": "1" if rec else "0"})
    expect("delete non-empty folder -> DIR_NOT_EMPTY", delete(f"{T}/Ordner Kopie"), 409, "DIR_NOT_EMPTY")
    expect("delete folder recursive", delete(f"{T}/Ordner Kopie", True), 200)
    check("... folder gone", "Ordner Kopie" not in ls(T)[1])
    expect("delete file", delete(f"{T}/Kopie.3mf"), 200)
    expect("delete missing -> NOT_FOUND", delete(f"{T}/Kopie.3mf"), 404, "NOT_FOUND")

    # push in app mode
    pd = make_3mf(16)
    PP = f"{T}/push_app.3mf"
    r = upload(PP, pd, push=True)
    j = jr(r)
    check("push in app mode", okr(r) and j.get("took_over") is False and j.get("provided") is False, r.text[:120])
    check("... content identical", sha(download(PP).content) == sha(pd))
    pd2 = make_3mf(8)
    r = upload(PP, pd2, push=True)
    check("push overwrites by default", okr(r) and sha(download(PP).content) == sha(pd2))

    # aborted upload
    sock = socket.create_connection((S.host, S.port), timeout=10)
    total = 300 * 1024
    sock.sendall((f"POST /api/upload?path={quote(T + '/abbruch.3mf', safe='')} HTTP/1.1\r\nHost: {S.host}\r\n"
                  f"X-PrintDrive: 1\r\nCookie: dd_session={S.main}\r\nContent-Length: {total}\r\n"
                  f"Content-Type: application/octet-stream\r\n\r\n").encode())
    sock.sendall(make_3mf(100)[:100 * 1024])
    time.sleep(1.5)
    sock.close()          # the server handles one request at a time: it answers after the abort
    st = jr(call("GET", "/api/status", timeout=60))
    r, ents = ls(T)
    check("aborted upload cleaned up", not st.get("upload", {}).get("active") and "abbruch.3mf" not in ents,
          st.get("upload"))
    check("no temp file left in the test folder", not [n for n in ents if n.lower().endswith(".tmp")])

    # USB mode
    host_now = status().get("drive", {})
    host_busy = host_now.get("usb_connected") and not host_now.get("host_ejected")
    if S.skip_destructive:
        skip("USB-mode checks (WRONG_OWNER, take-over push)", "--skip-destructive")
    elif S.orig_owner != "usb" and host_busy:
        skip("USB-mode checks (WRONG_OWNER, take-over push)",
             "drive was owned by the app and a USB host is connected: not handing it over")
    else:
        r = mode("usb")
        check("provide drive to USB", okr(r) and status().get("drive", {}).get("owner") == "usb", r.text[:100])
        expect("WRONG_OWNER: list", call("GET", "/api/files", params={"dir": T}), 409, "WRONG_OWNER")
        expect("WRONG_OWNER: mkdir", call("POST", "/api/mkdir", params={"path": f"{T}/usbmode"}), 409, "WRONG_OWNER")
        expect("WRONG_OWNER: delete", delete(f"{T}/nichts.3mf"), 409, "WRONG_OWNER")
        expect("WRONG_OWNER: move", move(f"{T}/a.3mf", f"{T}/b.3mf"), 409, "WRONG_OWNER")
        expect("WRONG_OWNER: upload/check",
               call("POST", "/api/upload/check", params={"path": f"{T}/x.3mf", "size": "10"}), 409, "WRONG_OWNER")
        expect("WRONG_OWNER: download", download(PP), 409, "WRONG_OWNER")
        drv = status().get("drive", {})
        connected = drv.get("usb_connected") and not drv.get("host_ejected")
        data_u = make_3mf(16)
        PU = f"{T}/push_usb.3mf"
        r = upload(PU, data_u, push=True)
        if not okr(r) and jcode(r) == "HOST_WRITING":
            time.sleep(S.guard + 1)
            r = upload(PU, data_u, push=True)
        if connected:
            expect("push from USB mode without force -> HOST_ACTIVE", r, 409, "HOST_ACTIVE")
            time.sleep(S.guard + 0.5)
            r = upload(PU, data_u, push=True, force=True)
            for _ in range(3):
                if jcode(r) != "HOST_WRITING":
                    break
                time.sleep(S.guard + 1)
                r = upload(PU, data_u, push=True, force=True)
        j = jr(r)
        check("push takes over and provides again (took_over, provided)",
              okr(r) and j.get("took_over") is True and j.get("provided") is True, r.text[:120])
        check("... drive belongs to USB afterwards", status().get("drive", {}).get("owner") == "usb")
        if okr(r):
            S.pushed = (f"{T}/push_usb.3mf", data_u)
    S.files_ok = True


def sec_wifi(a):
    head("5. Wi-Fi")
    r = call("GET", "/api/wifi/list")
    j = jr(r)
    nets, mx = j.get("networks", []), j.get("max", 5)
    check("wifi list", r.status_code == 200 and isinstance(j.get("networks"), list) and
          all("ssid" in n and "active" in n for n in nets), f"{len(nets)} of {mx}")
    check("no passwords in the list", not re.search(r'"(pass|password|psk)"', r.text, re.I) and
          all(set(n) <= {"ssid", "active"} for n in nets))
    rid = os.urandom(3).hex()
    ssid = f"apitest_{rid}"
    expect("BAD_REQUEST for 5-character password",
           call("POST", "/api/wifi/add", form={"ssid": ssid, "pass": "abcde"}), 400, "BAD_REQUEST")
    expect("BAD_REQUEST for 64-character password",
           call("POST", "/api/wifi/add", form={"ssid": ssid, "pass": "p" * 64}), 400, "BAD_REQUEST")
    expect("BAD_REQUEST for 33-byte SSID",
           call("POST", "/api/wifi/add", form={"ssid": "s" * 33, "pass": ""}), 400, "BAD_REQUEST")
    expect("BAD_REQUEST for empty SSID", call("POST", "/api/wifi/add", form={"ssid": "", "pass": ""}), 400, "BAD_REQUEST")
    check("rejected adds saved nothing", len(jr(call("GET", "/api/wifi/list")).get("networks", [])) == len(nets))
    expect("delete of unknown SSID -> NOT_FOUND", call("POST", "/api/wifi/delete", params={"ssid": ssid}), 404,
           "NOT_FOUND")
    if not a.wifi_test:
        skip("add / list / delete of a dummy network", "opt-in with --wifi-test")
        return
    if len(nets) >= mx:
        skip("add / delete of a dummy network", "5 networks stored")
        return
    S.wifi_ssid = ssid
    r = call("POST", "/api/wifi/add", form={"ssid": ssid, "pass": "apitest-pw-123"}, retry=True)
    check("add dummy network", okr(r), r.text[:100])
    wait_online()
    nets2 = jr(call("GET", "/api/wifi/list", timeout=30)).get("networks", [])
    check("... listed, not active", any(n["ssid"] == ssid and n["active"] is False for n in nets2))
    r = call("POST", "/api/wifi/delete", params={"ssid": ssid}, timeout=30)
    check("delete dummy network", okr(r), r.text[:100])
    S.wifi_ssid = None
    wait_online()
    nets3 = jr(call("GET", "/api/wifi/list", timeout=30)).get("networks", [])
    check("... gone, list as before", [n["ssid"] for n in nets3] == [n["ssid"] for n in nets])


def sec_status(a):
    head("6. Status")
    st = status()
    print(f"       firmware {st.get('version')}, owner {st.get('drive', {}).get('owner')}, "
          f"types {st.get('types')!r}, free {st.get('drive', {}).get('free')}")
    check("status has version, drive, wifi, upload", all(k in st for k in ("version", "drive", "wifi", "upload")))
    check("types is a string", isinstance(st.get("types"), str) and st["types"] != "")
    ota = st.get("ota")
    check("ota object (partition, pending_verify)", isinstance(ota, dict) and isinstance(ota.get("partition"), str)
          and isinstance(ota.get("pending_verify"), bool), ota)
    if a.ota_probe:
        before = (ota or {}).get("partition")
        r = call("POST", "/api/ota", body=b"\x00" * 16, timeout=30, retry=False)
        check("ota probe: invalid body refused (INVALID_FILE / TOO_LARGE)", r.status_code in (422, 413)
              and jcode(r) in ("INVALID_FILE", "TOO_LARGE"), f"{r.status_code} {jcode(r)}")
        check("ota probe: nothing flashed (same partition)", status().get("ota", {}).get("partition") == before)
    else:
        skip("/api/ota probe", "opt-in with --ota-probe")


def sec_drive(a):
    head("7. Windows drive")
    if not a.drive:
        skip("drive letter check", "no --drive")
        return
    if not S.pushed:
        skip("drive letter check", "no file was provided to USB by this test")
        return
    d = a.drive.rstrip("\\/")
    d = d + ":" if len(d) == 1 else d
    path = os.path.join(d + os.sep, *S.pushed[0].split("/"))
    for _ in range(60):
        if os.path.exists(path):
            break
        time.sleep(0.5)
    good = False
    if os.path.exists(path):
        with open(path, "rb") as f:
            good = f.read() == S.pushed[1]
    check("pushed file visible and identical on the Windows drive", good, path)


# ------------------------------------------------------------------ cleanup

def cleanup(a):
    head("Cleanup")
    if S.main is None:
        note("no main session, nothing to clean up")
        return
    try:
        if call("GET", "/api/status", timeout=10).status_code != 200:
            note("main session no longer valid, cannot clean up through the API")
            return
        if S.T_made or S.extra_paths:
            if ensure_app():
                if S.T_made:
                    r = call("POST", "/api/delete", params={"path": S.T, "recursive": "1"}, timeout=60)
                    check("test folder removed", okr(r) or r.status_code == 404, r.text[:100])
                    check("... not in the root listing", S.T not in ls("")[1])
                for p in S.extra_paths:
                    call("POST", "/api/delete", params={"path": p, "recursive": "1"})
            else:
                check("cleanup: take drive back to remove the test folder", False,
                      f"remove {S.T} manually")
        if S.tmp_user or S.extra_users:
            for u in ([S.tmp_user] if S.tmp_user else []) + S.extra_users:
                call("POST", "/api/users/delete", params={"user": u})
            users, _ = get_users()
            names = {u["name"].lower() for u in users}
            check("temporary users removed", not any(x and x.lower() in names for x in [S.tmp_user] + S.extra_users))
        for did in S.device_ids:
            call("POST", "/api/devices/delete", params={"id": did})
        for d in jr(call("GET", "/api/devices")).get("devices", []):
            if d.get("label", "").startswith("apitest") and not d.get("current"):
                call("POST", "/api/devices/delete", params={"id": d["id"]})
        left = [d for d in jr(call("GET", "/api/devices")).get("devices", []) if d.get("label", "").startswith("apitest")]
        check("test devices removed", not left)
        if S.wifi_ssid:
            call("POST", "/api/wifi/delete", params={"ssid": S.wifi_ssid}, timeout=30)
            wait_online()
        if S.orig_owner == "usb":
            r = mode("usb")
            check("drive provided to USB again (original state)", okr(r) or status().get("drive", {}).get("owner") == "usb")
        elif S.orig_owner == "app":
            check("drive owner app (original state)", ensure_app())
    except requests.RequestException as e:
        check("cleanup reachable", False, repr(e))
    for t in list(S.open_tokens):
        if t != S.main:
            try:
                logout(t)
            except requests.RequestException:
                pass
    try:
        r = logout(S.main)
        check("own session logged out", r.status_code == 200)
    except requests.RequestException:
        pass


def main():
    ap = argparse.ArgumentParser(description="End-to-end test of the WiFi DataDrive API v3")
    ap.add_argument("host", help="HOST or HOST:PORT")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", required=True)
    ap.add_argument("--drive", help="Windows drive letter of the dongle, e.g. E:")
    ap.add_argument("--size-kb", type=int, default=512)
    ap.add_argument("--skip-destructive", action="store_true",
                    help="do not take the drive from USB / switch the mode")
    ap.add_argument("--wifi-test", action="store_true", help="add and remove a dummy Wi-Fi network")
    ap.add_argument("--ota-probe", action="store_true", help="POST a tiny invalid body to /api/ota")
    a = ap.parse_args()
    S.host, _, p = a.host.partition(":")
    S.port = int(p) if p else 80
    S.base = f"http://{S.host}:{S.port}"
    S.user, S.password, S.skip_destructive = a.user, a.password, a.skip_destructive
    code = 0
    try:
        for fn in (sec_auth, sec_users, sec_devices, sec_files, sec_wifi, sec_status, sec_drive):
            try:
                fn(a)
            except Abort:
                raise
            except Exception as e:      # a broken section must not hide the others or skip the cleanup
                check(f"{fn.__name__} ran without exception", False, repr(e))
    except Abort as e:
        print(f"\nSTOP: {e}")
        code = 2
    finally:
        try:
            cleanup(a)
        except Exception as e:
            check("cleanup ran without exception", False, repr(e))
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed, {len(skipped)} skipped")
    for n in failed:
        print(f"  FAILED: {n}")
    sys.exit(code or (1 if failed else 0))


if __name__ == "__main__":
    main()
