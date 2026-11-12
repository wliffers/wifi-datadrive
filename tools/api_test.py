"""End-to-end test of the WiFi DataDrive web API (version 3, docs/09_api_v3.md, plus version 4,
docs/12_api_v4.md: Wi-Fi scan and drive encryption incl. recovery key renewal, erase, damaged state,
plus version 5, docs/15_api_v5.md: clock, automatic lock, display language, resumable upload,
folder ZIP, WebDAV).

Works against the mock (tools/mock_server.py) and a real dongle.

Usage (the ESP-IDF Python env and the system Python provide 'requests'):
    python tools/api_test.py HOST[:PORT] --user admin --password PW
                             [--drive E:] [--size-kb 512] [--skip-destructive]
                             [--wifi-test] [--ota-probe] [--drive-password PW]
                             [--crypt-test --i-know-this-erases-the-drive [--keep-encrypted]]
                             [--only SECTION[,SECTION...]]

Sections: 1 authentication, 2 users and roles, 3 trusted devices, 4 mode and files,
5 Wi-Fi, 6 status (+ optional OTA probe), 7 Windows drive letter (--drive),
8 Wi-Fi scan (v4), 9 drive encryption, non-destructive (v4), 9c optional HTTPS (read-only checks),
10 drive encryption, destructive (v4, opt-in),
11 clock and time zone (v5), 12 automatic lock settings (v5), 13 display language (v5),
14 resumable upload (v5), 15 folder as ZIP (v5), 16 WebDAV smoke test (v5; skipped if /dav/ answers 404).

  --only time,zip     run only these sections (after the login, which always runs). Names: users, devices,
                      files, wifi, status, drive, scan, crypt, https, crypt_test, time, autolock, lang, resume,
                      zip, webdav; "v5" = time,autolock,lang,resume,zip,webdav. The v5 file sections create their
                      own test folder if section 4 does not run.

The target is left as found: owner mode restored, everything created (test folder
_apitest_<random>, temporary user apitest_<random>, trusted devices, Wi-Fi entry) removed,
changed settings (automatic lock, display language) restored, own sessions logged out. Never
formats, reboots or flashes; only touches what it created. Section 11 may set the device clock
to this PC's time if the device clock is not NTP-synced (like the web UI does).

Options
  --skip-destructive  do not take the drive from a USB host and do not switch the mode
                      (file sections only run if the file manager already owns the drive;
                      the USB-mode checks and the take-over push are skipped).
  --wifi-test         also add and remove a dummy Wi-Fi network (a real device tries to
                      reconnect after an add, so this is opt-in).
  --ota-probe         POST a 16 byte invalid body to /api/ota (expects INVALID_FILE).
  --drive E:          after providing the drive to USB, compare the pushed file on the
                      Windows drive letter.

  --drive-password PW the drive password. Needed if the drive is found encrypted and locked
                      (the test then stops with a message without it; with it the test unlocks
                      the drive first and locks it again at the end). Never printed.
  --crypt-test        DESTRUCTIVE, needs --i-know-this-erases-the-drive and --drive-password:
                      formats the drive (ALL FILES LOST), sets up encryption with PW, runs
                      round trip / lock / wrong passwords + throttle (waits the Retry-After) /
                      unlock with password and recovery key / password change (unlocked only;
                      forgotten password: recovery key as `old`) / new recovery key / format on
                      the encrypted drive (no restart) / disable, then a second setup + lock +
                      POST /api/crypt/erase. The drive ends plain and working. Setup erases the
                      whole medium (up to 2 minutes each). Only for a drive that is not encrypted yet.
                      A drive found in state "damaged" cannot be produced on a real device: if the
                      target reports damaged:true (mock scenario crypt_damaged) only the damaged-state
                      section runs; with --crypt-test the erase is performed and the destructive
                      section follows on the now plain drive.
  --keep-encrypted    with --crypt-test: stop before disable; drive stays encrypted and unlocked
                      (the recovery key is printed once, record it).

Wrong passwords (login): at most 3 (one wrong login, one wrong `old` password, one wrong WebDAV
Basic password), never two in a row, so the lockout (5 in a row) of a real device is never reached. If the account still has the delivery
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
import zlib
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
    role_tok = None     # None: not tried, False: not possible, str: token of a temporary user
    relock = False      # drive was locked at the start: lock it again at the end
    crypt_pw = None     # current drive password (for the cleanup)
    crypt_by_test = False
    keep_encrypted = False
    recovery = None
    damaged = False     # the target reports crypt.damaged (only the mock can)
    autolock_orig = None    # (minutes, on_detach) to restore
    lang_orig = None        # display language to restore


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
    p = {"path": path, "now": str(int(time.time()))}       # API v5: Unix seconds (UTC)
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


def cpost(path, form=None, tok=DEF, tries=4, **kw):
    """POST to a crypt endpoint (long timeout: key derivation, formatting); HOST_WRITING is retried."""
    kw.setdefault("timeout", 330)      # setup/erase wipe the whole medium (up to ~2 min)
    r = None
    for i in range(tries):
        r = call("POST", path, form=form, tok=tok, **kw)
        if jcode(r) == "HOST_WRITING" and i < tries - 1:
            time.sleep(S.guard + 1)
            continue
        break
    return r


def locked_reqs_f(T2, P):
    """Requests that must be refused while the drive is locked (or damaged)."""
    return [
        ("GET files", "GET", "/api/files", {"dir": ""}, None),
        ("GET download", "GET", "/api/download", {"path": P}, None),
        ("POST mode", "POST", "/api/mode", {"target": "app"}, None),
        ("POST mkdir", "POST", "/api/mkdir", {"path": f"{T2}/x"}, None),
        ("POST delete", "POST", "/api/delete", {"path": P, "recursive": "0"}, None),
        ("POST move", "POST", "/api/move", {"from": P, "to": f"{T2}/y.3mf"}, None),
        ("POST copy", "POST", "/api/copy", {"from": P, "to": f"{T2}/y.3mf"}, None),
        ("POST upload/check", "POST", "/api/upload/check", {"path": f"{T2}/z.3mf", "size": "10"}, None),
        ("POST upload", "POST", "/api/upload", {"path": f"{T2}/z.3mf"}, make_3mf(1)),
        ("POST push", "POST", "/api/push", {"path": f"{T2}/z.3mf"}, make_3mf(1)),
        ("POST format", "POST", "/api/format", None, None),
        ("GET zip (v5)", "GET", "/api/zip", {"path": T2}, None),
    ]


def crypt_state():
    r = call("GET", "/api/crypt")
    j = jr(r)
    if r.status_code != 200:
        return None, None
    return j.get("enabled"), j.get("unlocked")


def role_token():
    """Token of a temporary user with role 'user' (created once, removed by the cleanup) or None."""
    if S.role_tok is not None:
        return S.role_tok or None
    S.role_tok = False
    users, mx = get_users()
    if len(users) >= mx:
        return None
    name = f"apitest_v4_{os.urandom(3).hex()}"
    pw = "Pw-" + os.urandom(5).hex()
    S.extra_users.append(name)
    if not okr(call("POST", "/api/users/add", form={"user": name, "pass": pw, "role": "user"})):
        return None
    r, tok = login(name, pw)
    if tok and okr(r):
        S.role_tok = tok
    return tok if S.role_tok else None


def crypt_preflight(a):
    """Right after the login: an encrypted and locked drive stops the test unless --drive-password."""
    r = call("GET", "/api/crypt")
    j = jr(r)
    if r.status_code != 200 or not (j.get("enabled") and j.get("unlocked") is False):
        return
    if j.get("damaged") is True:
        S.damaged = True
        print("note: the drive reports crypt.damaged (key area unreadable): only the damaged-state checks run", flush=True)
        return
    if not a.drive_password:
        raise Abort("The drive is encrypted and locked. Nothing was changed. Run the test again with "
                    "--drive-password PW (it unlocks the drive first and locks it again at the end).")
    r = cpost("/api/crypt/unlock", {"password": a.drive_password})
    if not okr(r):
        raise Abort(f"Unlocking the drive with --drive-password failed ({r.status_code} {jcode(r)}). "
                    "Nothing else was changed.")
    S.relock = True
    S.crypt_pw = a.drive_password
    check("drive was locked: unlocked with --drive-password (locked again at the end)", crypt_state() == (True, True))


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
    crypt_preflight(a)
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
        expect("WRONG_OWNER: zip (v5)", call("GET", "/api/zip", params={"path": T}), 409, "WRONG_OWNER")
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


RECOVERY_RE = re.compile(r"[0-9ABCDEFGHJKMNPQRSTVWXYZ]{4}(-[0-9ABCDEFGHJKMNPQRSTVWXYZ]{4}){7}")


def sec_scan(a):
    head("8. Wi-Fi scan (API v4)")
    expect("401 without login: GET /api/wifi/scan", call("GET", "/api/wifi/scan", tok=None), 401, "UNAUTHORIZED")
    tok = role_token()
    if tok:
        expect("FORBIDDEN_ROLE for user: GET wifi/scan", call("GET", "/api/wifi/scan", tok=tok), 403, "FORBIDDEN_ROLE")
    else:
        skip("role check for wifi/scan", "no temporary user possible")
    saved_before = [n["ssid"] for n in jr(call("GET", "/api/wifi/list")).get("networks", [])]
    def wifi_now():       # without rssi (varies from call to call)
        return {k: v for k, v in (status().get("wifi") or {}).items() if k != "rssi"}
    wifi_before = wifi_now()
    r, dt = None, 0.0
    for _ in range(4):
        t = time.time()
        r = call("GET", "/api/wifi/scan", timeout=40)
        dt = time.time() - t
        if r.status_code == 409 and jcode(r) == "BUSY":
            note("scan answered BUSY, retrying in 3 s")
            time.sleep(3)
            continue
        break
    if r.status_code == 409 and jcode(r) == "BUSY":
        skip("scan result checks", "still BUSY after 4 attempts")
        return
    j = jr(r)
    nets = j.get("networks")
    check("scan answers 200 with ok and a list", r.status_code == 200 and j.get("ok") is True and isinstance(nets, list),
          f"{len(nets) if isinstance(nets, list) else '?'} networks in {dt:.1f} s")
    if not isinstance(nets, list):
        return
    check("at most 20 networks", len(nets) <= 20, len(nets))
    check("every entry: ssid (non-empty string), rssi (negative int), secure, saved (bool)",
          all(isinstance(n.get("ssid"), str) and n["ssid"] != "" and isinstance(n.get("rssi"), int)
              and not isinstance(n.get("rssi"), bool) and n["rssi"] < 0 and isinstance(n.get("secure"), bool)
              and isinstance(n.get("saved"), bool) for n in nets), nets[:2])
    check("no unknown fields in entries", all(set(n) <= {"ssid", "rssi", "secure", "saved"} for n in nets))
    rs = [n.get("rssi", 0) for n in nets]
    check("sorted by rssi, strongest first", rs == sorted(rs, reverse=True), rs)
    names = [n.get("ssid") for n in nets]
    check("every SSID appears once", len(names) == len(set(names)))
    check("saved flag matches /api/wifi/list", all(n.get("saved") is (n.get("ssid") in saved_before) for n in nets))
    check("scan changed nothing: saved networks unchanged",
          [n["ssid"] for n in jr(call("GET", "/api/wifi/list")).get("networks", [])] == saved_before)
    check("scan changed nothing: connection unchanged", wifi_now() == wifi_before)


def sec_crypt(a):
    head("9. Drive encryption, non-destructive (API v4)")
    r = call("GET", "/api/crypt")
    j = jr(r)
    enabled, unlocked = j.get("enabled"), j.get("unlocked")
    check("GET /api/crypt: ok, enabled, unlocked and damaged are booleans, retry_after an integer >= 0",
          r.status_code == 200 and j.get("ok") is True and isinstance(enabled, bool) and isinstance(unlocked, bool)
          and isinstance(j.get("damaged"), bool) and isinstance(j.get("retry_after"), int)
          and not isinstance(j.get("retry_after"), bool) and j.get("retry_after") >= 0, j)
    if not isinstance(enabled, bool):
        return
    check("no wait pending and not damaged (retry_after 0, damaged false)", j.get("retry_after") == 0 and j.get("damaged") is False, j)
    check("state is plain or unlocked (locked was handled at the start)", unlocked is True, j)
    cs = status().get("crypt")
    check("status.crypt = {enabled, unlocked, damaged} and equals /api/crypt",
          isinstance(cs, dict) and cs.get("enabled") is enabled and cs.get("unlocked") is unlocked
          and cs.get("damaged") is False, cs)
    print(f"       drive encryption: {'enabled, unlocked' if enabled else 'not enabled'}")
    expect("401 without login: GET /api/crypt", call("GET", "/api/crypt", tok=None), 401, "UNAUTHORIZED")

    harmless = [("setup", {"password": "short"}), ("unlock", {"password": "x" * 12}), ("lock", None),
                ("password", {"old": "x"}), ("disable", {}), ("recovery", {"password": "x"}),
                ("erase", {"confirm": "ERASE"})]
    for name, form in harmless:
        expect(f"401 without login: POST crypt/{name}",
               call("POST", f"/api/crypt/{name}", form=form, tok=None), 401, "UNAUTHORIZED")
    for name, form in harmless:
        expect(f"403 FORBIDDEN without X-PrintDrive header: POST crypt/{name}",
               call("POST", f"/api/crypt/{name}", form=form, csrf=False), 403, "FORBIDDEN")
    check("... and the state did not change", crypt_state() == (enabled, unlocked))
    if enabled and crypt_state()[1] is False and a.drive_password:
        cpost("/api/crypt/unlock", {"password": a.drive_password})       # a refused lock must not have locked it

    tok = role_token()
    if tok:
        check("user may read GET /api/crypt", call("GET", "/api/crypt", tok=tok).status_code == 200)
        for name, form in (("setup", {"password": "short"}), ("password", {"old": "x"}), ("disable", {}),
                           ("recovery", {"password": "x"}), ("erase", {"confirm": "ERASE"})):
            expect(f"FORBIDDEN_ROLE for user: POST crypt/{name}",
                   call("POST", f"/api/crypt/{name}", form=form, tok=tok), 403, "FORBIDDEN_ROLE")
        if enabled:
            r = call("POST", "/api/crypt/unlock", form={"password": "x" * 12}, tok=tok)
            check("user may call crypt/unlock (already unlocked: 200 without check)", okr(r), r.text[:100])
        else:
            expect("user may call crypt/unlock (CRYPT_NOT_ENABLED, not a role error)",
                   call("POST", "/api/crypt/unlock", form={"password": "x" * 12}, tok=tok), 409, "CRYPT_NOT_ENABLED")
            expect("user may call crypt/lock (CRYPT_NOT_ENABLED, not a role error)",
                   call("POST", "/api/crypt/lock", tok=tok), 409, "CRYPT_NOT_ENABLED")
    else:
        skip("role checks for the crypt endpoints", "no temporary user possible")

    weak = [("11 characters", "elf-zeichen"), ("11 Unicode characters (22 bytes)", "\u00e4" * 11), ("1 character", "x")]
    for label, pw in weak:
        r = call("POST", "/api/crypt/setup", form={"password": pw})
        if enabled:       # order of the checks is not specified: weak password or already enabled, never success
            check(f"setup with weak password refused ({label})",
                  (r.status_code, jcode(r)) in ((400, "CRYPT_WEAK_PASSWORD"), (409, "CRYPT_ALREADY_ENABLED")),
                  f"{r.status_code} {jcode(r)}")
        else:
            expect(f"setup with weak password -> CRYPT_WEAK_PASSWORD ({label})", r, 400, "CRYPT_WEAK_PASSWORD")
    expect("setup without password -> BAD_REQUEST", call("POST", "/api/crypt/setup", form={}), 400, "BAD_REQUEST")
    check("... nothing was set up or changed", crypt_state() == (enabled, unlocked))

    # erase: the confirmation is checked first; unlocked drives and plain drives are refused (nothing is erased)
    expect("erase without confirm -> BAD_REQUEST", call("POST", "/api/crypt/erase", form={}), 400, "BAD_REQUEST")
    expect("erase with confirm=erase (wrong case) -> BAD_REQUEST",
           call("POST", "/api/crypt/erase", form={"confirm": "erase"}), 400, "BAD_REQUEST")
    expect("recovery without password -> BAD_REQUEST", call("POST", "/api/crypt/recovery", form={}), 400, "BAD_REQUEST")
    if enabled:
        expect("erase while unlocked -> CRYPT_ALREADY_ENABLED (only locked or damaged)",
               call("POST", "/api/crypt/erase", form={"confirm": "ERASE"}), 409, "CRYPT_ALREADY_ENABLED")
    else:
        expect("erase while not encrypted -> CRYPT_NOT_ENABLED",
               call("POST", "/api/crypt/erase", form={"confirm": "ERASE"}), 409, "CRYPT_NOT_ENABLED")
        expect("recovery while not encrypted -> CRYPT_NOT_ENABLED",
               call("POST", "/api/crypt/recovery", form={"password": "x" * 12}), 409, "CRYPT_NOT_ENABLED")
    check("... nothing was erased or changed", crypt_state() == (enabled, unlocked))

    if enabled:
        skip("CRYPT_NOT_ENABLED checks (unlock / lock / password / disable)", "encryption is enabled on this drive")
        return
    pw12 = "Pruefen-" + os.urandom(3).hex()          # valid length, never set anywhere
    expect("unlock while not enabled -> CRYPT_NOT_ENABLED",
           call("POST", "/api/crypt/unlock", form={"password": pw12}), 409, "CRYPT_NOT_ENABLED")
    expect("unlock with recovery key while not enabled -> CRYPT_NOT_ENABLED",
           call("POST", "/api/crypt/unlock", form={"recovery_key": "7K3M-Q9XD-4WTB-HN2C-5RYE-8FGA-ZP6V-JC3S"}),
           409, "CRYPT_NOT_ENABLED")
    expect("lock while not enabled -> CRYPT_NOT_ENABLED", call("POST", "/api/crypt/lock"), 409, "CRYPT_NOT_ENABLED")
    expect("password change while not enabled -> CRYPT_NOT_ENABLED",
           call("POST", "/api/crypt/password", form={"old": pw12, "new": pw12 + "-neu"}), 409, "CRYPT_NOT_ENABLED")
    expect("disable while not enabled -> CRYPT_NOT_ENABLED",
           call("POST", "/api/crypt/disable", form={"password": pw12}), 409, "CRYPT_NOT_ENABLED")
    check("... the drive is still plain and unlocked", crypt_state() == (False, True))


def sec_crypt_destructive(a):
    head("10. Drive encryption, destructive (--crypt-test)")
    pw = a.drive_password
    enabled, unlocked = crypt_state()
    if enabled is not False:
        skip("destructive encryption test", "the drive is already encrypted (or /api/crypt missing): needs a plain drive")
        return
    if not ensure_app():
        check("destructive test: file manager owns the drive", False, "cannot take the drive over")
        return
    rid = os.urandom(3).hex()
    T2 = f"_apitest_crypt_{rid}"
    S.extra_paths.append(T2)
    new_pw = "Neu-" + os.urandom(8).hex()
    u = role_token()

    # setup
    expect("setup with 65-byte password -> BAD_REQUEST", cpost("/api/crypt/setup", {"password": "p" * 65}), 400, "BAD_REQUEST")
    check("... still plain", crypt_state() == (False, True))
    r = cpost("/api/crypt/setup", {"password": pw})
    j = jr(r)
    key = j.get("recovery_key", "")
    ok = check("setup: 200 with recovery_key", okr(r) and j.get("code") == "OK" and isinstance(key, str), f"{r.status_code} {jcode(r)}")
    if not ok:
        return
    S.crypt_by_test, S.crypt_pw, S.recovery = True, pw, key
    check("recovery key has 8 groups of 4 (0-9 A-Z without I L O U)", RECOVERY_RE.fullmatch(key) is not None,
          "(format wrong)" if not RECOVERY_RE.fullmatch(key) else "")
    check("state: enabled and unlocked; status.crypt the same", crypt_state() == (True, True)
          and status().get("crypt") == {"enabled": True, "unlocked": True, "damaged": False})
    d = status().get("drive", {})
    check("drive owner app, fs_ok after setup", d.get("owner") == "app" and d.get("fs_ok") is True, d)
    expect("setup again -> CRYPT_ALREADY_ENABLED", cpost("/api/crypt/setup", {"password": pw}), 409, "CRYPT_ALREADY_ENABLED")
    if a.keep_encrypted:
        note(f"recovery key (record it, shown only here): {key}")

    # round trip
    data = make_3mf(min(a.size_kb, 256))
    P = f"{T2}/Würfel äöü.3mf"
    expect("encrypted: mkdir", call("POST", "/api/mkdir", params={"path": T2}), 200)
    check("encrypted: upload", okr(upload(P, data)))
    r, ents = ls(T2)
    check("encrypted: listing shows the file", ents.get("Würfel äöü.3mf", {}).get("size") == len(data), list(ents))
    check("encrypted: download identical (SHA-256)", sha(download(P).content) == sha(data))

    # lock
    expect("lock (as role user)" if u else "lock", cpost("/api/crypt/lock", tok=u or DEF), 200)
    check("locked: /api/crypt and status.crypt say enabled / not unlocked", crypt_state() == (True, False)
          and status().get("crypt") == {"enabled": True, "unlocked": False, "damaged": False})
    d = status().get("drive", {})
    check("locked: drive owner none, fs_ok false, needs_format false, total/free/files 0",
          d.get("owner") == "none" and d.get("fs_ok") is False and d.get("needs_format") is False
          and d.get("total") == 0 and d.get("free") == 0 and d.get("files") == 0, d)
    expect("lock again (already locked) -> 200", cpost("/api/crypt/lock"), 200)
    locked_reqs = locked_reqs_f(T2, P)
    for label, m, path, params, body in locked_reqs:
        expect(f"423 DRIVE_LOCKED: {label}", call(m, path, params=params, body=body), 423, "DRIVE_LOCKED")
    expect("401 before DRIVE_LOCKED without login: GET files", call("GET", "/api/files", params={"dir": ""}, tok=None),
           401, "UNAUTHORIZED")
    if u:
        expect("403 FORBIDDEN_ROLE before DRIVE_LOCKED: format as user", call("POST", "/api/format", tok=u),
               403, "FORBIDDEN_ROLE")
    reach = [call("GET", p).status_code for p in ("/api/status", "/api/session", "/api/users", "/api/devices",
                                                  "/api/wifi/list", "/api/log", "/api/crypt")]
    check("locked: status, session, users, devices, wifi, log, crypt still reachable", reach == [200] * 7, reach)

    # wrong passwords: one global counter, the first two are free, then a wait (5 s, doubling)
    wrong = "falsch-" + os.urandom(4).hex() + "-xx"
    codes, retry = [], None
    for i in range(5):
        r = cpost("/api/crypt/unlock", {"password": wrong + str(i)})
        codes.append((r.status_code, jcode(r)))
        if r.status_code == 429:
            retry = r.headers.get("Retry-After")
            break
    check("first two wrong passwords -> 403 CRYPT_WRONG_PASSWORD (free)",
          codes[:2] == [(403, "CRYPT_WRONG_PASSWORD")] * 2, codes)
    check("then 429 LOCKED at the third attempt (or the fourth, if the third is still checked)",
          codes[-1] == (429, "LOCKED") and 3 <= len(codes) <= 4, codes)
    wait = int(retry) if retry and retry.isdigit() else 0
    check("429 carries Retry-After (seconds, 1..300)", 0 < wait <= 300, retry)
    j = jr(call("GET", "/api/crypt"))
    check("GET /api/crypt during the wait: retry_after > 0 and <= Retry-After", 0 < j.get("retry_after", 0) <= max(wait, 1), j)
    r = cpost("/api/crypt/unlock", {"password": pw})
    expect("correct password during the wait is not checked -> 429", r, 429, "LOCKED")
    check("... still locked", crypt_state() == (True, False))
    wait = max(wait, int(r.headers.get("Retry-After", "0") or 0))
    note(f"waiting {wait + 1} s (Retry-After)")
    time.sleep(wait + 1)
    check("after the wait retry_after is 0 again", jr(call("GET", "/api/crypt")).get("retry_after") == 0)

    # unlock with password; data survived
    r = cpost("/api/crypt/unlock", {"password": pw})
    check("unlock with the password after the wait", okr(r), f"{r.status_code} {jcode(r)}")
    check("unlocked again; owner usb or app", crypt_state() == (True, True)
          and status().get("drive", {}).get("owner") in ("usb", "app"))
    check("unlock when already unlocked -> 200 without check",
          okr(cpost("/api/crypt/unlock", {"password": "egal-" + os.urandom(4).hex() + "-xx"})))
    if not ensure_app():
        check("file manager owns the drive again", False)
        return
    check("after unlock: file intact (SHA-256)", sha(download(P).content) == sha(data))

    # unlock with the recovery key (lower case, no dashes)
    expect("lock before recovery unlock", cpost("/api/crypt/lock"), 200)
    r = cpost("/api/crypt/unlock", {"recovery_key": key.replace("-", "").lower()}, tok=u or DEF)
    check("unlock with the recovery key (lower case, no dashes)" + (" as user" if u else ""), okr(r),
          f"{r.status_code} {jcode(r)}")
    check("... unlocked, file intact", crypt_state() == (True, True) and ensure_app() and sha(download(P).content) == sha(data))

    # password change (only with an unlocked drive); forgotten password: recovery key as `old`
    expect("change password: weak new -> CRYPT_WEAK_PASSWORD",
           cpost("/api/crypt/password", {"old": pw, "new": "kurz"}), 400, "CRYPT_WEAK_PASSWORD")
    r = cpost("/api/crypt/password", {"old": pw, "new": new_pw})
    check("change password (unlocked)", okr(r), f"{r.status_code} {jcode(r)}")
    if okr(r):
        S.crypt_pw = new_pw
    check("... state unchanged (unlocked), no recovery key in the answer",
          crypt_state() == (True, True) and "recovery_key" not in jr(r))
    expect("lock", cpost("/api/crypt/lock"), 200)
    r = cpost("/api/crypt/unlock", {"password": pw})
    expect("old password no longer works -> CRYPT_WRONG_PASSWORD", r, 403, "CRYPT_WRONG_PASSWORD")
    expect("change password while locked -> 423 DRIVE_LOCKED",
           cpost("/api/crypt/password", {"old": new_pw, "new": pw}), 423, "DRIVE_LOCKED")
    expect("new recovery key while locked -> 423 DRIVE_LOCKED",
           cpost("/api/crypt/recovery", {"password": new_pw}), 423, "DRIVE_LOCKED")
    check("... state unchanged (locked)", crypt_state() == (True, False))
    r = cpost("/api/crypt/unlock", {"recovery_key": key})
    check("forgotten password: unlock with the recovery key (still valid after the password change)", okr(r),
          f"{r.status_code} {jcode(r)}")
    r = cpost("/api/crypt/password", {"old": key, "new": pw})
    check("change password with old = recovery key (back to the original)", okr(r), f"{r.status_code} {jcode(r)}")
    if okr(r):
        S.crypt_pw = pw
    r = cpost("/api/crypt/lock")
    r = cpost("/api/crypt/unlock", {"password": pw})
    check("unlock with the password after the change", okr(r) and crypt_state() == (True, True))
    if not ensure_app():
        check("file manager owns the drive again", False)
        return
    check("... file still intact", sha(download(P).content) == sha(data))

    # new recovery key (drive unlocked): old key stops working, new key unlocks
    expect("recovery without password -> BAD_REQUEST", cpost("/api/crypt/recovery", {}), 400, "BAD_REQUEST")
    r = cpost("/api/crypt/recovery", {"password": pw})
    key2 = jr(r).get("recovery_key", "")
    check("new recovery key (confirmed with the drive password): 200, valid format, different",
          okr(r) and RECOVERY_RE.fullmatch(key2) is not None and key2 != key, f"{r.status_code} {jcode(r)}")
    if not RECOVERY_RE.fullmatch(key2 or ""):
        return
    S.recovery = key2
    check("... state unchanged (unlocked)", crypt_state() == (True, True))
    r = cpost("/api/crypt/recovery", {"password": key2.lower()})
    key3 = jr(r).get("recovery_key", "")
    check("another new key confirmed with the current recovery key (lower case)",
          okr(r) and RECOVERY_RE.fullmatch(key3) is not None and key3 not in (key, key2), f"{r.status_code} {jcode(r)}")
    if not RECOVERY_RE.fullmatch(key3 or ""):
        return
    S.recovery = key3
    expect("lock", cpost("/api/crypt/lock"), 200)
    r = cpost("/api/crypt/unlock", {"recovery_key": key2})
    expect("the previous recovery key no longer works -> CRYPT_WRONG_PASSWORD", r, 403, "CRYPT_WRONG_PASSWORD")
    r = cpost("/api/crypt/unlock", {"recovery_key": key3})
    check("the newest recovery key unlocks", okr(r) and crypt_state() == (True, True), f"{r.status_code} {jcode(r)}")
    key = key3
    if not ensure_app():
        check("file manager owns the drive again", False)
        return

    # format on the encrypted, unlocked drive: no restart, stays encrypted and unlocked
    r = call("POST", "/api/format", params={"confirm": "FORMAT"}, timeout=120)
    check("format on the encrypted drive: 200 {ok, restart:false}", okr(r) and jr(r).get("restart") is False,
          f"{r.status_code} {r.text[:80]}")
    check("... still encrypted and unlocked, no restart", crypt_state() == (True, True))
    check("... file system new: test folder gone, owner app, fs_ok",
          T2 not in ls("")[1] and status().get("drive", {}).get("fs_ok") is True)

    if a.keep_encrypted:
        note("--keep-encrypted: stopping before disable; the drive stays encrypted and unlocked")
        note(f"recovery key (record it, shown only here): {key}")
        return

    # disable
    r = cpost("/api/crypt/disable", {"password": pw})
    check("disable with the password", okr(r), f"{r.status_code} {jcode(r)}")
    if not okr(r):
        return
    S.crypt_by_test = False
    wait_online()
    check("state plain: /api/crypt and status.crypt", crypt_state() == (False, True)
          and status().get("crypt") == {"enabled": False, "unlocked": True, "damaged": False})
    d = status().get("drive", {})
    check("drive owner app and fs_ok after disable", d.get("owner") == "app" and d.get("fs_ok") is True, d)
    check("disable formatted: test folder is gone", T2 not in ls("")[1])
    expect("unlock after disable -> CRYPT_NOT_ENABLED", cpost("/api/crypt/unlock", {"password": pw}), 409,
           "CRYPT_NOT_ENABLED")
    d2 = make_3mf(16)
    expect("plain again: mkdir", call("POST", "/api/mkdir", params={"path": T2}), 200)
    check("plain again: upload + download identical",
          okr(upload(f"{T2}/plain.3mf", d2)) and sha(download(f"{T2}/plain.3mf").content) == sha(d2))
    expect("plain again: delete folder", call("POST", "/api/delete", params={"path": T2, "recursive": "1"}), 200)
    S.extra_paths.remove(T2)

    # erase (lost password and key): set up again, lock, then erase the key area and format plain
    r = cpost("/api/crypt/setup", {"password": pw})
    key = jr(r).get("recovery_key", "")
    if not check("second setup (for the erase test): 200 with recovery_key", okr(r) and bool(key), f"{r.status_code} {jcode(r)}"):
        return
    S.crypt_by_test, S.crypt_pw, S.recovery = True, pw, key
    expect("lock", cpost("/api/crypt/lock"), 200)
    expect("erase without confirm -> BAD_REQUEST", cpost("/api/crypt/erase", {}), 400, "BAD_REQUEST")
    expect("erase with a wrong confirm -> BAD_REQUEST", cpost("/api/crypt/erase", {"confirm": "yes"}), 400, "BAD_REQUEST")
    if u:
        expect("erase as role user -> FORBIDDEN_ROLE", cpost("/api/crypt/erase", {"confirm": "ERASE"}, tok=u),
               403, "FORBIDDEN_ROLE")
    check("... still encrypted and locked", crypt_state() == (True, False))
    r = cpost("/api/crypt/erase", {"confirm": "ERASE"})
    check("erase while locked (no password needed): 200", okr(r), f"{r.status_code} {jcode(r)}")
    if not okr(r):
        return
    S.crypt_by_test = False
    wait_online()
    check("state plain: /api/crypt and status.crypt", crypt_state() == (False, True)
          and status().get("crypt") == {"enabled": False, "unlocked": True, "damaged": False})
    d = status().get("drive", {})
    check("drive owner app and fs_ok after erase", d.get("owner") == "app" and d.get("fs_ok") is True, d)
    expect("erase on a plain drive -> CRYPT_NOT_ENABLED", cpost("/api/crypt/erase", {"confirm": "ERASE"}), 409,
           "CRYPT_NOT_ENABLED")
    expect("unlock after erase -> CRYPT_NOT_ENABLED", cpost("/api/crypt/unlock", {"password": pw}), 409, "CRYPT_NOT_ENABLED")


def sec_crypt_damaged(a):
    head("9b. Drive encryption, state damaged (only when the target reports damaged:true)")
    r = call("GET", "/api/crypt")
    j = jr(r)
    check("GET /api/crypt: enabled true, unlocked false, damaged true",
          r.status_code == 200 and j.get("enabled") is True and j.get("unlocked") is False and j.get("damaged") is True, j)
    check("status.crypt = {enabled:true, unlocked:false, damaged:true}",
          status().get("crypt") == {"enabled": True, "unlocked": False, "damaged": True}, status().get("crypt"))
    d = status().get("drive", {})
    check("damaged: drive owner none, fs_ok false, needs_format false, total/free/files 0",
          d.get("owner") == "none" and d.get("fs_ok") is False and d.get("needs_format") is False
          and d.get("total") == 0 and d.get("free") == 0 and d.get("files") == 0, d)
    T2, P = "_apitest_damaged", "_apitest_damaged/x.3mf"
    for label, m, path, params, body in locked_reqs_f(T2, P):
        expect(f"423 CRYPT_DAMAGED: {label}", call(m, path, params=params, body=body), 423, "CRYPT_DAMAGED")
    expect("401 before CRYPT_DAMAGED without login: GET files", call("GET", "/api/files", params={"dir": ""}, tok=None),
           401, "UNAUTHORIZED")
    expect("unlock (password) -> 423 CRYPT_DAMAGED", call("POST", "/api/crypt/unlock", form={"password": "x" * 12}),
           423, "CRYPT_DAMAGED")
    expect("unlock (recovery key) -> 423 CRYPT_DAMAGED",
           call("POST", "/api/crypt/unlock", form={"recovery_key": "7K3M-Q9XD-4WTB-HN2C-5RYE-8FGA-ZP6V-JC3S"}),
           423, "CRYPT_DAMAGED")
    expect("password change -> 423 CRYPT_DAMAGED",
           call("POST", "/api/crypt/password", form={"old": "x" * 12, "new": "y" * 12}), 423, "CRYPT_DAMAGED")
    expect("new recovery key -> 423 CRYPT_DAMAGED", call("POST", "/api/crypt/recovery", form={"password": "x" * 12}),
           423, "CRYPT_DAMAGED")
    reach = [call("GET", p).status_code for p in ("/api/status", "/api/session", "/api/users", "/api/devices",
                                                  "/api/wifi/list", "/api/log", "/api/crypt")]
    check("damaged: status, session, users, devices, wifi, log, crypt still reachable", reach == [200] * 7, reach)
    expect("erase without confirm -> BAD_REQUEST", call("POST", "/api/crypt/erase", form={}), 400, "BAD_REQUEST")
    expect("erase with a wrong confirm -> BAD_REQUEST", call("POST", "/api/crypt/erase", form={"confirm": "erase"}),
           400, "BAD_REQUEST")
    expect("401 without login: erase", call("POST", "/api/crypt/erase", form={"confirm": "ERASE"}, tok=None), 401,
           "UNAUTHORIZED")
    u = role_token()
    if u:
        expect("erase as role user -> FORBIDDEN_ROLE", call("POST", "/api/crypt/erase", form={"confirm": "ERASE"}, tok=u),
               403, "FORBIDDEN_ROLE")
    check("... still damaged", jr(call("GET", "/api/crypt")).get("damaged") is True)
    if not a.crypt_test:
        skip("erase of the damaged drive", "needs --crypt-test (--i-know-this-erases-the-drive)")
        return
    r = cpost("/api/crypt/erase", {"confirm": "ERASE"})
    check("erase of the damaged drive: 200", okr(r), f"{r.status_code} {jcode(r)}")
    if not okr(r):
        return
    S.damaged = False
    wait_online()
    j = jr(call("GET", "/api/crypt"))
    check("after erase: plain, unlocked, not damaged",
          j.get("enabled") is False and j.get("unlocked") is True and j.get("damaged") is False, j)
    d = status().get("drive", {})
    check("drive owner app and fs_ok after erase", d.get("owner") == "app" and d.get("fs_ok") is True, d)
    expect("erase again -> CRYPT_NOT_ENABLED", cpost("/api/crypt/erase", {"confirm": "ERASE"}), 409, "CRYPT_NOT_ENABLED")


FP_RE = re.compile(r"[0-9A-F]{2}(:[0-9A-F]{2}){31}")


def sec_https(a):
    head("9c. Optional HTTPS, non-destructive (status fields, roles, CA download; the setting is never changed)")
    st = status()
    keys = ("https", "https_active", "https_restart", "cert_sha256", "ca_sha256", "cert_expires")
    if not any(k in st for k in keys):
        skip("HTTPS checks", "no HTTPS fields in the status: HTTPS compiled out (PD_HTTPS_OPTION, default in 0.4.0/0.5.0)")
        return
    check("status has https, https_active, https_restart, cert_sha256, ca_sha256, cert_expires",
          all(k in st for k in keys), [k for k in keys if k not in st])
    if not all(k in st for k in keys):
        return
    check("https / https_active / https_restart are booleans",
          all(isinstance(st[k], bool) for k in ("https", "https_active", "https_restart")), st)
    fp_ok = all(x == "" or FP_RE.fullmatch(x) for x in (st["cert_sha256"], st["ca_sha256"]))
    check("cert_sha256 / ca_sha256: empty or AA:BB:... (32 bytes)", fp_ok, (st["cert_sha256"], st["ca_sha256"]))
    check("cert_expires: empty or YYYY-MM-DD", re.fullmatch(r"(\d{4}-\d{2}-\d{2})?", st["cert_expires"]) is not None,
          st["cert_expires"])
    check("a certificate exists exactly when the CA fingerprint is set", (st["cert_sha256"] == "") == (st["ca_sha256"] == ""), st)
    print(f"       HTTPS: setting {st['https']}, active {st['https_active']}, restart needed {st['https_restart']}")
    expect("401 without login: POST settings/https", call("POST", "/api/settings/https", params={"value": st["https"] and "1" or "0"}, tok=None),
           401, "UNAUTHORIZED")
    expect("401 without login: GET https/ca.cer", call("GET", "/api/https/ca.cer", tok=None), 401, "UNAUTHORIZED")
    expect("403 FORBIDDEN without X-PrintDrive header: POST settings/https",
           call("POST", "/api/settings/https", params={"value": "1" if st["https"] else "0"}, csrf=False), 403, "FORBIDDEN")
    expect("settings/https without any parameter -> BAD_REQUEST (nothing changes)",
           call("POST", "/api/settings/https"), 400, "BAD_REQUEST")
    u = role_token()
    if u:
        expect("FORBIDDEN_ROLE for user: POST settings/https",
               call("POST", "/api/settings/https", params={"value": "1" if st["https"] else "0"}, tok=u), 403, "FORBIDDEN_ROLE")
    else:
        skip("role check for settings/https", "no temporary user possible")
    r = call("GET", "/api/https/ca.cer", tok=u or DEF)
    if st["ca_sha256"]:
        check("GET https/ca.cer (role user): 200, application/x-x509-ca-cert, DER (starts with 0x30), not empty",
              r.status_code == 200 and r.headers.get("Content-Type", "").split(";")[0].strip() == "application/x-x509-ca-cert"
              and r.content[:1] == b"0" and len(r.content) > 0, f"{r.status_code} {r.headers.get('Content-Type')}")
    else:
        expect("no CA yet: GET https/ca.cer -> 404 NOT_FOUND", r, 404, "NOT_FOUND")
    check("... the setting was not changed", status().get("https") == st["https"])


# ------------------------------------------------------------------ API v5 (docs/15_api_v5.md)

def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def v5_folder():
    """Test folder for the v5 file sections: the one of section 4, or a new one (--only). The file manager must own
    the drive; it is taken over like in section 4 (not with --skip-destructive). None if impossible."""
    if status().get("drive", {}).get("owner") != "app":
        if S.skip_destructive or not ensure_app():
            return None
        S.took = True
    if not S.T_made:
        S.T = f"_apitest_{os.urandom(3).hex()}"
        if not okr(call("POST", "/api/mkdir", params={"path": S.T})):
            return None
        S.T_made = True
    return S.T


def allowed_exts():
    """Extensions the target accepts for uploads (status.types), .3mf first."""
    types = status().get("types", "all")
    if types == "all":
        return [".3mf", ".gcode", ".txt"]
    exts = [e.strip().lower() for e in types.split(",") if e.strip()]
    return sorted(exts, key=lambda e: e != ".3mf") or [".3mf"]


def sample_for(ext, kb):
    return make_3mf(kb) if ext == ".3mf" else os.urandom(kb * 1024)


def sec_time(a):
    head("11. Clock and time zone (API v5)")
    t = status().get("time")
    ok = check("status.time = {epoch: integer >= 0, synced: bool, tz: non-empty string}",
               isinstance(t, dict) and is_int(t.get("epoch")) and t["epoch"] >= 0 and isinstance(t.get("synced"), bool)
               and isinstance(t.get("tz"), str) and t["tz"] != "", t)
    if not ok:
        return
    print(f"       clock: epoch {t['epoch']}, synced {t['synced']}, tz {t['tz']!r}")
    now = int(time.time())
    if t["epoch"]:
        check("device clock within 2 minutes of this PC (Unix seconds, UTC)", abs(t["epoch"] - now) <= 120,
              f"device {t['epoch']}, PC {now}")
    check("synced implies a known time (epoch > 0)", not t["synced"] or t["epoch"] > 0, t)
    expect("401 without login: POST settings/time", call("POST", "/api/settings/time", params={"tz": t["tz"]}, tok=None),
           401, "UNAUTHORIZED")
    expect("401 without login: POST time", call("POST", "/api/time", params={"epoch": str(now)}, tok=None),
           401, "UNAUTHORIZED")
    expect("403 FORBIDDEN without X-PrintDrive header: POST settings/time",
           call("POST", "/api/settings/time", params={"tz": t["tz"]}, csrf=False), 403, "FORBIDDEN")
    expect("403 FORBIDDEN without X-PrintDrive header: POST time",
           call("POST", "/api/time", params={"epoch": str(now)}, csrf=False), 403, "FORBIDDEN")
    u = role_token()
    if u:
        expect("FORBIDDEN_ROLE for user: POST settings/time",
               call("POST", "/api/settings/time", params={"tz": t["tz"]}, tok=u), 403, "FORBIDDEN_ROLE")
    else:
        skip("role check for settings/time", "no temporary user possible")
    expect("settings/time without tz -> BAD_REQUEST", call("POST", "/api/settings/time"), 400, "BAD_REQUEST")
    expect("settings/time with an empty tz -> BAD_REQUEST",
           call("POST", "/api/settings/time", params={"tz": ""}), 400, "BAD_REQUEST")
    expect("settings/time with a 200-byte tz -> BAD_REQUEST",
           call("POST", "/api/settings/time", params={"tz": "A" * 200}), 400, "BAD_REQUEST")
    check("... tz unchanged", status().get("time", {}).get("tz") == t["tz"])
    r = call("POST", "/api/settings/time", params={"tz": t["tz"]})     # quote() encodes '+' as %2B
    check("settings/time with the current tz: 200, status.time.tz unchanged", okr(r)
          and status().get("time", {}).get("tz") == t["tz"], r.text[:120])

    for label, val in (("missing", None), ("implausible (12345)", "12345"), ("not a number", "jetzt"),
                       ("year 2100", "4102444800")):
        expect(f"POST time with epoch {label} -> BAD_REQUEST",
               call("POST", "/api/time", params={"epoch": val} if val else None), 400, "BAD_REQUEST")
    synced = status().get("time", {}).get("synced")
    now = int(time.time())
    r = call("POST", "/api/time", params={"epoch": str(now)}, tok=u or DEF)
    j = jr(r)
    jt = status().get("time") or {}
    check("POST time" + (" as role user" if u else "") + ": 200 with set (bool)",
          okr(r) and isinstance(j.get("set"), bool), r.text[:120])
    if synced:
        check("NTP-synced clock is not overridden (set:false; status: still synced)", j.get("set") is False
              and jt.get("synced") is True and abs(jt.get("epoch", 0) - now) <= 120, (j, jt))
    else:
        check("unsynced clock is set (set:true; status: epoch = sent time, synced false)", j.get("set") is True
              and abs(jt.get("epoch", 0) - now) <= 5 and jt.get("synced") is False, (j, jt))
        note("the device clock was set to this PC's time (like the web UI does)")

    T = v5_folder()
    if not T:
        skip("time stamps of uploads", "the file manager does not own the drive (--skip-destructive)")
        return
    ext = allowed_exts()[0]
    data = sample_for(ext, 4)
    p1 = f"{T}/Zeit ohne mtime{ext}"
    r = call("POST", "/api/upload", params={"path": p1}, body=data, timeout=up_timeout(len(data)))
    clock = status().get("time", {}).get("epoch", 0)
    e = ls(T)[1].get(p1.rsplit("/", 1)[1], {})
    if clock:
        check("upload without mtime: listing mtime = device clock (Unix seconds, +-2 min)",
              okr(r) and is_int(e.get("mtime")) and abs(e["mtime"] - clock) <= 120, f"mtime {e.get('mtime')}, clock {clock}")
    else:
        check("upload without mtime and unknown clock: listing mtime 0", okr(r) and e.get("mtime") == 0, e)
    p2 = f"{T}/Zeit mit mtime{ext}"
    r = call("POST", "/api/upload", params={"path": p2, "mtime": "1790000000"}, body=data, timeout=up_timeout(len(data)))
    e = ls(T)[1].get(p2.rsplit("/", 1)[1], {})
    check("upload with mtime=1790000000 (Unix seconds): listing returns it (FAT 2 s)",
          okr(r) and abs(e.get("mtime", 0) - 1790000000) <= 2, e.get("mtime"))


def sec_autolock(a):
    head("12. Automatic lock of the encrypted drive: settings (API v5)")
    al = status().get("autolock")
    ok = check("status.autolock = {minutes: 0..1440, on_detach: bool}",
               isinstance(al, dict) and is_int(al.get("minutes")) and 0 <= al["minutes"] <= 1440
               and isinstance(al.get("on_detach"), bool), al)
    if not ok:
        return
    m, d = al["minutes"], al["on_detach"]
    print(f"       automatic lock: {m} min ({'off' if not m else 'on'}), on detach {d}")
    expect("401 without login: POST settings/autolock",
           call("POST", "/api/settings/autolock", params={"minutes": str(m)}, tok=None), 401, "UNAUTHORIZED")
    expect("403 FORBIDDEN without X-PrintDrive header: POST settings/autolock",
           call("POST", "/api/settings/autolock", params={"minutes": str(m)}, csrf=False), 403, "FORBIDDEN")
    u = role_token()
    if u:
        expect("FORBIDDEN_ROLE for user: POST settings/autolock",
               call("POST", "/api/settings/autolock", params={"minutes": str(m)}, tok=u), 403, "FORBIDDEN_ROLE")
    for label, params in (("no parameter", None), ("minutes=1441", {"minutes": "1441"}), ("minutes=-1", {"minutes": "-1"}),
                          ("minutes=abc", {"minutes": "abc"}), ("on_detach=2", {"on_detach": "2"})):
        expect(f"settings/autolock with {label} -> BAD_REQUEST", call("POST", "/api/settings/autolock", params=params),
               400, "BAD_REQUEST")
    check("... nothing changed", status().get("autolock") == al, status().get("autolock"))

    S.autolock_orig = (m, d)
    m2 = 30 if m != 30 else 45
    r = call("POST", "/api/settings/autolock", params={"minutes": str(m2), "on_detach": "0" if d else "1"})
    check(f"set minutes={m2}, on_detach={not d}: 200, status shows it",
          okr(r) and status().get("autolock") == {"minutes": m2, "on_detach": not d}, r.text[:120])
    r = call("POST", "/api/settings/autolock", params={"on_detach": "true" if d else "false"})
    check("on_detach accepts true/false too", okr(r) and status().get("autolock", {}).get("on_detach") is d,
          r.text[:120])
    r = call("POST", "/api/settings/autolock", params={"on_detach": "0" if d else "1"})
    r = call("POST", "/api/settings/autolock", params={"minutes": str(m)})
    check("only minutes given: on_detach keeps its value", okr(r)
          and status().get("autolock") == {"minutes": m, "on_detach": not d}, status().get("autolock"))
    r = call("POST", "/api/settings/autolock", params={"on_detach": "1" if d else "0"})
    check("only on_detach given: back to the original settings", okr(r) and status().get("autolock") == al,
          status().get("autolock"))
    if status().get("autolock") == al:
        S.autolock_orig = None
    skip("the automatic lock itself", "takes minutes; on the mock: /mock/set?idle_s=N and usb_connected=0")


def sec_lang(a):
    head("13. Display language (API v5)")
    lang = status().get("lang")
    if not check("status.lang is de or en", lang in ("de", "en"), lang):
        return
    expect("401 without login: POST settings/lang", call("POST", "/api/settings/lang", params={"value": lang}, tok=None),
           401, "UNAUTHORIZED")
    expect("403 FORBIDDEN without X-PrintDrive header: POST settings/lang",
           call("POST", "/api/settings/lang", params={"value": lang}, csrf=False), 403, "FORBIDDEN")
    for label, params in (("no value", None), ("value=fr", {"value": "fr"}), ("value=DE", {"value": "DE"})):
        expect(f"settings/lang with {label} -> BAD_REQUEST", call("POST", "/api/settings/lang", params=params),
               400, "BAD_REQUEST")
    check("... language unchanged", status().get("lang") == lang)
    other = "en" if lang == "de" else "de"
    S.lang_orig = lang
    u = role_token()
    r = call("POST", "/api/settings/lang", params={"value": other}, tok=u or DEF)
    check(f"set value={other}" + (" as role user" if u else "") + ": 200, status shows it",
          okr(r) and status().get("lang") == other, r.text[:120])
    r = call("POST", "/api/settings/lang", params={"value": lang})
    check(f"back to {lang}", okr(r) and status().get("lang") == lang)
    if status().get("lang") == lang:
        S.lang_orig = None


def raw_partial_upload(path, size, part, extra=""):
    """Starts POST /api/upload?path&size&offset=0 over a raw socket, sends `part` and closes (Wi-Fi drop)."""
    sock = socket.create_connection((S.host, S.port), timeout=10)
    q = f"path={quote(path, safe='')}&size={size}&offset=0{extra}"
    sock.sendall((f"POST /api/upload?{q} HTTP/1.1\r\nHost: {S.host}\r\nX-PrintDrive: 1\r\n"
                  f"Cookie: dd_session={S.main}\r\nContent-Length: {size}\r\n"
                  f"Content-Type: application/octet-stream\r\n\r\n").encode())
    sock.sendall(part)
    time.sleep(1.5)
    sock.close()          # the server handles one request at a time: the next one waits until it noticed


def sec_resume(a):
    head("14. Resumable upload (API v5)")
    T = v5_folder()
    if not T:
        skip("resumable upload", "the file manager does not own the drive (--skip-destructive)")
        return
    D = f"{T}/Fortsetzen"
    expect("mkdir for the resume test", call("POST", "/api/mkdir", params={"path": D}), 200)
    data = make_3mf(max(a.size_kb, 256))
    n = len(data)
    P = f"{D}/Groß äöü.3mf"

    def resume(path, size, tok=DEF):
        return call("GET", "/api/upload/resume", params={"path": path, "size": str(size)}, tok=tok)

    def part(path, size, off, body, **extra):
        return call("POST", "/api/upload", params={"path": path, "size": str(size), "offset": str(off), **extra},
                    body=body, timeout=up_timeout(len(body)))

    def resumable():
        return jr(call("GET", "/api/status", timeout=60)).get("upload", {})

    expect("401 without login: GET upload/resume", resume(P, n, tok=None), 401, "UNAUTHORIZED")
    expect("upload/resume without size -> BAD_REQUEST", call("GET", "/api/upload/resume", params={"path": P}),
           400, "BAD_REQUEST")
    r = resume(P, n)
    check("upload/resume for a new file: 200, offset 0", okr(r) and jr(r).get("offset") == 0, r.text[:120])
    up = resumable()
    check("status.upload.resumable exists (null or object)", "resumable" in up, up)

    half = n // 2
    raw_partial_upload(P, n, data[:half])
    up = resumable()
    res = up.get("resumable") or {}
    off = res.get("offset", 0)
    ok = check("after the broken upload: not active, status.upload.resumable = {path, size, offset}, 0 < offset <= sent",
               up.get("active") is False and res.get("path") == P and res.get("size") == n and is_int(off)
               and 0 < off <= half, f"{up} (sent {half})")
    if not ok:
        return
    note(f"server kept {off} of {half} sent bytes")
    r = resume(P, n)
    check("upload/resume reports the same offset", okr(r) and jr(r).get("offset") == off, r.text[:120])
    check("upload/resume with another size -> offset 0", jr(resume(P, n + 1)).get("offset") == 0)
    check("upload/resume with another path -> offset 0", jr(resume(f"{D}/anders.3mf", n)).get("offset") == 0)
    check("the partial file is not in the listing", P.rsplit("/", 1)[1] not in ls(D)[1])

    if off + 1 < n:
        r = part(P, n, off + 1, data[off + 1:])
        expect("resume at the wrong offset -> 409 RESUME_MISMATCH", r, 409, "RESUME_MISMATCH")
        check("... the answer carries the server's offset", jr(r).get("offset") == off, r.text[:120])
    r = part(P, n + 1, off, data[off:] + b"\0")
    expect("resume with another size (no partial for it) -> 409 RESUME_MISMATCH", r, 409, "RESUME_MISMATCH")
    check("... offset 0 in the answer", jr(r).get("offset") == 0, r.text[:120])
    r = part(P, n, 0, data)
    expect("offset=0 while a part of this file is held -> 409 RESUME_MISMATCH", r, 409, "RESUME_MISMATCH")
    check("... the answer carries the server's offset", jr(r).get("offset") == off, r.text[:120])
    r = part(P, n, n + 1, b"")
    expect("offset > size -> 409 RESUME_MISMATCH", r, 409, "RESUME_MISMATCH")
    expect("Content-Length != size - offset -> BAD_REQUEST", part(P, n, off, data[off:-1]), 400, "BAD_REQUEST")
    check("refused requests kept the partial upload", jr(resume(P, n)).get("offset") == off)

    r = part(P, n, off, data[off:])
    check("resume at the server's offset: 200", okr(r), r.text[:120])
    check("... file complete and identical (SHA-256)", sha(download(P).content) == sha(data))
    check("... listing shows the full size", ls(D)[1].get(P.rsplit("/", 1)[1], {}).get("size") == n)
    check("... status.upload.resumable is null, upload/resume says 0",
          resumable().get("resumable") is None and jr(resume(P, n)).get("offset") == 0)

    # a partial upload is discarded by another upload
    data2 = make_3mf(max(a.size_kb, 256) // 2)
    n2 = len(data2)
    P2 = f"{D}/Zweiter Versuch.3mf"
    raw_partial_upload(P2, n2, data2[:n2 // 2])
    res = resumable().get("resumable") or {}
    check("second broken upload is resumable", res.get("path") == P2 and res.get("offset", 0) > 0, res)
    r = upload(f"{D}/anderes.3mf", make_3mf(1))
    check("another (plain) upload: 200", okr(r), r.text[:120])
    check("... discarded the partial upload (status null, upload/resume 0, not listed)",
          resumable().get("resumable") is None and jr(resume(P2, n2)).get("offset") == 0
          and P2.rsplit("/", 1)[1] not in ls(D)[1])
    r = part(P2, n2, 8192, data2[8192:])
    expect("resume without a partial upload -> 409 RESUME_MISMATCH", r, 409, "RESUME_MISMATCH")
    check("... offset 0 in the answer", jr(r).get("offset") == 0, r.text[:120])
    r = part(P2, n2, 0, data2)
    check("offset=0 with the whole file: 200, identical", okr(r) and sha(download(P2).content) == sha(data2),
          r.text[:120])


def sec_zip(a):
    head("15. Folder as ZIP (API v5)")
    T = v5_folder()
    if not T:
        skip("folder ZIP", "the file manager does not own the drive (--skip-destructive)")
        return
    top = "ZIP Test äöü"
    Z = f"{T}/{top}"
    exts = allowed_exts()
    dirs = ["Unterordner Ö", "Unterordner Ö/Tiefer €", "Leer"]
    files = {f"Würfel ä{exts[0]}": sample_for(exts[0], 8),
             f"Unterordner Ö/Datei ß{exts[1 % len(exts)]}": sample_for(exts[1 % len(exts)], 3),
             f"Unterordner Ö/Tiefer €/Notiz{exts[2 % len(exts)]}": sample_for(exts[2 % len(exts)], 1),
             f"Unterordner Ö/Tiefer €/Leer.3mf" if ".3mf" in exts else f"Unterordner Ö/Zweite{exts[0]}":
                 sample_for(".3mf" if ".3mf" in exts else exts[0], 1)}
    made = okr(call("POST", "/api/mkdir", params={"path": Z}))
    for d in dirs:
        made = okr(call("POST", "/api/mkdir", params={"path": f"{Z}/{d}"})) and made
    for p, body in files.items():
        made = okr(upload(f"{Z}/{p}", body)) and made
    if not check("tree with umlauts, nested and empty folders created", made):
        return
    r = call("GET", "/api/zip", params={"path": Z}, timeout=120)
    cd = r.headers.get("Content-Disposition", "")
    check("GET zip: 200, Content-Type application/zip", r.status_code == 200
          and r.headers.get("Content-Type", "").split(";")[0].strip() == "application/zip",
          f"{r.status_code} {r.headers.get('Content-Type')}")
    check("Content-Disposition: attachment with a .zip file name", cd.startswith("attachment") and ".zip" in cd, cd)
    check("sent chunked (Transfer-Encoding: chunked)", r.headers.get("Transfer-Encoding", "").lower() == "chunked",
          r.headers.get("Transfer-Encoding"))
    try:
        z = zipfile.ZipFile(io.BytesIO(r.content))
    except zipfile.BadZipFile as e:
        check("the answer is a readable ZIP", False, repr(e))
        return
    check("ZIP readable, every CRC right (testzip)", z.testzip() is None)
    names = set(z.namelist())
    want = {f"{top}/"} | {f"{top}/{d}/" for d in dirs} | {f"{top}/{p}" for p in files}
    check("entries: folder name on top, every file and folder (also the empty one), nothing else",
          names == want, f"missing {sorted(want - names)}, extra {sorted(names - want)}")
    infos = {i.filename: i for i in z.infolist()}
    same = all(p_full in infos and z.read(p_full) == body and infos[p_full].CRC == (zlib.crc32(body) & 0xFFFFFFFF)
               for p_full, body in ((f"{top}/{p}", b) for p, b in files.items()))
    check("file contents and CRC-32 identical to the uploads", same)
    check("stored, not compressed (method 0)", all(i.compress_type == zipfile.ZIP_STORED for i in infos.values()))
    check("non-ASCII names carry the UTF-8 flag (bit 11)",
          all(i.flag_bits & 0x800 for n_, i in infos.items() if not n_.isascii()))
    u = role_token()
    if u:
        r = call("GET", "/api/zip", params={"path": Z}, tok=u, timeout=120)
        check("role user may download a ZIP", r.status_code == 200 and r.content[:2] == b"PK", r.status_code)
    expect("401 without login: GET zip", call("GET", "/api/zip", params={"path": Z}, tok=None), 401, "UNAUTHORIZED")
    expect("zip of a missing folder -> NOT_FOUND", call("GET", "/api/zip", params={"path": f"{Z}/gibtsnicht"}),
           404, "NOT_FOUND")
    expect("zip of a file -> NOT_FOUND", call("GET", "/api/zip", params={"path": f"{Z}/{next(iter(files))}"}),
           404, "NOT_FOUND")
    expect("zip with '..' -> BAD_NAME", call("GET", "/api/zip", params={"path": f"{T}/../x"}), 400, "BAD_NAME")


DAV_NS = "{DAV:}"


def dav(method, path, headers=None, body=None, auth=DEF, timeout=60):
    """WebDAV request: HTTP Basic, no cookie, no X-PrintDrive header."""
    if auth is DEF:
        auth = (S.user, S.password)
    return requests.request(method, S.base + path, headers=headers or {}, data=body, auth=auth, timeout=timeout)


def dav_entries(r):
    """207 body -> {href (decoded, without trailing slash): {"dir": bool, "size": int|None}}"""
    from urllib.parse import unquote
    from xml.etree import ElementTree
    out = {}
    try:
        root = ElementTree.fromstring(r.content)
    except ElementTree.ParseError:
        return None
    for resp in root.iter(DAV_NS + "response"):
        href = unquote(resp.findtext(DAV_NS + "href") or "")
        href = re.sub(r"^https?://[^/]+", "", href).rstrip("/")
        rt = resp.find(f".//{DAV_NS}resourcetype")
        size = resp.findtext(f".//{DAV_NS}getcontentlength")
        out[href] = {"dir": rt is not None and rt.find(DAV_NS + "collection") is not None,
                     "size": int(size) if size and size.isdigit() else None}
    return out


def sec_webdav(a):
    head("16. WebDAV network drive (API v5, smoke test)")
    st = status()
    if "webdav" in st:
        expect("401 without login: POST settings/webdav", call("POST", "/api/settings/webdav",
               params={"value": "1" if st["webdav"] else "0"}, tok=None), 401, "UNAUTHORIZED")
        u = role_token()
        if u:
            expect("FORBIDDEN_ROLE for user: POST settings/webdav", call("POST", "/api/settings/webdav",
                   params={"value": "1" if st["webdav"] else "0"}, tok=u), 403, "FORBIDDEN_ROLE")
        expect("settings/webdav with value=2 -> BAD_REQUEST",
               call("POST", "/api/settings/webdav", params={"value": "2"}), 400, "BAD_REQUEST")
        check("... switch unchanged", status().get("webdav") == st["webdav"])
        if st["webdav"] is False:
            skip("WebDAV smoke test", "switched off (status webdav:false); this test does not change the setting")
            return
    r = dav("OPTIONS", "/dav/", auth=None)
    if r.status_code == 404:
        skip("WebDAV smoke test", "/dav/ answers 404: no WebDAV in this firmware")
        return
    # OPTIONS is public on the device (Windows asks before it logs in): test the login with PROPFIND
    r = dav("PROPFIND", "/dav/", auth=None, headers={"Depth": "0"})
    check("401 without credentials, WWW-Authenticate: Basic", r.status_code == 401
          and r.headers.get("WWW-Authenticate", "").lower().startswith("basic"), f"{r.status_code} "
          f"{r.headers.get('WWW-Authenticate')}")
    r = dav("OPTIONS", "/dav/")
    check("OPTIONS with Basic: 200, DAV header with class 1", r.status_code == 200
          and "1" in r.headers.get("DAV", ""), f"{r.status_code} DAV={r.headers.get('DAV')}")
    r = dav("PROPFIND", "/dav/", headers={"Depth": "0", "Cookie": f"dd_session={S.main}"}, auth=None)
    check("session cookie alone is not accepted (401)", r.status_code == 401, r.status_code)
    r = dav("PROPFIND", "/dav/", headers={"Depth": "0"}, auth=(S.user, "wrong-" + os.urandom(3).hex()))
    check("wrong password -> 401", r.status_code == 401, r.status_code)
    T = v5_folder()          # the correct password above (in v5_folder's status call) resets the failure counter
    if not T:
        skip("WebDAV file operations", "the file manager does not own the drive (--skip-destructive)")
        return
    base = "/dav/" + quote(T) + "/"
    r = dav("PROPFIND", "/dav/", headers={"Depth": "0"})
    e = dav_entries(r) if r.status_code == 207 else None
    check("PROPFIND Depth 0 on /dav/: 207, exactly one response, a collection",
          e is not None and len(e) == 1 and list(e.values())[0]["dir"], f"{r.status_code} {r.text[:120]!r}")
    r = dav("PROPFIND", "/dav/", headers={"Depth": "1"})
    e = dav_entries(r) or {}
    check("PROPFIND Depth 1 on /dav/ lists the test folder", f"/dav/{T}" in e, sorted(e)[:8])
    expect_st = lambda name, r, codes: check(name, r.status_code in codes, f"-> {r.status_code}" if r.status_code in codes
                                             else f"expected {codes}, got {r.status_code} {r.text[:100]!r}")
    expect_st("PROPFIND Depth infinity -> 403", dav("PROPFIND", base, headers={"Depth": "infinity"}), (403,))
    F = base + quote("DAV Ordner ä") + "/"
    expect_st("MKCOL folder with umlaut -> 201", dav("MKCOL", F), (201,))
    expect_st("MKCOL again -> 405", dav("MKCOL", F), (405,))
    expect_st("MKCOL with missing parent -> 409", dav("MKCOL", base + "fehlt/neu/"), (409,))
    ext = allowed_exts()[0]
    body = sample_for(ext, 16)
    f1 = F + quote(f"Datei ü{ext}")
    expect_st("PUT new file -> 201", dav("PUT", f1, body=body), (201,))
    e = ls(f"{T}/DAV Ordner ä")[1].get(f"Datei ü{ext}", {})
    check("... visible in /api/files with the right size", e.get("size") == len(body), e)
    body2 = sample_for(ext, 8)
    expect_st("PUT onto the existing file -> 204 (or 200)", dav("PUT", f1, body=body2), (200, 204))
    r = dav("GET", f1)
    check("GET returns the new content", r.status_code == 200 and sha(r.content) == sha(body2), r.status_code)
    r = dav("HEAD", f1)
    check("HEAD: 200 with Content-Length", r.status_code == 200 and r.headers.get("Content-Length") == str(len(body2)),
          f"{r.status_code} {r.headers.get('Content-Length')}")
    r = dav("PROPFIND", F, headers={"Depth": "1"})
    e = dav_entries(r) or {}
    me = e.get(f"/dav/{T}/DAV Ordner ä/Datei ü{ext}", {})
    check("PROPFIND Depth 1 on the folder: the file with getcontentlength", r.status_code == 207
          and me.get("size") == len(body2) and me.get("dir") is False, sorted(e))
    f2 = F + quote(f"Umbenannt{ext}")
    expect_st("MOVE (rename) -> 201", dav("MOVE", f1, headers={"Destination": S.base + f2}), (201,))
    expect_st("... old name: GET 404", dav("GET", f1), (404,))
    r = dav("GET", f2)
    check("... new name: content intact", r.status_code == 200 and sha(r.content) == sha(body2), r.status_code)
    f3 = F + quote(f"Kopie{ext}")
    expect_st("COPY -> 201", dav("COPY", f2, headers={"Destination": S.base + f3}), (201,))
    r = dav("GET", f3)
    check("... copy identical", r.status_code == 200 and sha(r.content) == sha(body2), r.status_code)
    expect_st("MOVE onto an existing file with Overwrite: F -> 412",
              dav("MOVE", f3, headers={"Destination": S.base + f2, "Overwrite": "F"}), (412,))
    r = dav("LOCK", f2, headers={"Timeout": "Second-60"}, body=(
        '<?xml version="1.0" encoding="utf-8"?><D:lockinfo xmlns:D="DAV:"><D:lockscope><D:exclusive/></D:lockscope>'
        '<D:locktype><D:write/></D:locktype><D:owner>apitest</D:owner></D:lockinfo>').encode())
    tok = r.headers.get("Lock-Token", "")
    check("LOCK: 200 with a Lock-Token", r.status_code == 200 and tok != "", f"{r.status_code} {tok}")
    if tok:
        expect_st("UNLOCK -> 204", dav("UNLOCK", f2, headers={"Lock-Token": tok}), (204,))
    r = dav("PROPPATCH", f2, body=(
        '<?xml version="1.0" encoding="utf-8"?><D:propertyupdate xmlns:D="DAV:" xmlns:Z="urn:schemas-microsoft-com:">'
        '<D:set><D:prop><Z:Win32LastModifiedTime>Wed, 11 Nov 2026 10:00:00 GMT</Z:Win32LastModifiedTime></D:prop></D:set>'
        '</D:propertyupdate>').encode())
    expect_st("PROPPATCH (Windows time stamps) -> 207", r, (207,))
    expect_st("DELETE file -> 204", dav("DELETE", f3), (204,))
    expect_st("... GET 404", dav("GET", f3), (404,))
    expect_st("DELETE the non-empty folder -> 204", dav("DELETE", F), (204,))
    expect_st("... PROPFIND 404", dav("PROPFIND", F, headers={"Depth": "0"}), (404,))
    check("... gone from /api/files too", "DAV Ordner ä" not in ls(T)[1])


# ------------------------------------------------------------------ cleanup

def crypt_cleanup(a):
    """After an aborted destructive test: unlock (password, then recovery key) and disable encryption,
    unless --keep-encrypted."""
    if not S.crypt_by_test:
        return
    enabled, unlocked = crypt_state()
    if enabled is not True:
        S.crypt_by_test = False
        return
    if S.keep_encrypted:
        return
    if unlocked is False:
        r = cpost("/api/crypt/unlock", {"password": S.crypt_pw})
        if not okr(r) and S.recovery:
            r = cpost("/api/crypt/unlock", {"recovery_key": S.recovery})
        if not okr(r):
            check("cleanup: unlock the test-encrypted drive", False,
                  f"{r.status_code} {jcode(r)}; recovery key: {S.recovery}")
            return
    ensure_app()
    r = cpost("/api/crypt/disable", {"password": S.crypt_pw})
    ok = okr(r)
    check("cleanup: encryption of the test removed (drive plain again)", ok,
          "" if ok else f"{r.status_code} {jcode(r)}; recovery key: {S.recovery}")
    if ok:
        S.crypt_by_test = False
        wait_online()


def cleanup(a):
    head("Cleanup")
    if S.main is None:
        note("no main session, nothing to clean up")
        return
    try:
        if call("GET", "/api/status", timeout=10).status_code != 200:
            note("main session no longer valid, cannot clean up through the API")
            return
        crypt_cleanup(a)
        if S.autolock_orig is not None:
            m, d = S.autolock_orig
            r = call("POST", "/api/settings/autolock", params={"minutes": str(m), "on_detach": "1" if d else "0"})
            check("automatic lock settings restored", okr(r), r.text[:100])
        if S.lang_orig is not None:
            r = call("POST", "/api/settings/lang", params={"value": S.lang_orig})
            check("display language restored", okr(r), r.text[:100])
        if S.T_made or S.extra_paths:
            if ensure_app():
                res = (status().get("upload") or {}).get("resumable") or {}
                if S.T_made and str(res.get("path", "")).startswith(S.T + "/"):
                    upload(f"{S.T}/_discard.3mf", make_3mf(1))      # another upload discards the partial one
                    check("partial upload of the test discarded", not (status().get("upload") or {}).get("resumable"))
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
        if S.relock:
            r = cpost("/api/crypt/lock")
            check("drive locked again (original state)", okr(r) and crypt_state() == (True, False),
                  f"{r.status_code} {jcode(r)}")
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


SECTIONS = {"users": sec_users, "devices": sec_devices, "files": sec_files, "wifi": sec_wifi, "status": sec_status,
            "drive": sec_drive, "scan": sec_scan, "crypt": sec_crypt, "https": sec_https,
            "crypt_test": sec_crypt_destructive, "time": sec_time, "autolock": sec_autolock, "lang": sec_lang,
            "resume": sec_resume, "zip": sec_zip, "webdav": sec_webdav}
V5_SECTIONS = ["time", "autolock", "lang", "resume", "zip", "webdav"]


def main():
    ap = argparse.ArgumentParser(description="End-to-end test of the WiFi DataDrive API v3 + v4 + v5")
    ap.add_argument("host", help="HOST or HOST:PORT")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", required=True)
    ap.add_argument("--drive", help="Windows drive letter of the dongle, e.g. E:")
    ap.add_argument("--size-kb", type=int, default=512)
    ap.add_argument("--skip-destructive", action="store_true",
                    help="do not take the drive from USB / switch the mode")
    ap.add_argument("--wifi-test", action="store_true", help="add and remove a dummy Wi-Fi network")
    ap.add_argument("--ota-probe", action="store_true", help="POST a tiny invalid body to /api/ota")
    ap.add_argument("--drive-password", help="drive password: unlocks a locked drive first; needed for --crypt-test")
    ap.add_argument("--crypt-test", action="store_true",
                    help="DESTRUCTIVE: set up / lock / unlock / change / disable drive encryption (erases the drive)")
    ap.add_argument("--i-know-this-erases-the-drive", action="store_true", help="confirmation for --crypt-test")
    ap.add_argument("--keep-encrypted", action="store_true", help="with --crypt-test: stop before disable")
    ap.add_argument("--only", help="comma-separated sections to run after the login, e.g. v5 or time,zip "
                                   f"(names: {', '.join(SECTIONS)}, v5)")
    a = ap.parse_args()
    only = None
    if a.only:
        only = []
        for name in (x.strip().lower() for x in a.only.split(",") if x.strip()):
            names = V5_SECTIONS if name == "v5" else [name]
            for n in names:
                if n not in SECTIONS:
                    ap.error(f"--only: unknown section '{n}' (known: {', '.join(SECTIONS)}, v5)")
                if n not in only:
                    only.append(n)
        if "crypt_test" in only and not a.crypt_test:
            ap.error("--only crypt_test needs --crypt-test")
    if a.crypt_test:
        if not a.i_know_this_erases_the_drive or not a.drive_password:
            ap.error("--crypt-test erases the drive: it needs BOTH --i-know-this-erases-the-drive and --drive-password PW")
        if a.skip_destructive:
            ap.error("--crypt-test cannot be combined with --skip-destructive")
        if len(a.drive_password) < 12 or len(a.drive_password.encode("utf-8")) > 64:
            ap.error("--drive-password for --crypt-test needs 12 characters at least and 64 bytes at most")
    else:
        if a.keep_encrypted:
            ap.error("--keep-encrypted only makes sense with --crypt-test")
        if a.i_know_this_erases_the_drive:
            print("note: --i-know-this-erases-the-drive without --crypt-test does nothing", flush=True)
    S.keep_encrypted = a.keep_encrypted
    if a.crypt_test:
        print("!!! --crypt-test: the drive of the target will be FORMATTED twice (setup and disable).\n"
              "!!! ALL FILES ON THE MICROSD CARD WILL BE LOST (not recoverable). Encryption is switched on,\n"
              "!!! used, and " + ("LEFT ON (--keep-encrypted)." if a.keep_encrypted else "switched off again.")
              + "\n!!! Press Ctrl+C within 5 seconds to abort.", flush=True)
        time.sleep(5)
    S.host, _, p = a.host.partition(":")
    S.port = int(p) if p else 80
    S.base = f"http://{S.host}:{S.port}"
    S.user, S.password, S.skip_destructive = a.user, a.password, a.skip_destructive
    code = 0
    try:
        fns = [sec_auth]
        done = []
        while True:
            for fn in fns:
                if fn in done:
                    continue
                done.append(fn)
                try:
                    fn(a)
                except Abort:
                    raise
                except Exception as e:      # a broken section must not hide the others or skip the cleanup
                    check(f"{fn.__name__} ran without exception", False, repr(e))
            if len(fns) > 1:
                break
            if S.damaged:       # nothing but the crypt endpoints works on a damaged drive
                fns = [sec_auth, sec_crypt_damaged] + ([sec_crypt_destructive] if a.crypt_test else [])
            elif only:
                fns = [sec_auth] + [SECTIONS[n] for n in only]
            else:
                fns = [sec_auth, sec_users, sec_devices, sec_files, sec_wifi, sec_status, sec_drive, sec_scan, sec_crypt,
                       sec_https, sec_time, sec_autolock, sec_lang, sec_resume, sec_zip, sec_webdav]
                if a.crypt_test:
                    fns.append(sec_crypt_destructive)
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
