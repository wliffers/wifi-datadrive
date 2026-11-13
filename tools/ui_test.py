#!/usr/bin/env python3
"""Browser test of the WiFi DataDrive web UI (index.html, API v3, v4 and v5 / 0.5.0) against tools/mock_server.py.

Drives the single-file UI in a real browser engine (Playwright + the installed Edge or
Chrome) and prints one `[OK ]` / `[FAIL]` line per check plus a summary. Exit code 1 if a
check failed. Nothing here talks to a real dongle - only to the mock server.

Requirements
------------
    pip install playwright          (no `playwright install` needed: the script uses the
                                     browser that is already installed, see --channel)
    python tools/mock_server.py --port 8090      (must be running; the script switches its
                                     scenario and settings through /mock/scenario and /mock/set)

Usage
-----
    python tools/ui_test.py [--base http://localhost:8090] [--shots DIR] [--headed]
                            [--channel msedge|chrome] [--profile desktop|phone|both]
                            [--only TEXT] [--no-shots] [--list]

    --only TEXT   run only the tests whose name contains TEXT (e.g. --only clipboard)
    --shots DIR   screenshot folder (default: environment variable DATADRIVE_UI_SHOTS, else
                  <system temp>/datadrive-ui-test/shots); every significant state is saved in the
                  light and in the dark colour scheme as <profile>_<scheme>_<state>.png

What it does
------------
Every test resets the mock (`/mock/scenario?name=...`, which also ends all sessions), opens
a fresh browser context and signs in through the real login form, so the tests are
independent. Each flow runs in two profiles:
    desktop  1280x800
    phone    390x844, device_scale_factor 3, is_mobile, has_touch, iPhone Safari user agent
The contexts are created with color_scheme="light"; for screenshots the scheme is switched
with page.emulate_media() so both themes show exactly the same state.

Covered: sign-in (wrong password, empty fields, "trust this device", lock-out, expired
session, sign-out), the forced password change, the file list with folders (create, enter,
breadcrumb, browser back, deep links, missing folder), upload into a folder, rename, cut /
copy / paste (also name clashes and the busy overlay), multi-select and delete, download,
switching the owner (USB drive / file management, host active, host writing, another client
uploading), all settings sections (theme, language, Wi-Fi list, users, trusted devices,
firmware update dialog, log, restart), the role `user` (no admin functions), German and
English texts (no untranslated or raw key texts visible) and, in every view, no horizontal
scrolling. API v4 (docs/12_api_v4.md): the encrypted drive (unlock view with wrong password,
lock-out countdown and recovery key, 423 DRIVE_LOCKED during use, "lock now", settings: set up with
the recovery-key dialog, change the drive password, turn off, role `user`), the Wi-Fi scan list in the
"add network" dialog (loading, secured / open / saved networks, search again, BUSY, odd SSIDs, manual
entry), the new lock-out wording and the 503 AUTH_UNAVAILABLE notice. API v5 (docs/15_api_v5.md, version 0.5.0):
device clock and time zone (synced / set by the browser / not set), true UTC dates in the list and sorting by date,
resumable uploads (automatic retries, "Fortsetzen" after three failures, RESUME_MISMATCH, the hint for an unfinished
upload), folder upload through the folder picker (existing folders, summary), folder ZIP download (single folder, mixed
selection, refusals WRONG_OWNER / TOO_MANY_FILES), automatic lock settings, the display language, the network drive
card (WebDAV), the captive portal mode (/?captive=1), the HTTPS section with and without the HTTPS fields, English
texts and narrow windows (390 / 320 px). Where the mock cannot produce a state (status without `webdav` or without the
HTTPS fields) the test rewrites the /api/status answer inside the browser (page.route).
UI redesign (0.5.x): the mode card (segmented control "Dateiverwaltung | USB-Gerät" with a sliding thumb, a tap during a
running switch is carried out afterwards), the encryption card, the collapsible settings cards (they start closed: tests
open a card with open_card() / to_sec() first), the ⓘ popovers (tap, outside tap, Escape with focus return, one at a
time, "Anleitung öffnen") and the local help page (#!hilfe/<topic>, also signed out), the rules against doubled actions
(one "Hochladen" with a menu, no floating button, "Entsperren" once, "Sperren" not in the user menu) and reduced motion.
API v6 (docs/16_api_v6.md): one request at a time (DEVICE_JS models the device with HTTPS: one connection, a second one
closes the first; regression test for the lost auto-lock setting), one quiet retry of requests that are safe to repeat,
the auto-lock countdown in the encryption card (ticking, reset, warning colour, none in USB mode and the full period after
switching back, waiting = lock attempt refused, lock at 0), the address and
network name items (DHCP / fixed with validation at the fields, the "now reachable at" notice with the reachability
check), signed updates (key line, BAD_SIGNATURE / UNSIGNED) and a folder upload whose name is taken by a file.

The page is instrumented only by an init script that records toasts and dialog openings;
index.html itself is not modified. "Failed to load resource" lines of the browser console for
HTTP 4xx/5xx answers are ignored (the UI asks /api/session while signed out and several tests
provoke API errors on purpose); every other console error and every uncaught exception counts.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright
except ImportError:                                   # pragma: no cover
    print("ui_test: Playwright is missing - run `pip install playwright`", file=sys.stderr)
    sys.exit(2)

DEFAULT_SHOTS = Path(os.environ.get("DATADRIVE_UI_SHOTS") or Path(tempfile.gettempdir()) / "datadrive-ui-test" / "shots")

PHONE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1")
PROFILES = {
    "desktop": dict(viewport={"width": 1280, "height": 800}),
    "phone": dict(viewport={"width": 390, "height": 844}, device_scale_factor=3, is_mobile=True,
                  has_touch=True, user_agent=PHONE_UA),
}

ADMIN = ("admin", "admin123")
GUEST = ("gast", "gast123")
T_USB = "segUsb"                 # ui()["title"]: the pressed segment of the mode card (who has the drive) ...
T_APP = "segApp"                 # ... or, with no segment pressed (locked / unreadable), the status text
T_TAKEOVER = "Laufwerk vom USB-Gerät trennen?"
ROOT_DIRS = ["Größen Prüfung äöü", "Leerer Ordner", "Projekte"]          # the folders of scenario `files`, sorted
ROOT_COUNT = 17                                                          # 14 files + 3 folders

# ------------------------------------------------------------------ page instrumentation

INIT_JS = r"""
window.__toasts = []; window.__dialogs = [];
(() => {
  const sm = HTMLDialogElement.prototype.showModal;
  HTMLDialogElement.prototype.showModal = function(){
    const t = this.querySelector('#dTitle');
    window.__dialogs.push({t: performance.now(), title: t ? t.textContent : ''});
    return sm.apply(this, arguments);
  };
  document.addEventListener('DOMContentLoaded', () => {
    const box = document.getElementById('toasts');
    if (box) new MutationObserver(ms => { for (const m of ms) for (const n of m.addedNodes)
      window.__toasts.push({t: performance.now(), text: n.textContent, err: n.classList.contains('err')}); })
      .observe(box, {childList: true});
  });
})();
"""

JS_UI = r"""() => {
  const q = s => document.querySelector(s);
  const vis = el => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
  // the mode switch is the segment of the side that does not have the drive (none while locked / unreadable)
  const b = [...document.querySelectorAll('.seg2 button')].find(x => x.getAttribute('aria-pressed') !== 'true' && !x.disabled) || null, st = q('#list .state');
  const on = [...document.querySelectorAll('.seg2 button')].find(x => x.getAttribute('aria-pressed') === 'true');
  const crumbs = [...document.querySelectorAll('#crumbs .cr')].filter(vis).map(e => e.textContent.trim());
  return {
    auth: S.auth, login: vis(q('#vLogin')), pw: vis(q('#vPw')), app: vis(q('#app')), header: vis(q('header.top')),
    title: on ? on.id : q('#modeTitle').textContent, status: q('#modeTitle').textContent, sub: q('#modeInfo').dataset.x || '', modeCls: q('#modeCell').className,
    seg: on ? on.id : '', thumb: q('.seg2').dataset.on || '',
    btn: vis(b) ? b.textContent.trim() : null, btnDisabled: !b,
    stoTitle: q('#stoTitle').textContent, stoSub: q('#stoSub').textContent,
    count: q('#count').textContent, h1: q('#title').textContent, crumbs, crumbsVisible: vis(q('#crumbs')),
    rows: [...document.querySelectorAll('#list .row')].map(r => r.dataset.name),
    dirs: [...document.querySelectorAll('#list .row.dir')].map(r => r.dataset.name),
    skel: document.querySelectorAll('#list .skel').length,
    stateH: st && st.querySelector('h2') ? st.querySelector('h2').textContent.trim() : null,
    notice: q('#notice').hidden ? '' : q('#notice').textContent.trim(),
    files: vis(q('#vFiles')), settings: vis(q('#vSettings')), hash: location.hash, dir: S.dir,
    selbar: vis(q('#selbar')), selCount: q('#selCount').textContent, clipbar: vis(q('#clipbar')),
    clipTxt: q('#clipTxt').textContent, busy: vis(q('#busy')), busyT: q('#busyT').textContent,
    toolbar: vis(q('#toolbar')), mkBtn: vis(q('#mkBtn')), upBtn: vis(q('#upBtn')), fab: vis(q('#fab')),
    search: vis(q('#search')), dlgOpen: q('#dlg').open, lang: document.documentElement.lang
  };
}"""

JS_DLG = r"""() => {
  const q = s => document.querySelector(s), d = q('#dlg');
  return {open: d.open, title: q('#dTitle').textContent, body: q('#dBody').textContent, err: q('#dErr').textContent,
    yes: q('#dYes').textContent, no: q('#dNo').textContent, yesCls: q('#dYes').className,
    yesDisabled: q('#dYes').disabled,
    fields: [...document.querySelectorAll('#dFields input, #dFields select')].map(i =>
      ({id: i.id, type: i.type || i.tagName.toLowerCase(), value: i.value, label: (document.querySelector('label[for="' + i.id + '"]') || {textContent: ''}).textContent})),
    focus: document.activeElement ? document.activeElement.id : ''};
}"""

JS_MENU = r"""() => {
  const m = document.querySelector('#menu'), r = m.getBoundingClientRect();
  return {hidden: m.hidden, rect: {l: r.left, t: r.top, r: r.right, b: r.bottom}, vw: innerWidth, vh: innerHeight,
    items: [...m.querySelectorAll('[data-act]')].map(b => b.textContent.trim()), head: m.querySelector('.mhead').textContent.trim()};
}"""

# Elements that stick out of the viewport horizontally (content of scroll containers is ignored).
JS_OVERFLOW = r"""() => {
  const W = innerWidth, out = [];
  for (const el of document.body.querySelectorAll('*')) {
    if (el.closest('svg') && el.tagName.toLowerCase() !== 'svg') continue;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    let p = el.parentElement, clipped = false;
    while (p && p !== document.body) { if (getComputedStyle(p).overflowX !== 'visible') { clipped = true; break; } p = p.parentElement; }
    if (clipped) continue;
    if (r.right > W + 0.5 || r.left < -0.5)
      out.push((el.id ? '#' + el.id : el.tagName.toLowerCase() + '.' + el.className) + ' ' + Math.round(r.left) + '..' + Math.round(r.right));
  }
  return {scrollW: document.documentElement.scrollWidth, innerW: W, out: out.slice(0, 6)};
}"""

JS_NAMES = r"""() => {
  const vis = el => { const cs = getComputedStyle(el), r = el.getBoundingClientRect();
    return cs.display !== 'none' && cs.visibility !== 'hidden' && r.width > 0 && r.height > 0; };
  const name = el => {
    const al = el.getAttribute('aria-label'); if (al && al.trim()) return al.trim();
    if (el.matches('input,select,textarea')) {
      if (el.id) { const l = document.querySelector('label[for="' + el.id + '"]'); if (l && l.textContent.trim()) return l.textContent.trim(); }
      const wl = el.closest('label'); if (wl && wl.textContent.trim()) return wl.textContent.trim();
      return (el.getAttribute('title') || '').trim();
    }
    return el.textContent.trim() || (el.getAttribute('title') || '').trim();
  };
  const bad = []; let n = 0;
  for (const el of document.querySelectorAll('button,a[href],input:not([type=hidden]),select,[role=menuitem]')) {
    if (!vis(el)) continue; n++;
    if (!name(el)) bad.push(el.outerHTML.slice(0, 100));
  }
  return {n, bad};
}"""

JS_TARGETS = r"""(sels) => {
  const res = [];
  for (const sel of sels) {
    const els = [...document.querySelectorAll(sel)].filter(e => e.getClientRects().length);
    if (!els.length) { res.push({sel, missing: true}); continue; }
    const r = els[0].getBoundingClientRect();
    res.push({sel, w: Math.round(r.width * 10) / 10, h: Math.round(r.height * 10) / 10});
  }
  return res;
}"""

# Visible texts (and aria-label / placeholder / title attributes) that are in the wrong language or look like a raw
# dictionary key. L is the UI's own dictionary (de / en); a text counts as foreign when it equals a string of the
# other language that the current language does not share.
# Section chips of the settings (visible ones). The optional "Security" chip (HTTPS, only when the firmware reports it) is
# left out: it is not part of the contract of these tests.
JS_TABS = r"""() => [...document.querySelectorAll('.sec')].filter(s => s.getClientRects().length).map(s => s.querySelector('.sechead b').textContent)"""

# The ⓘ popover: open state, text, guide link, position, how many (i) buttons are expanded.
JS_POP = r"""() => { const p = document.querySelector('#pop'), r = p.getBoundingClientRect(), a = document.querySelector('#popA');
  return {open: !p.hidden, text: document.querySelector('#popT').textContent, link: a.hidden ? '' : a.getAttribute('href'), linkTxt: a.textContent,
    l: r.left, r: r.right, t: r.top, b: r.bottom, vw: innerWidth, vh: innerHeight,
    expanded: [...document.querySelectorAll('.ib[aria-expanded=true]')].map(b => b.id || b.dataset.i || '?'), focus: document.activeElement ? document.activeElement.id : ''}; }"""

JS_UNTRANSLATED = r"""() => {
  const cur = L[lang], oth = L[lang === 'de' ? 'en' : 'de'];
  const strs = d => { const s = new Set(); for (const [k, v] of Object.entries(d)) {
    if (k === 'E') { for (const x of Object.values(v)) if (typeof x === 'string') s.add(x); } else if (typeof v === 'string') s.add(v); } return s; };
  const mine = strs(cur), foreign = strs(oth), keys = new Set(Object.keys(L.de).filter(k => L.en[k] !== k));
  const bad = [], seen = new Set();
  const visible = el => { for (let e = el; e && e !== document.body; e = e.parentElement) {
    if (e.hidden || getComputedStyle(e).display === 'none' || getComputedStyle(e).visibility === 'hidden') return false; } return true; };
  const test = (text, where) => {
    text = text.replace(/\s+/g, ' ').trim();
    if (!text || seen.has(text)) return; seen.add(text);
    if ((foreign.has(text) && !mine.has(text)) || keys.has(text) || /undefined|\[object|NaN/.test(text)) bad.push(where + ': ' + text.slice(0, 60));
  };
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n; (n = w.nextNode());) {
    const p = n.parentElement;
    if (!p || p.closest('script,style,svg,.row,#toasts') || !visible(p)) continue;
    const lg = p.closest('[lang]'); if (lg && lg !== document.documentElement) continue;          // "Deutsch" / "English" stay native
    if (p.matches('.txt') && p.closest('#devList,#userList,#wifiList')) continue;                 // data: device label, user, SSID
    test(n.textContent, p.id ? '#' + p.id : p.tagName.toLowerCase());
  }
  for (const el of document.querySelectorAll('[aria-label],[placeholder],[title]')) {
    if (el.closest('.row,svg') || !visible(el)) continue;
    for (const a of ['aria-label', 'placeholder', 'title']) if (el.getAttribute(a)) test(el.getAttribute(a), a);
  }
  return {lang, bad: bad.slice(0, 8), n: seen.size};
}"""


# ------------------------------------------------------------------ helpers

def basic(user: str, pw: str) -> dict:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


class Mock:
    def __init__(self, base: str):
        self.base = base

    def get(self, path: str, timeout: float = 90, headers: dict | None = None):
        req = urllib.request.Request(self.base + path, headers=headers or {})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def scenario(self, name: str):
        return self.get("/mock/scenario?name=" + urllib.parse.quote(name))

    def set(self, **kw):
        return self.get("/mock/set?" + urllib.parse.urlencode(kw))

    def state(self) -> dict:
        return self.get("/mock/state")

    def ls(self, dir_: str = "", auth=ADMIN) -> dict:
        """{name: entry} of a folder, asked through the real API (HTTP Basic)."""
        j = self.api("/api/files?dir=" + urllib.parse.quote(dir_), auth)
        return {e["name"]: e for e in j.get("entries", [])}

    def api(self, path: str, auth=ADMIN) -> dict:
        try:
            return self.get(path, headers=basic(*auth))
        except urllib.error.HTTPError as e:
            return json.loads(e.read().decode("utf-8"))

    def post(self, path: str, auth=ADMIN) -> dict:
        """POST through the real API (HTTP Basic, CSRF header), parameters in the URL."""
        req = urllib.request.Request(self.base + path, data=b"", method="POST", headers={**basic(*auth), "X-PrintDrive": "1"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return json.loads(e.read().decode("utf-8"))

    def raw(self, path: str, auth=ADMIN, method="GET", headers: dict | None = None) -> tuple:
        """(HTTP status, body bytes) of any request (downloads, /dav/)."""
        req = urllib.request.Request(self.base + path, method=method, headers={**basic(*auth), **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()


class Run:
    def __init__(self, base: str, shots: Path | None, headed: bool):
        self.base = base.rstrip("/")
        self.shots = shots
        self.headed = headed
        self.mock = Mock(self.base)
        self.results = []
        self.tmp = Path(tempfile.mkdtemp(prefix="ddui_"))
        self.f = {}
        self._make_files()

    def _make_files(self):
        def put(key, name, data):
            p = self.tmp / name
            p.write_bytes(data)
            self.f[key] = p
        put("txt", "ui-test notiz.txt", ("Zeile mit Umlauten äöüß\n" * 80).encode("utf-8"))
        put("odd", "Tom's Teil & Entwurf #1 100%.txt", b"Sonderzeichen im Namen\n" * 40)
        put("empty", "leer.txt", b"")
        put("fw", "printdrive.bin", b"\xe9" + os.urandom(80 * 1024))               # first byte 0xE9 like an ESP32 image
        put("fw_bad", "falsch.bin", b"\x00" + os.urandom(80 * 1024))
        put("fw_small", "klein.bin", b"\xe9" + os.urandom(2000))
        put("fw_txt", "firmware.txt", b"\xe9" + os.urandom(80 * 1024))
        # API v5: files for the resumable upload (256 KiB each, content differs) and folders for the folder picker
        for key, name in (("big", "Großer Upload.txt"), ("big2", "Fortsetzen Test.txt"), ("big3", "Mismatch Test.txt"), ("big4", "Teil wieder.txt")):
            put(key, name, os.urandom(256 * 1024))

        def tree(key, top, files):
            root = self.tmp / ("tree_" + key)
            for rel, data in files.items():
                p = root / top / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
            self.f[key] = root / top
        txt = lambda n: ("Inhalt %s äöü\n" % n * 900).encode("utf-8")           # about 15 KB
        tree("tree_new", "Neuer Upload Ordner", {"a.txt": txt("a"), "Unter/b.txt": txt("b"), "Unter/Tief/c.txt": txt("c"),
                                                 ".DS_Store": b"\0" * 64, ".versteckt/geheim.txt": b"geheim"})
        tree("tree_exist", "Projekte", {"neu2.txt": txt("n2"), "Drachen/neu.txt": txt("n")})
        tree("tree_fail", "Mit Fehler", {"ok.txt": txt("ok"), "leer.txt": b""})
        tree("tree_file", "README", {"inhalt.txt": txt("i")})

    def check(self, name: str, cond, info=""):
        ok = bool(cond)
        self.results.append((name, ok))
        info = re.sub(r"\s*\n\s*", " ", str(info))
        if len(info) > 300:
            info = info[:300] + " …"
        print(f"[{'OK ' if ok else 'FAIL'}] {name} {info}".rstrip(), flush=True)
        return ok

    def info(self, text: str):
        print(f"[INFO] {text}", flush=True)


def real_console_error(line: str, net=False) -> bool:
    if "Failed to load resource" in line and ("status of 4" in line or "status of 5" in line):
        return False
    return not (net and "net::ERR_" in line)


class Ctx:
    """One profile (desktop or phone); opens a fresh browser context per page."""

    def __init__(self, run: Run, browser, prof: str):
        self.run, self.browser, self.prof = run, browser, prof
        self.phone = prof == "phone"
        self.mock = run.mock
        self.context = None
        self.page = None
        self.console = []
        self.requests = []

    # ---- lifecycle
    def close(self):
        if self.context is not None:
            try:
                self.context.close()
            except Exception:
                pass
        self.context = self.page = None

    def reset(self, scenario: str, **sets):
        self.close()
        self.mock.scenario(scenario)
        if sets:
            self.mock.set(**sets)

    def open(self, hash_: str = "", query: str = "", init: str = "", **ctx_kw):
        """Open the page and wait until the sign-in view (or the app, with a trusted cookie) is there.
        init: an extra init script (e.g. DEVICE_JS, the one-connection model of the device)."""
        self.close()
        opts = dict(PROFILES[self.prof])
        opts.update(color_scheme="light", locale="de-DE", accept_downloads=True)
        opts.update(ctx_kw)
        self.context = self.browser.new_context(**opts)
        self.context.add_init_script(INIT_JS)
        if init:
            self.context.add_init_script(init)
        p = self.page = self.context.new_page()
        p.set_default_timeout(8000)
        self.console, self.requests = [], []
        p.on("console", lambda m: self.console.append(f"{m.type}: {m.text}") if m.type == "error" else None)
        p.on("pageerror", lambda e: self.console.append(f"pageerror: {e}"))
        p.on("request", lambda r: self.requests.append((r.method, r.url)))
        p.goto(self.run.base + "/" + query + hash_, timeout=30000)
        self.wait("() => typeof S !== 'undefined' && S.auth !== ''", timeout=15000)
        return p

    def login(self, who=ADMIN, trust=False, wait=True):
        p = self.page
        p.fill("#lUser", who[0])
        p.fill("#lPass", who[1])
        p.eval_on_selector("#lTrust", "(e, v) => { e.checked = v; }", trust)
        self.tap("#loginBtn")
        if wait:
            self.wait_ready()

    def wait_ready(self, timeout=15000) -> bool:
        """Signed in, status loaded and (file mode) the folder listed."""
        return self.wait("() => S.auth === 'ok' && !!S.st && (S.entries !== null || S.st.drive.owner !== 'app' || !S.st.drive.fs_ok)", timeout=timeout)

    def start(self, scenario="files", who=ADMIN, hash_="", trust=False, **sets):
        """Reset the mock, open the page, sign in (once more if the single-threaded mock stalled the first try)."""
        for attempt in (1, 2):
            try:
                self.reset(scenario, **sets)
                self.open(hash_)
                self.login(who, trust)
                return self.page
            except PWTimeout:
                if attempt == 2:
                    raise
                print("[INFO] start: timeout while signing in, trying once more", flush=True)

    # ---- reporting
    def check(self, name, cond, info=""):
        return self.run.check(f"{self.prof}: {name}", cond, info)

    def shot(self, name: str, full: bool = False):
        if not self.run.shots or not self.page:
            return
        for scheme in ("light", "dark"):
            try:
                self.page.emulate_media(color_scheme=scheme)
                self.page.screenshot(path=str(self.run.shots / f"{self.prof}_{scheme}_{name}.png"), full_page=full)
            except Exception as e:                 # a screenshot must never break a test
                print(f"[INFO] screenshot {name} failed: {e}")
        self.page.emulate_media(color_scheme="light")

    def no_console_errors(self, label="no JS console errors", net=False):
        """net=True: ignore net::ERR_* lines (tests that restart or disconnect the mock on purpose)."""
        bad = [m for m in self.console if real_console_error(m, net)]
        self.check(label, not bad, bad[:3])

    # ---- page access
    def ev(self, js, arg=None):
        return self.page.evaluate(js, arg)

    def wait(self, js, arg=None, timeout=7000) -> bool:
        try:
            self.page.wait_for_function(js, arg=arg, timeout=timeout)
            return True
        except PWTimeout:
            return False

    def ui(self):
        return self.ev(JS_UI)

    def dlg(self):
        return self.ev(JS_DLG)

    def menu(self):
        self.settled("#menu")
        return self.ev(JS_MENU)

    def settled(self, sel: str, timeout=2000) -> bool:
        """Waits until the element's enter/exit transition is over (geometry is read only then)."""
        return self.wait("s => { const e = document.querySelector(s); return !e || !e.getAnimations().length; }", sel, timeout)

    def text(self, sel):
        return self.ev("s => document.querySelector(s).textContent", sel)

    def toasts(self):
        return self.ev("() => window.__toasts")

    def toast_texts(self):
        return [t["text"] for t in self.toasts()]

    def clear_toasts(self):
        self.ev("() => { window.__toasts.length = 0; }")

    def wait_toast(self, sub: str, timeout=8000) -> bool:
        return self.wait("s => window.__toasts.some(t => t.text.includes(s))", sub, timeout)

    def wait_dlg(self, title: str, timeout=7000) -> bool:
        return self.wait("t => document.querySelector('#dlg').open && document.querySelector('#dTitle').textContent.includes(t)",
                         title, timeout)

    def wait_dlg_closed(self, timeout=7000) -> bool:
        return self.wait("() => !document.querySelector('#dlg').open", None, timeout)

    def wait_derr(self, timeout=7000) -> bool:
        return self.wait("() => document.querySelector('#dErr').textContent.trim() !== '' && !document.querySelector('#dYes').disabled",
                         None, timeout)

    def wait_rows(self, names, timeout=8000) -> bool:
        return self.wait("n => n.every(x => [...document.querySelectorAll('#list .row')].some(r => r.dataset.name === x))", names, timeout)

    def wait_gone(self, name: str, timeout=8000) -> bool:
        return self.wait("n => ![...document.querySelectorAll('#list .row')].some(r => r.dataset.name === n)", name, timeout)

    def wait_dir(self, d: str, timeout=8000) -> bool:
        return self.wait("d => S.dir === d && S.entries !== null", d, timeout)

    # ---- input
    def tap(self, target, **kw):
        loc = self.page.locator(target) if isinstance(target, str) else target
        for attempt in (1, 2):                 # the single-threaded mock sometimes stalls the page for seconds: retry once
            try:
                if self.phone:
                    loc.tap(**kw)
                else:
                    loc.click(**kw)
                return
            except PWTimeout:
                if attempt == 2:
                    raise
                print(f"[INFO] tap/click on {target!r} timed out, trying once more", flush=True)

    def row(self, name: str):
        return self.page.locator(f'#list .row[data-name="{name}"]')

    def open_menu(self, name: str) -> bool:
        self.tap(self.row(name).locator("[data-more]"))
        return self.wait("() => !document.querySelector('#menu').hidden")

    def act(self, name: str, action: str):
        self.open_menu(name)
        self.tap(f'#menu [data-act="{action}"]')

    def go(self, hash_: str):
        self.ev("h => { location.hash = h; }", hash_)

    def new_folder(self):
        """'Neuer Ordner': its own button on the desktop, an item of the 'Hochladen' menu on the phone."""
        if self.phone:
            self.tap("#upBtn")
            self.wait("() => !document.querySelector('#menu').hidden")
            self.tap('#menu [data-act="mk"]')
        else:
            self.tap("#mkBtn")

    def pop(self):
        return self.ev(JS_POP)

    def choose_files(self, paths):
        """'Hochladen' › 'Dateien hochladen' through the real file chooser (the button opens a menu)."""
        self.tap("#upBtn")
        self.wait("() => !document.querySelector('#menu').hidden", None, 3000)
        with self.page.expect_file_chooser() as fc:
            self.tap('#menu [data-act="file"]')
        fc.value.set_files(paths)

    def switch(self):
        """Taps the segment of the mode card that does not have the drive (activate / hand over)."""
        self.tap('.seg2 button[aria-pressed="false"]:not(:disabled)')

    def fill_dlg(self, *vals):
        for i, v in enumerate(vals):
            sel = f"#dF{i}"
            if self.ev("s => document.querySelector(s).tagName", sel) == "SELECT":
                self.page.select_option(sel, v)
            elif self.ev("s => document.querySelector(s).type", sel) == "checkbox":
                self.page.set_checked(sel, bool(v))
            else:
                self.page.fill(sel, v)

    def dlg_yes(self):
        self.tap("#dYes")

    def dlg_no(self):
        self.tap("#dNo")

    def overflow_check(self, label: str):
        # enter/exit transitions (transform) must be over; spinners and the loading pulse loop forever and are ignored
        self.wait("() => !document.getAnimations().some(a => a.effect && a.effect.getTiming().iterations !== Infinity)", None, 2000)
        o = self.ev(JS_OVERFLOW)
        self.check(f"{label}: no horizontal scrolling, nothing outside the viewport",
                   o["scrollW"] <= o["innerW"] and not o["out"], f"scrollWidth {o['scrollW']} / {o['innerW']} {o['out'] or ''}")

    def select_rows(self, first: str, *more):
        """Selection mode through the row menu ('Auswählen'), then the other rows (works with mouse and touch)."""
        self.act(first, "sel")
        self.wait("() => S.sel.size > 0")
        for n in more:
            self.tap(self.row(n).locator(".name"))
        self.wait("n => S.sel.size === n", 1 + len(more))


TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def login_note(c: Ctx) -> str:
    return c.ev("() => { const n = document.querySelector('#loginNote'); return n.hidden ? '' : n.textContent; }")


# ================================================================== 1. sign-in

@test
def t01a_login(c: Ctx):
    c.reset("two_users")
    p = c.open()
    ui = c.ui()
    c.check("first visit: sign-in view only, no header, no app", ui["auth"] == "login" and ui["login"] and not ui["app"] and not ui["header"], ui["auth"])
    texts = c.ev("() => ({u: document.querySelector('label[for=lUser]').textContent, "
                 "p: document.querySelector('label[for=lPass]').textContent, btn: document.querySelector('#loginBtn').textContent, "
                 "trust: document.querySelector('#lTrust').checked, lang: document.querySelector('#loginForm [data-lang]').textContent, note: document.querySelector('#loginNote').hidden})")
    c.check("sign-in texts (German) and 'trust this device' ticked by default",
            texts["u"] == "Benutzername" and texts["p"] == "Passwort" and texts["btn"] == "Anmelden" and texts["trust"]
            and texts["lang"] == "English" and texts["note"], texts)
    c.check("focus starts in the user name field", c.ev("() => document.activeElement.id") == "lUser")
    c.shot("login")
    c.overflow_check("sign-in")
    c.tap("#loginBtn")
    c.check("empty submit: hint, still signed out", c.text("#loginMsg") == "Bitte Benutzername und Passwort eingeben." and c.ui()["auth"] == "login", c.text("#loginMsg"))
    p.fill("#lUser", "admin")
    p.fill("#lPass", "falsch")
    c.tap("#loginBtn")
    ok = c.wait("() => document.querySelector('#loginMsg').textContent !== '' && !document.querySelector('#loginBtn').disabled")
    c.check("wrong password: German error text, still signed out, focus back in the password field",
            ok and c.text("#loginMsg") == "Benutzername oder Passwort ist falsch." and c.ui()["auth"] == "login"
            and c.ev("() => document.activeElement.id") == "lPass", c.text("#loginMsg"))
    c.shot("login_wrong_password")
    c.overflow_check("sign-in with error")
    # success without trusting the device
    p.fill("#lPass", ADMIN[1])
    p.set_checked("#lTrust", False)
    c.tap("#loginBtn")
    ok = c.wait_ready()
    ui = c.ui()
    c.check("success: file view, header and avatar, sign-in view gone", ok and ui["app"] and ui["header"] and not ui["login"] and ui["files"]
            and c.text("#userAv") == "a", f"auth {ui['auth']}")
    c.check("untrusted sign-in: no device stored in the mock", len(c.mock.state()["devices"]) == 0, c.mock.state()["devices"])
    c.check("password field cleared after sign-in", c.ev("() => document.querySelector('#lPass').value") == "")
    c.shot("after_login")
    p.reload()
    ok = c.wait_ready()
    c.check("reload: the session cookie keeps the browser signed in", ok and c.ui()["app"], c.ui()["auth"])
    # sign out through the account menu
    c.tap("#userBtn")
    c.wait("() => !document.querySelector('#menu').hidden")
    m = c.menu()
    c.check("account menu: user, role, change password, sign out", m["head"].startswith("admin") and "Administrator" in m["head"]
            and m["items"] == ["Passwort ändern", "Abmelden"], m)
    c.shot("account_menu")
    c.tap('#menu [data-act="out"]')
    ok = c.wait("() => S.auth === 'login'")
    c.check("sign out: sign-in view with 'Du hast dich abgemeldet.'", ok and login_note(c) == "Du hast dich abgemeldet." and not c.ui()["app"], login_note(c))
    c.shot("login_after_logout")
    p.reload()
    c.wait("() => S.auth !== ''")
    c.check("after sign out a reload stays on the sign-in view (cookie removed)", c.ui()["auth"] == "login")
    # trusted device
    p.fill("#lUser", "admin")
    p.fill("#lPass", ADMIN[1])
    p.set_checked("#lTrust", True)
    c.tap("#loginBtn")
    c.wait_ready()
    devs = c.mock.state()["devices"]
    want = "iPhone" if c.phone else "Windows-PC"
    label = devs[0].get("label", "") if devs else ""
    c.check("'trust this device': one device stored with a label derived from the browser", len(devs) == 1 and label.startswith(want), devs)
    ck = [x for x in c.context.cookies() if x["name"] == "dd_session"]
    c.check("'trust this device': long-lived session cookie (about one year)", ck and ck[0]["expires"] - time.time() > 300 * 86400,
            ck[0]["expires"] - time.time() if ck else "no cookie")
    c.no_console_errors()
    # lock-out
    c.reset("two_users", login_locked=1)
    p = c.open()
    p.fill("#lUser", "admin")
    p.fill("#lPass", ADMIN[1])
    c.tap("#loginBtn")
    ok = c.wait("() => document.querySelector('#loginBtn').disabled && document.querySelector('#loginMsg').textContent !== ''")
    msg = c.text("#loginMsg")
    c.check("lock-out (LOCKED): countdown message and disabled button", ok and msg.startswith("Zu viele Fehlversuche. Bitte warte noch") and c.ui()["auth"] == "login", msg)
    c.shot("login_locked")
    c.overflow_check("sign-in locked")
    # session expiry while signed in
    c.start("two_users")
    c.mock.set(session_expire=1)
    c.ev("() => refresh()")
    ok = c.wait("() => S.auth === 'login'")
    c.check("expired session: back on the sign-in view with a reason", ok and login_note(c) == "Deine Sitzung ist abgelaufen. Bitte melde dich erneut an.", login_note(c))
    c.shot("login_expired")


@test
def t01b_forced_password(c: Ctx):
    c.reset("default_login")
    p = c.open()
    p.fill("#lUser", "admin")
    p.fill("#lPass", "admin")
    c.tap("#loginBtn")
    ok = c.wait("() => S.auth === 'pw'")
    ui = c.ui()
    c.check("admin/admin: forced password change instead of the app", ok and ui["pw"] and not ui["app"] and not ui["header"] and not ui["login"], ui["auth"])
    c.check("password change form: texts and user name field", c.text("#vPw h1") == "Neues Passwort festlegen"
            and c.ev("() => document.querySelector('#pwUser').value") == "admin" and c.ev("() => document.activeElement.id") == "pwOld")
    c.shot("forced_pw")
    c.overflow_check("forced password change")
    sets = [("", "", "", "Bitte das aktuelle Passwort eingeben."),
            ("admin", "abc", "abc", "Das neue Passwort muss 5 bis 64 Zeichen lang sein."),
            ("admin", "admin", "admin", "Bitte ein anderes Passwort als „admin“ wählen."),
            ("admin", "geheim123", "geheim124", "Die beiden neuen Passwörter stimmen nicht überein."),
            ("falsch", "geheim123", "geheim123", "Das aktuelle Passwort stimmt nicht.")]
    for old, a, b, msg in sets:
        p.fill("#pwOld", old)
        p.fill("#pwNew", a)
        p.fill("#pwNew2", b)
        c.tap("#pwBtn2")
        c.wait("() => document.querySelector('#pwMsg').textContent !== '' && !document.querySelector('#pwBtn2').disabled")
        c.check(f"password change: '{msg}'", c.text("#pwMsg") == msg and c.ui()["pw"], c.text("#pwMsg"))
    c.shot("forced_pw_error")
    c.overflow_check("forced password change with error")
    p.fill("#pwOld", "admin")
    p.fill("#pwNew", "geheim123")
    p.fill("#pwNew2", "geheim123")
    c.tap("#pwBtn2")
    ok = c.wait_ready()
    ui = c.ui()
    c.check("valid new password: app opens, toast confirms", ok and ui["app"] and c.wait_toast("Das Passwort wurde geändert."), c.toast_texts())
    c.check("mock: delivery password no longer active", c.mock.state()["users"][0]["default_password"] is False)
    c.check("new password works over the API", c.mock.api("/api/status", ("admin", "geheim123")).get("version") is not None)
    c.shot("after_forced_pw")
    c.no_console_errors()
    # leave the forced change by signing out
    c.reset("default_login")
    p = c.open()
    p.fill("#lUser", "admin")
    p.fill("#lPass", "admin")
    c.tap("#loginBtn")
    c.wait("() => S.auth === 'pw'")
    c.tap("#pwOut")
    ok = c.wait("() => S.auth === 'login'")
    c.check("forced change: 'Abmelden' returns to the sign-in view", ok and c.ui()["login"] and not c.ui()["pw"], c.ui()["auth"])
    # a session that becomes forced later (mock switches the password back to the delivery one)
    c.start("usb_active")
    c.mock.set(default_password=1)
    c.ev("() => refresh()")
    ok = c.wait("() => S.auth === 'pw'")
    c.check("MUST_CHANGE_PASSWORD during use: the app switches to the password change view", ok and c.ui()["pw"] and not c.ui()["app"], c.ui()["auth"])


# ================================================================== 2. files, folders, navigation

@test
def t02a_files_and_folders(c: Ctx):
    p = c.start("files")
    ui = c.ui()
    c.check("mode card: 'Dateiverwaltung' pressed, short status, other segment switches, storage 'microSD · used / total'",
            ui["title"] == T_APP and ui["thumb"] == "app" and ui["status"] == "Bereit für Uploads" and ui["btn"] == "USB-Gerät"
            and ui["stoTitle"] == "microSD" and re.fullmatch(r"[\d,]+ / [\d,]+ GB", ui["stoSub"]), f"{ui['title']} {ui['status']} / {ui['stoSub']}")
    c.check("mode card ⓘ holds the explanation (no description text in the card)", ui["sub"].startswith("Das USB-Gerät sieht das Laufwerk im Moment nicht.")
            and vis(c, "#modeInfo") and not c.ev("() => !!document.querySelector('#status p')"), ui["sub"][:60])
    c.check("root: 17 entries, folders first, count shown", len(ui["rows"]) == ROOT_COUNT and ui["dirs"] == ROOT_DIRS and ui["rows"][:3] == ROOT_DIRS
            and ui["count"] == str(ROOT_COUNT) and ui["h1"] == "Meine Dateien" and not ui["crumbsVisible"], f"{len(ui['rows'])} rows, {ui['dirs']}")
    c.check("hidden system folders are not listed", "System Volume Information" not in ui["rows"] and ".Trash-1000" not in ui["rows"])
    c.check("toolbar: one 'Hochladen'; 'Neuer Ordner' as a button only on the desktop; no floating button, no separate '+'",
            ui["upBtn"] and ui["mkBtn"] != c.phone and not c.ev("() => !!document.querySelector('#fab, #upDirBtn')")
            and c.ev("() => [...document.querySelectorAll('#toolbar button')].filter(b => b.getClientRects().length).length") == (1 if c.phone else 2),
            f"mk {ui['mkBtn']} up {ui['upBtn']}")
    c.tap("#upBtn")
    c.wait("() => !document.querySelector('#menu').hidden")
    m = c.menu()
    want = ["Dateien hochladen", "Neuer Ordner"] if c.phone else ["Dateien hochladen", "Ordner hochladen"]   # iPhone: no folder picker
    c.check("upload menu: files, folder upload where supported, new folder on the phone", m["items"] == want
            and c.ev("() => document.querySelector('#upBtn').getAttribute('aria-haspopup')") == "menu", m["items"])
    c.shot("upload_menu")
    c.page.keyboard.press("Escape")
    c.wait("() => document.querySelector('#menu').hidden")
    c.shot("files_root")
    c.overflow_check("file list root")
    nm = c.ev(JS_NAMES)
    c.check("every visible button, link and field has an accessible name", not nm["bad"], f"{nm['n']} controls {nm['bad'][:3]}")

    # enter a folder: click on the row
    c.tap(c.row("Projekte").locator(".name"))
    ok = c.wait_dir("Projekte")
    ui = c.ui()
    c.check("enter folder by tap: hash, title, breadcrumb, content", ok and ui["hash"] == "#/Projekte" and ui["h1"] == "Projekte" and ui["crumbsVisible"]
            and ui["crumbs"] == ["Meine Dateien", "Projekte"] and ui["rows"] == ["Drachen", "Halterungen"], f"{ui['hash']} {ui['crumbs']} {ui['rows']}")
    c.shot("folder_projekte")
    c.overflow_check("folder level 1")
    # deeper, via the row menu 'Öffnen'
    c.act("Drachen", "open")
    ok = c.wait_dir("Projekte/Drachen")
    c.tap(c.row("Teile").locator(".name"))
    ok = ok and c.wait_dir("Projekte/Drachen/Teile")
    ui = c.ui()
    c.check("three levels deep: title is the folder, hash holds the path", ok and ui["h1"] == "Teile" and ui["hash"] == "#/Projekte/Drachen/Teile", f"{ui['hash']} {ui['h1']}")
    if c.phone:
        c.check("breadcrumb on the phone: middle folders collapse behind '…'", ui["crumbs"][:2] == ["Meine Dateien", "…"] and "Projekte" not in ui["crumbs"]
                and ui["crumbs"][-2:] == ["Drachen", "Teile"], ui["crumbs"])
        c.tap('#crumbs .ell')
        c.wait("() => !document.querySelector('#menu').hidden")
        m = c.menu()
        c.check("'…' opens a list with the hidden folder", m["items"] == ["Projekte"], m["items"])
        c.shot("crumb_ellipsis_menu")
        c.tap('#menu [data-act="0"]')
        ok = c.wait_dir("Projekte")
        c.check("'…' menu: choosing a folder navigates there", ok and c.ui()["hash"] == "#/Projekte", c.ui()["hash"])
        c.page.go_back()
        c.wait_dir("Projekte/Drachen/Teile")
    else:
        c.check("breadcrumb: every parent is a link", ui["crumbs"] == ["Meine Dateien", "Projekte", "Drachen", "Teile"], ui["crumbs"])
    c.overflow_check("folder level 3")
    c.shot("folder_deep")
    # breadcrumb click
    c.tap('#crumbs a:has-text("Drachen")')
    ok = c.wait_dir("Projekte/Drachen")
    c.check("breadcrumb click: back to the parent", ok and c.ui()["crumbs"][-1] == "Drachen" and c.ui()["rows"] == ["Teile", "Drachenkopf.3mf"], c.ui()["rows"])
    # browser back / forward
    c.page.go_back()
    ok = c.wait_dir("Projekte/Drachen/Teile")
    c.check("browser back returns to the previous folder", ok and c.ui()["hash"] == "#/Projekte/Drachen/Teile", c.ui()["hash"])
    c.page.go_forward()
    ok = c.wait_dir("Projekte/Drachen")
    c.check("browser forward works", ok)
    c.tap('#crumbs a:has-text("Meine Dateien")')
    ok = c.wait_dir("")
    c.check("breadcrumb 'Meine Dateien' returns to the root", ok and not c.ui()["crumbsVisible"] and len(c.ui()["rows"]) == ROOT_COUNT)
    c.go("#/Projekte")
    c.wait_dir("Projekte")
    c.tap('a.brand')
    c.check("brand link goes to the root", c.wait_dir("") and c.ui()["hash"] in ("#/", ""))
    # folder with umlauts and spaces
    c.tap(c.row("Größen Prüfung äöü").locator(".name"))
    ok = c.wait_dir("Größen Prüfung äöü")
    c.check("folder with umlauts and spaces: path percent-encoded in the hash and decoded back",
            ok and c.ev("() => decodeURIComponent(location.hash)") == "#/Größen Prüfung äöü" and "%C3%B6" in c.ui()["hash"], c.ui()["hash"])
    c.overflow_check("folder with umlauts")
    # empty folder
    c.go("#/Leerer Ordner")
    c.wait_dir("Leerer Ordner")
    ui = c.ui()
    c.check("empty folder: friendly empty state, no list", ui["stateH"] == "Dieser Ordner ist leer" and not ui["rows"] and not ui["search"], ui["stateH"])
    c.shot("folder_empty")
    c.overflow_check("empty folder")
    # deep link and a folder that does not exist
    c.go("#/Projekte/Halterungen")
    ok = c.wait_dir("Projekte/Halterungen")
    c.check("deep link by changing the hash opens that folder", ok and c.ui()["h1"] == "Halterungen")
    c.clear_toasts()
    c.go("#/gibt/es/nicht")
    ok = c.wait_toast("gibt es nicht mehr")
    c.wait_dir("")
    c.check("missing folder: toast and back in the root", ok and c.ui()["dir"] == "" and c.ui()["hash"] == "#/", c.toast_texts())
    # reload inside a folder keeps the folder (cookie + hash)
    c.go("#/Projekte/Drachen")
    c.wait_dir("Projekte/Drachen")
    p.reload()
    c.wait_ready()
    ok = c.wait_dir("Projekte/Drachen")
    c.check("reload inside a folder: still there after the session check", ok and c.ui()["rows"] == ["Teile", "Drachenkopf.3mf"], c.ui()["hash"])
    # search and sort
    c.go("#/")
    c.wait_dir("")
    p.fill("#search", "3mf")
    c.wait("() => document.querySelectorAll('#list .row').length < 17")
    ui = c.ui()
    c.check("search filters the current folder only, count 'x von y'", all("3mf" in n.lower() for n in ui["rows"]) and ui["count"] == f"{len(ui['rows'])} von {ROOT_COUNT}", f"{ui['count']} {ui['rows'][:3]}")
    p.fill("#search", "zzzz")
    c.wait("() => !!document.querySelector('#list .state')")
    c.check("search without hit: 'Keine Treffer'", c.ui()["stateH"] == "Keine Treffer", c.ui()["stateH"])
    p.fill("#search", "")
    c.wait("() => document.querySelectorAll('#list .row').length === 17")
    if c.phone:
        p.select_option("#sortM", "size,-1")
    else:
        c.tap('.cols button[data-sort="size"]')           # first click on 'Größe' sorts largest first
    c.wait("() => S.sort.k === 'size'")
    rows = c.ui()["rows"]
    c.check("sorting: folders stay first, then by size", set(rows[:3]) == set(ROOT_DIRS) and rows[3] == "Video Zeitraffer.mp4", rows[:5])
    c.no_console_errors()


@test
def t02b_create_folder_and_upload(c: Ctx):
    p = c.start("files")
    c.go("#/Projekte")
    c.wait_dir("Projekte")
    c.new_folder()
    ok = c.wait_dlg("Neuer Ordner")
    d = c.dlg()
    c.check("new folder dialog: one name field with focus", ok and len(d["fields"]) == 1 and d["fields"][0]["label"] == "Name" and d["focus"] == "dF0" and d["yes"] == "Anlegen", d)
    c.shot("mkdir_dialog")
    c.overflow_check("new folder dialog")
    for val, msg in (("", "Bitte einen Namen eingeben."), ("a/b", "Der Name darf nicht mit Punkt oder Leerzeichen beginnen oder enden"),
                     (".versteckt", "Der Name darf nicht mit Punkt"), ("x" * 130, "Der Name ist zu lang."), ("Drachen", "Eine Datei oder ein Ordner mit diesem Namen existiert hier bereits.")):
        p.fill("#dF0", val)
        c.dlg_yes()
        ok = c.wait_derr()
        c.check(f"new folder, input {val[:12]!r}: error text in the dialog, stays open", ok and c.dlg()["err"].startswith(msg) and c.dlg()["open"], c.dlg()["err"][:70])
    c.shot("mkdir_dialog_error")
    p.fill("#dF0", "Neu äöü")
    c.dlg_yes()
    ok = c.wait_dlg_closed() and c.wait_rows(["Neu äöü"])
    c.check("folder created: appears in the list, toast, mock has it",
            ok and c.wait_toast("Ordner „Neu äöü“ angelegt.") and c.mock.ls("Projekte").get("Neu äöü", {}).get("type") == "dir", c.toast_texts())
    c.tap(c.row("Neu äöü").locator(".name"))
    ok = c.wait_dir("Projekte/Neu äöü")
    c.check("enter the new folder: empty state", ok and c.ui()["stateH"] == "Dieser Ordner ist leer", c.ui()["stateH"])
    c.check("empty folder: no second upload button in the empty state (the toolbar has 'Hochladen'), an ⓘ instead of a hint text",
            c.ui()["upBtn"] and not c.ev("() => !!document.querySelector('#list .state button.btn, #list .state p')")
            and c.ev("() => !!document.querySelector('#list .state .ib[data-i=i_empty]')"))
    c.shot("folder_new_empty")
    # upload into this folder
    c.page.set_input_files("#picker", [str(c.run.f["txt"]), str(c.run.f["odd"])])
    ok = c.wait("() => S.ups.length === 2 && !S.working && S.ups.every(u => u.state === 'done')", None, 20000)
    names = [c.run.f["txt"].name, c.run.f["odd"].name]
    c.check("upload into the open folder: both files saved there (not in the root)", ok and all(n in c.mock.ls("Projekte/Neu äöü") for n in names)
            and not any(n in c.mock.ls("") for n in names), list(c.mock.ls("Projekte/Neu äöü")))
    c.check("upload: panel says 'Gespeichert', toast, list shows both", c.wait_toast("2 Dateien gespeichert.") and c.text("#upHead") == "2 Dateien gespeichert" and c.wait_rows(names), c.text("#upHead"))
    c.shot("upload_in_folder")
    c.overflow_check("upload panel")
    if c.phone:
        c.settled("#uploads")
        panel_ok = c.ev("() => { const r = document.querySelector('#uploads').getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth && r.bottom <= innerHeight + 1; }")
        c.check("upload panel fits the phone screen", panel_ok)
    if c.ev("() => !document.querySelector('#uploads').hidden"):       # a finished panel without errors closes itself after about 6 s
        c.tap("#upClose")
    c.check("upload panel closes (close button, or by itself after the uploads are done)", c.wait("() => document.querySelector('#uploads').hidden", None, 9000))
    # empty file: error line instead of a toast
    c.page.set_input_files("#picker", [str(c.run.f["empty"])])
    c.wait("() => S.ups.length === 1 && !S.working")
    c.check("empty file: refused in the panel with a clear message", c.ev("() => S.ups[0].state") == "err" and "Die Datei ist leer." in c.text("#upList"), c.text("#upList"))
    c.tap("#upClose")
    # target folder vanished meanwhile
    c.mock.set(fail_next="NOT_FOUND@/api/upload/check")
    c.page.set_input_files("#picker", [str(c.run.f["txt"])])
    c.wait("() => S.ups.length === 1 && !S.working")
    c.check("target folder gone (NOT_FOUND): specific message", "Der Zielordner existiert nicht mehr" in c.text("#upList"), c.text("#upList"))
    c.no_console_errors()


@test
def t02c_rename_and_menu(c: Ctx):
    p = c.start("files")
    c.open_menu("Benchy.3mf")
    m = c.menu()
    c.check("file menu: download, rename, cut, copy, select, delete (+ title on the phone)",
            m["items"] == ["Herunterladen", "Umbenennen", "Ausschneiden", "Kopieren", "Auswählen", "Löschen"] and (m["head"] == "Benchy.3mf" or not c.phone), m)
    c.check("menu stays inside the viewport", m["rect"]["l"] >= 0 and m["rect"]["r"] <= m["vw"] + 1 and m["rect"]["b"] <= m["vh"] + 1 and m["rect"]["t"] >= 0, m["rect"])
    c.shot("row_menu")
    p.keyboard.press("Escape")
    c.check("Escape closes the menu", c.ev("() => document.querySelector('#menu').hidden"))
    c.open_menu("Projekte")
    c.check("folder menu: 'Öffnen', then 'Als ZIP herunterladen' (v5), then the usual entries",
            c.menu()["items"] == ["Öffnen", "Als ZIP herunterladen", "Umbenennen", "Ausschneiden", "Kopieren", "Auswählen", "Löschen"], c.menu()["items"])
    p.keyboard.press("Escape")
    # rename a file
    c.act("Benchy.3mf", "ren")
    ok = c.wait_dlg("Datei umbenennen")
    d = c.dlg()
    sel = c.ev("() => { const i = document.querySelector('#dF0'); return [i.selectionStart, i.selectionEnd]; }")
    c.check("rename dialog: name prefilled, only the part before the extension selected", ok and d["fields"][0]["value"] == "Benchy.3mf" and sel == [0, 6] and d["yes"] == "Umbenennen", f"{d['fields']} {sel}")
    c.shot("rename_dialog")
    p.fill("#dF0", "Würfel.3MF")
    c.dlg_yes()
    ok = c.wait_derr()
    c.check("rename onto an existing name (case-insensitive): error, stays open", ok and "existiert hier bereits" in c.dlg()["err"] and c.dlg()["open"], c.dlg()["err"])
    p.fill("#dF0", "a:b")
    c.dlg_yes()
    c.wait_derr()
    c.check("rename with a forbidden character: error", c.dlg()["err"].startswith("Der Name darf nicht"), c.dlg()["err"][:50])
    p.fill("#dF0", "Benchy neu ü.3mf")
    c.dlg_yes()
    ok = c.wait_dlg_closed() and c.wait_rows(["Benchy neu ü.3mf"]) and c.wait_gone("Benchy.3mf")
    names = c.mock.ls("")
    c.check("rename done: list, toast and mock agree", ok and "Benchy neu ü.3mf" in names and "Benchy.3mf" not in names and c.wait_toast("Umbenannt in „Benchy neu ü.3mf“."), c.toast_texts())
    # rename a folder
    c.act("Leerer Ordner", "ren")
    ok = c.wait_dlg("Ordner umbenennen")
    p.fill("#dF0", "Archiv")
    c.dlg_yes()
    ok = ok and c.wait_dlg_closed() and c.wait_rows(["Archiv"])
    c.check("rename a folder", ok and c.mock.ls("").get("Archiv", {}).get("type") == "dir" and "Leerer Ordner" not in c.mock.ls(), list(c.mock.ls())[:4])
    # Escape cancels
    c.act("Archiv", "ren")
    c.wait_dlg("Ordner umbenennen")
    p.keyboard.press("Escape")
    c.check("Escape cancels the rename dialog", c.wait_dlg_closed() and "Archiv" in c.mock.ls())
    # download through the menu
    with p.expect_download(timeout=8000) as dl:
        c.act("README", "dl")
    d = dl.value
    c.check("download from the menu: file name kept, request goes to /api/download?path=…", d.suggested_filename == "README"
            and "/api/download?path=README" in d.url, d.url)
    # download of a file inside a folder
    c.go("#/Projekte/Drachen")
    c.wait_dir("Projekte/Drachen")
    with p.expect_download(timeout=8000) as dl:
        c.act("Drachenkopf.3mf", "dl")
    c.check("download inside a folder: full path in the link", dl.value.suggested_filename == "Drachenkopf.3mf"
            and "path=Projekte%2FDrachen%2FDrachenkopf.3mf" in dl.value.url, dl.value.url)
    # right click / click open the menu on the desktop
    if not c.phone:
        c.row("Drachenkopf.3mf").click(button="right")
        c.check("right click opens the row menu (desktop)", c.wait("() => !document.querySelector('#menu').hidden"))
        p.keyboard.press("Escape")
        c.row("Drachenkopf.3mf").click()
        c.check("click on a file row opens its menu", c.wait("() => !document.querySelector('#menu').hidden"))
        p.keyboard.press("Escape")
    c.no_console_errors()


# ================================================================== 3. clipboard, selection, delete

@test
def t03a_clipboard(c: Ctx):
    p = c.start("files")
    # cut a file, paste into a folder
    c.act("Würfel.3mf", "cut")
    ok = c.wait("() => !document.querySelector('#clipbar').hidden")
    ui = c.ui()
    c.check("cut: clipboard bar 'ausgeschnitten' with 'Hier einfügen'", ok and ui["clipTxt"] == "1 Element ausgeschnitten" and c.text("#clipPaste") == "Hier einfügen", ui["clipTxt"])
    c.shot("clipboard_cut")
    c.overflow_check("clipboard bar")
    c.clear_toasts()
    c.tap("#clipPaste")
    c.check("paste where it was cut: 'liegen bereits in diesem Ordner'", c.wait_toast("liegen bereits in diesem Ordner"), c.toast_texts())
    c.tap(c.row("Leerer Ordner").locator(".name"))
    c.wait_dir("Leerer Ordner")
    c.check("clipboard survives navigation and is shown in the folder", c.ui()["clipbar"] and c.ui()["clipTxt"] == "1 Element ausgeschnitten")
    c.shot("clipboard_in_target")
    c.clear_toasts()
    c.tap("#clipPaste")
    ok = c.wait_toast("1 Element verschoben.") and c.wait_rows(["Würfel.3mf"])
    c.check("paste moves the file: list, toast, mock (gone from the root), clipboard emptied",
            ok and "Würfel.3mf" in c.mock.ls("Leerer Ordner") and "Würfel.3mf" not in c.mock.ls("") and not c.ui()["clipbar"], c.toast_texts())
    # copy two files, paste into another folder
    c.go("#/")
    c.wait_dir("")
    c.select_rows("KURZNAME.GCO", "README")
    c.shot("selection_two")
    c.overflow_check("selection bar")
    c.tap("#sCopy")
    ok = c.wait("() => !document.querySelector('#clipbar').hidden")
    c.check("copy of a selection: 'Elemente kopiert', selection mode ends", ok and c.ui()["clipTxt"] == "2 Elemente kopiert" and not c.ui()["selbar"], c.ui()["clipTxt"])
    c.go("#/Projekte/Halterungen")
    c.wait_dir("Projekte/Halterungen")
    c.clear_toasts()
    c.tap("#clipPaste")
    ok = c.wait_toast("2 Elemente kopiert.") and c.wait_rows(["KURZNAME.GCO", "README"])
    c.check("paste of a copy: both files in the target, still in the source", ok and {"KURZNAME.GCO", "README"} <= set(c.mock.ls("Projekte/Halterungen"))
            and {"KURZNAME.GCO", "README"} <= set(c.mock.ls("")), c.toast_texts())
    c.check("copy stays on the clipboard for another paste", c.ui()["clipbar"] and c.ui()["clipTxt"] == "2 Elemente kopiert")
    # name clash: paste the same copy again -> dialog; new name
    c.clear_toasts()
    c.tap("#clipPaste")
    ok = c.wait_dlg("gibt es hier schon")
    d = c.dlg()
    c.check("name clash: dialog offers 'Mit neuem Namen' / 'Überspringen' and 'Für alle weiteren übernehmen'",
            ok and d["yes"] == "Mit neuem Namen" and d["no"] == "Überspringen" and d["fields"] and d["fields"][0]["type"] == "checkbox", f"{d['title']} {d['yes']} {d['no']}")
    c.shot("paste_name_clash")
    c.overflow_check("name clash dialog")
    c.dlg_yes()
    ok = c.wait("t => document.querySelector('#dlg').open && document.querySelector('#dTitle').textContent.includes(t) && !document.querySelector('#dTitle').textContent.includes('KURZNAME')", "gibt es hier schon")
    c.check("second clash asked again (no 'for all' ticked)", ok, c.dlg()["title"])
    c.dlg_no()
    ok = c.wait_toast("1 Element kopiert. 1 übersprungen.")
    ls = c.mock.ls("Projekte/Halterungen")
    c.check("clash: renamed copy 'KURZNAME (2).GCO' created, the other skipped", ok and "KURZNAME (2).GCO" in ls and "README (2)" not in ls, c.toast_texts())
    # for all: skip
    c.clear_toasts()
    c.tap("#clipPaste")
    c.wait_dlg("gibt es hier schon")
    c.fill_dlg(True)
    c.dlg_no()
    ok = c.wait_toast("2 übersprungen.")
    c.check("'for all' + skip: no second question, both skipped", ok and not c.ev("() => document.querySelector('#dlg').open"), c.toast_texts())
    # cut a folder and paste it into itself
    c.go("#/")
    c.wait_dir("")
    c.act("Projekte", "cut")
    c.go("#/Projekte/Drachen")
    c.wait_dir("Projekte/Drachen")
    c.clear_toasts()
    c.tap("#clipPaste")
    ok = c.wait_toast("kann nicht in sich selbst eingefügt werden")
    c.check("folder into itself: refused with a clear message, clipboard kept", ok and c.ui()["clipbar"] and "Projekte" in c.mock.ls(""), c.toast_texts())
    c.tap("#clipX")
    c.check("clipboard 'Abbrechen' empties it", not c.ui()["clipbar"])
    # a failing paste: NO_SPACE
    c.go("#/")
    c.wait_dir("")
    c.act("LEER.TXT", "copy")
    c.go("#/Leerer Ordner")
    c.wait_dir("Leerer Ordner")
    c.mock.set(fail_next="NO_SPACE@/api/copy")
    c.clear_toasts()
    c.tap("#clipPaste")
    ok = c.wait_toast("Nicht genug freier Speicher")
    c.check("paste fails with NO_SPACE: error toast, clipboard kept", ok and c.ui()["clipbar"], c.toast_texts())
    c.no_console_errors()


@test
def t03b_busy_overlay(c: Ctx):
    p = c.start("files")
    c.act("KURZNAME.GCO", "copy")
    c.go("#/Leerer Ordner")
    c.wait_dir("Leerer Ordner")
    c.mock.set(delay_ms=1500)
    c.tap("#clipPaste")
    ok = c.wait("() => !document.querySelector('#busy').hidden", None, 4000)
    ui = c.ui()
    c.check("copy: blocking overlay with the file name while the device is busy", ok and "KURZNAME.GCO" in ui["busyT"] and "wird kopiert" in ui["busyT"], ui["busyT"])
    c.shot("busy_copy")
    c.overflow_check("busy overlay")
    inert = c.ev("() => document.querySelector('#app').inert && document.querySelector('.top').inert")
    c.check("overlay makes the page behind it inert", inert)
    ok = c.wait("() => document.querySelector('#busy').hidden", None, 12000)
    c.mock.set(delay_ms=0)
    c.check("overlay disappears, the copy arrived", ok and c.wait_toast("1 Element kopiert.") and "KURZNAME.GCO" in c.mock.ls("Leerer Ordner"), c.toast_texts())
    # delete several items: overlay with counter
    c.go("#/")
    c.wait_dir("")
    c.select_rows("LEER.TXT", "README")
    c.tap("#sDel")
    ok = c.wait_dlg("2 Elemente löschen?")
    c.mock.set(delay_ms=900)
    c.dlg_yes()
    ok2 = c.wait("() => !document.querySelector('#busy').hidden && /Wird gelöscht/.test(document.querySelector('#busyT').textContent)", None, 4000)
    c.shot("busy_delete")
    c.check("multi delete: overlay 'Wird gelöscht … (i von n)'", ok and ok2, c.ui()["busyT"])
    c.wait("() => document.querySelector('#busy').hidden", None, 12000)
    c.mock.set(delay_ms=0)
    c.check("multi delete finished", c.wait_toast("2 Elemente gelöscht."))
    # another client uploads: notice, changes answer BUSY
    c.mock.set(other_upload=1)
    ok = c.wait("() => document.querySelector('#notice').textContent.includes('Ein anderes Gerät lädt gerade eine Datei hoch')", None, 8000)
    c.check("other_upload: notice at the top", ok, c.ui()["notice"])
    c.shot("notice_other_upload")
    c.overflow_check("notice other upload")
    c.new_folder()
    c.wait_dlg("Neuer Ordner")
    p.fill("#dF0", "Gesperrt")
    c.dlg_yes()
    ok = c.wait_derr()
    c.check("new folder while another client uploads: BUSY text in the dialog", ok and "Dateiübertragung" in c.dlg()["err"], c.dlg()["err"])
    c.dlg_no()
    c.mock.set(other_upload=0)
    ok = c.wait("() => document.querySelector('#notice').hidden", None, 8000)
    c.check("notice goes away when the other upload ends", ok)
    c.no_console_errors()


@test
def t03c_select_and_delete(c: Ctx):
    p = c.start("files")
    # selection through the menu, bar, select all, clear
    c.select_rows("README", "LEER.TXT")
    ui = c.ui()
    c.check("selection bar replaces the toolbar and counts", ui["selbar"] and not ui["toolbar"] and ui["selCount"] == "2 ausgewählt", ui["selCount"])
    dl = c.ev("() => document.querySelector('#sDl').getClientRects().length > 0")
    c.check("two files selected: download button visible (v5: any selection downloads, one item after the other)", dl)
    if c.phone:
        c.tap("#sAll")
        c.wait("() => S.sel.size === 17")
        c.check("'Alle auswählen' selects every entry of the folder", c.ui()["selCount"] == "17 ausgewählt" and c.ev("() => document.querySelector('#sAll').disabled"), c.ui()["selCount"])
    else:
        c.check("desktop: no 'Alle auswählen' in the bar (the header checkbox does it)", not vis(c, "#sAll") and vis(c, "#ckAll"))
        c.tap("#ckAll")
        c.wait("() => S.sel.size === 17")
        c.check("header checkbox selects every entry of the folder", c.ui()["selCount"] == "17 ausgewählt", c.ui()["selCount"])
    c.shot("selection_all")
    c.overflow_check("selection all")
    c.tap("#sX")
    c.check("clear selection returns to the toolbar", c.wait("() => document.querySelector('#selbar').hidden") and c.ui()["toolbar"])
    if not c.phone:
        c.row("README").locator(".ck").click()
        c.row("notizen.txt").locator(".ck").click()
        c.check("checkbox clicks select rows", c.wait("() => S.sel.size === 2"))
        c.tap("#ckAll")
        c.check("header checkbox selects all visible", c.wait("() => S.sel.size === 17"))
        c.tap("#ckAll")
        c.check("header checkbox again clears", c.wait("() => S.sel.size === 0") and c.ui()["toolbar"])
        c.row("README").click(modifiers=["Control"])
        c.check("Ctrl+click toggles a row", c.wait("() => S.sel.has('README')"))
        p.keyboard.press("Escape")
        c.check("Escape leaves the selection", c.wait("() => S.sel.size === 0"))
    else:
        c.row("README").scroll_into_view_if_needed()
        box = c.row("README").bounding_box()
        c.page.touchscreen.tap(box["x"] + 60, box["y"] + box["height"] / 2)
        c.page.wait_for_timeout(300)
        c.check("phone: a tap on a row opens its sheet (no selection)", not c.ev("() => document.querySelector('#menu').hidden") and c.ev("() => S.sel.size") == 0)
        c.ev("() => document.querySelector('#scrim').click()")
    # single download button in the selection bar
    c.select_rows("README")
    c.check("one file selected: download button visible", c.ev("() => !document.querySelector('#sDl').hidden"))
    with p.expect_download(timeout=8000) as dl:
        c.tap("#sDl")
    c.check("selection bar download triggers the file download", dl.value.suggested_filename == "README", dl.value.suggested_filename)
    c.tap("#sX")
    # multi delete: cancel, then confirm
    c.select_rows("notizen.txt", "KURZNAME.GCO", "Projekte")
    c.tap("#sDel")
    ok = c.wait_dlg("3 Elemente löschen?")
    d = c.dlg()
    c.check("delete dialog for 3 items warns about the folder content", ok and "Darunter ist 1 Ordner" in d["body"] and d["yes"] == "Löschen" and "danger" in d["yesCls"], d["body"])
    c.shot("delete_many_dialog")
    c.overflow_check("delete dialog")
    c.dlg_no()
    c.wait_dlg_closed()
    c.check("cancel deletes nothing, selection kept", {"notizen.txt", "KURZNAME.GCO", "Projekte"} <= set(c.mock.ls("")) and c.ui()["selbar"], f"selbar {c.ui()['selbar']} sel {c.ev('() => S.sel.size')}")
    c.tap("#sDel")
    c.wait_dlg("3 Elemente löschen?")
    c.dlg_yes()
    ok = c.wait_gone("Projekte") and c.wait_gone("notizen.txt")
    ls = c.mock.ls("")
    c.check("confirmed: items (incl. the non-empty folder) gone in the list and in the mock, toast, selection mode over",
            ok and not ({"notizen.txt", "KURZNAME.GCO", "Projekte"} & set(ls)) and c.wait_toast("3 Elemente gelöscht.") and not c.ui()["selbar"], c.toast_texts())
    c.check("count updated", c.ui()["count"] == str(ROOT_COUNT - 3) and len(c.ui()["rows"]) == ROOT_COUNT - 3, c.ui()["count"])
    # single delete
    c.act("Foto Druckbett.jpg", "del")
    ok = c.wait_dlg("Datei löschen?")
    c.check("single file: confirmation names the file", ok and "„Foto Druckbett.jpg“" in c.dlg()["body"], c.dlg()["body"])
    c.shot("delete_dialog")
    c.dlg_yes()
    c.check("single delete done", c.wait_gone("Foto Druckbett.jpg") and "Foto Druckbett.jpg" not in c.mock.ls() and c.wait_toast("Gelöscht."), c.toast_texts())
    # delete a folder via its menu: folder wording; failing delete shows the error in the dialog
    c.act("Größen Prüfung äöü", "del")
    c.wait_dlg("mit Inhalt löschen?")
    c.check("folder delete: title with name and 'mit Inhalt'", "„Größen Prüfung äöü“" in c.dlg()["title"], c.dlg()["title"])
    c.mock.set(fail_next="IO@/api/delete")
    c.dlg_yes()
    ok = c.wait_derr()
    c.check("delete fails (IO): error shown in the dialog, dialog stays", ok and "Lese- oder Schreibfehler" in c.dlg()["err"] and c.dlg()["open"], c.dlg()["err"])
    c.dlg_no()
    c.no_console_errors()


# ================================================================== 4. owner switch

@test
def t04a_owner_switch(c: Ctx):
    c.start("usb_active")
    ui = c.ui()
    c.check("usb_active: 'USB-Gerät' pressed, status 'USB-Gerät verbunden' (details in ⓘ), state panel without list or upload buttons",
            ui["title"] == T_USB and ui["thumb"] == "usb" and ui["status"] == "USB-Gerät verbunden" and ui["sub"] == "Das angeschlossene USB-Gerät hat Zugriff auf die Dateien."
            and ui["btn"] == "Dateiverwaltung" and ui["stateH"] == "Die Dateien gehören gerade dem USB-Gerät" and not ui["rows"] and not ui["mkBtn"] and not ui["upBtn"],
            f"{ui['title']} {ui['status']} / {ui['stateH']}")
    c.shot("usb_locked")
    c.overflow_check("locked state")
    c.switch()
    ok = c.wait_dlg(T_TAKEOVER)
    d = c.dlg()
    c.check("activate while the host uses the drive: take-over dialog (danger button)", ok and d["yes"] == "Trennen und fortfahren" and d["no"] == "Abbrechen" and "danger" in d["yesCls"], d["title"])
    c.shot("takeover_dialog")
    c.overflow_check("take-over dialog")
    c.dlg_no()
    c.wait_dlg_closed()
    c.check("cancel keeps USB mode", c.mock.state()["owner"] == "usb" and c.ui()["title"] == T_USB and c.wait("() => document.querySelector('.seg2').dataset.on === 'usb'"))
    c.switch()
    c.wait_dlg(T_TAKEOVER)
    c.page.keyboard.press("Escape")
    c.check("Escape cancels the dialog", c.wait_dlg_closed() and c.mock.state()["owner"] == "usb")
    c.switch()
    c.wait_dlg(T_TAKEOVER)
    c.dlg_yes()
    ok = c.wait("() => document.querySelector('#segApp').getAttribute('aria-pressed') === 'true' && document.querySelectorAll('#list .row').length > 0")
    c.check("confirm: file mode, list appears, thumb on 'Dateiverwaltung'", ok and c.mock.state()["owner"] == "app" and c.ui()["thumb"] == "app", c.ui()["title"])
    c.shot("after_activate")
    # back to the USB device
    c.clear_toasts()
    c.switch()
    ok = c.wait_toast("Das USB-Laufwerk ist bereitgestellt.")
    c.check("hand over to the USB device: toast, state panel, owner usb", ok and c.wait("() => document.querySelector('#segUsb').getAttribute('aria-pressed') === 'true'")
            and c.mock.state()["owner"] == "usb" and not c.ui()["rows"], c.toast_texts())
    # slow device: the thumb moves on press, the target segment shows a spinner and stays tappable
    c.mock.set(delay_ms=1200)
    c.switch()
    ok = c.wait("() => !!document.querySelector('#segApp .spin') && document.querySelector('.seg2').dataset.on === 'app' && !document.querySelector('#segApp').disabled", None, 4000)
    c.shot("switching_busy")
    c.check("while switching: thumb already on the target, spinner in it, segments not blocked", ok, c.ui())
    c.wait_dlg(T_TAKEOVER, timeout=8000)
    c.mock.set(delay_ms=0)
    c.dlg_no()
    # a second tap during a running switch is kept and done afterwards (no host: no take-over question)
    c.start("usb_no_host", delay_ms=900)
    c.switch()
    c.wait("() => S.switching === 'app'", None, 3000)
    c.tap("#segUsb")
    ok = c.wait("() => document.querySelector('.seg2').dataset.on === 'usb'", None, 2000)
    done = c.wait("() => !S.switching && S.st.drive.owner === 'usb' && !S.segQ", None, 15000)
    modes = [q.get("target") for q in reqs(c, "POST", "/api/mode")]
    c.check("second tap while switching: registered (thumb back at once), carried out after the first switch", ok and done and modes[:2] == ["app", "usb"]
            and c.mock.state()["owner"] == "usb", modes)
    c.mock.set(delay_ms=0)
    # no host connected / ejected: no dialog
    for label, scen, sets in (("host ejected", "usb_active", dict(host_ejected=1, owner="usb")), ("no USB device", "usb_no_host", {})):
        c.start(scen, **sets)
        c.switch()
        ok = c.wait("() => document.querySelector('#segApp').getAttribute('aria-pressed') === 'true'")
        c.check(f"activate with {label}: no dialog, file mode", ok and not c.ev("() => window.__dialogs.length") and c.mock.state()["owner"] == "app")
    # host writing
    c.start("usb_active", host_writing="hold")
    c.wait("() => (document.querySelector('#modeInfo').dataset.x || '').includes('schreibt')")
    c.check("host writing: status line and ⓘ say so", c.ui()["status"] == "USB-Gerät schreibt …" and c.ui()["sub"] == "Das USB-Gerät schreibt gerade auf das Laufwerk.", c.ui()["sub"])
    c.switch()
    ok = c.wait_toast("schreibt gerade")
    t = c.toasts()
    c.check("host writing: error toast, no dialog, still USB", ok and t[-1]["err"] and not c.ev("() => window.__dialogs.length") and c.mock.state()["owner"] == "usb", t[-1]["text"] if t else "no toast")
    c.shot("host_writing")
    c.mock.set(host_writing=0)
    # upload while the USB device owns the drive: take-over, auto re-provide
    c.start("usb_active")
    with c.page.expect_file_chooser() as fc:
        c.tap("#lockedUp")
    fc.value.set_files(str(c.run.f["txt"]))
    ok = c.wait_dlg(T_TAKEOVER)
    c.dlg_yes()
    ok = ok and c.wait_toast("gespeichert", 20000)
    c.page.wait_for_timeout(500)
    st = c.mock.state()
    c.check("upload in USB mode: take-over, saved, drive handed back automatically (toast says so)",
            ok and st["owner"] == "usb" and any("wieder bereitgestellt" in x for x in c.toast_texts()) and c.run.f["txt"].name in [f["path"] for f in st["files"]], c.toast_texts())
    c.shot("upload_usb_reprovided")
    c.no_console_errors()
    # special media / states
    for scen, title in (("no_fs", ""), ("empty", T_APP), ("flash_medium", T_APP)):
        c.start(scen)
        ui = c.ui()
        extra = (ui["stateH"] == "Der Speicher muss eingerichtet werden" and not ui["seg"] and ui["btn"] is None) if scen == "no_fs" \
            else ui["stateH"] == "Noch keine Dateien" if scen == "empty" else ui["stoTitle"] == "Intern"
        c.check(f"scenario {scen}: status and list state", (ui["title"] == title or not title) and extra, f"{ui['title']} / {ui['stateH']} / {ui['stoTitle']}")
        c.shot(f"scenario_{scen}")
        c.overflow_check(f"scenario {scen}")


# ================================================================== 5. settings

@test
def t05a_settings_general(c: Ctx):
    p = c.start("two_users", trust=True, wifi_count=3, scan_ms=200)
    c.tap("#navSettings")
    c.wait("() => !document.querySelector('#vSettings').hidden")
    ok = c.wait("() => document.querySelector('#wifiList .li') !== null")
    ui = c.ui()
    c.check("settings: view switches, hash, header link hidden", ok and ui["settings"] and not ui["files"] and ui["hash"] == "#einstellungen" and c.ev("() => document.querySelector('#navSettings').hidden"), ui["hash"])
    tabs = c.ev(JS_TABS)
    c.check("admin sees all settings cards", tabs == ["WLAN", "Speicher & USB", "Sicherheit", "Benutzer", "Darstellung & Sprache", "System"], tabs)
    cards = c.ev("""() => [...document.querySelectorAll('.sec')].filter(s => s.getClientRects().length).map(s => ({id: s.id,
      exp: s.querySelector('.sechead').getAttribute('aria-expanded'), ctl: s.querySelector('.sechead').getAttribute('aria-controls'),
      body: s.querySelector('.secbody').hidden, sum: s.querySelector('.sum').textContent}))""")
    c.check("cards start closed, headers expose aria-expanded/aria-controls, every card has a one-line summary",
            all(x["exp"] == "false" and x["body"] and x["ctl"] == "b-" + x["id"][2:] and x["sum"] for x in cards), cards)
    c.check("summaries show the state", c.text("#sumGen") == "Automatisch · Deutsch" and "belegt" in c.text("#sumUsb")
            and c.text("#sumNet").endswith("· Verbunden") and c.text("#sumSys").startswith("In Betrieb seit"), [x["sum"] for x in cards])
    c.check("no account block in the settings (password and sign-out live in the user menu only)",
            not c.ev("() => !!document.querySelector('#acctX, #pwBtn, #outBtn')"))
    c.check("'?' at the top right opens the help page", c.ev("() => document.querySelector('#helpBtn').getAttribute('href')") == "#!hilfe" and vis(c, "#helpBtn"))
    c.shot("settings_top")
    c.shot("settings_full", full=True)
    c.overflow_check("settings")
    nm = c.ev(JS_NAMES)
    c.check("settings: every visible control has an accessible name", not nm["bad"], f"{nm['n']} controls {nm['bad'][:3]}")
    c.ev("() => scrollTo(0, 0)")
    # one card open at a time
    open_card(c, "usb")
    open_card(c, "gen")
    c.check("opening a card closes the other one; the open card hides its summary",
            c.ev("() => document.querySelector('#b-usb').hidden && !document.querySelector('#b-gen').hidden && !document.querySelector('#sumGen').getClientRects().length"))
    c.shot("settings_card_open")
    # theme
    c.tap('#theme button[data-v="dark"]')
    th = c.ev("() => [document.documentElement.dataset.theme, getComputedStyle(document.body).backgroundColor, localStorage.getItem('dd-theme')]")
    c.check("theme 'Dunkel': data-theme, dark background, stored", th[0] == "dark" and th[1] == "rgb(18, 20, 22)" and th[2] == "dark", th)
    c.shot("settings_theme_dark_forced")
    c.tap('#theme button[data-v="light"]')
    th = c.ev("() => [document.documentElement.dataset.theme, getComputedStyle(document.body).backgroundColor]")
    c.check("theme 'Hell' gives the light background", th[0] == "light" and th[1] == "rgb(247, 247, 245)", th)
    c.tap('#theme button[data-v=""]')
    c.check("theme 'Automatisch' removes the override", c.ev("() => document.documentElement.dataset.theme") is None and c.ev("() => localStorage.getItem('dd-theme')") is None)
    pressed = c.ev("() => [...document.querySelectorAll('#theme button')].map(b => b.getAttribute('aria-pressed'))")
    c.check("theme buttons expose their state", pressed == ["true", "false", "false"], pressed)
    # USB / storage section
    open_card(c, "usb")
    c.check("storage section filled", c.text("#suMedium").startswith("microSD-Karte") and "von" in c.text("#suUsed") and c.text("#suFree") != "–", c.text("#suMedium"))
    c.tap("label.switch:has(#swWrite)")
    ok = c.wait_toast("darf nur noch lesen")
    c.check("'USB-Gerät darf Dateien ändern' off: API called, toast, mock follows", ok and c.mock.state()["host_writable"] is False, c.toast_texts())
    c.tap("label.switch:has(#swAuto)")
    c.check("'Nach dem Hochladen wieder bereitstellen' is stored per browser", c.ev("() => localStorage.getItem('dd-auto')") == "0" and not c.ev("() => document.querySelector('#swAuto').checked"))
    c.tap("#fmtBtn")
    c.wait_dlg("Speicher formatieren?")
    c.fill_dlg("falsch")
    c.dlg_yes()
    ok = c.wait_derr()
    c.check("format: needs the exact word LÖSCHEN", ok and c.dlg()["err"] == "Bitte genau das Wort LÖSCHEN eingeben.", c.dlg()["err"])
    c.shot("format_dialog")
    c.dlg_no()
    c.wait_dlg_closed()
    # system section
    open_card(c, "sys")
    c.check("system section: version, slot, uptime, memory", c.text("#sysVer") == c.mock.state()["version"] and c.text("#sysSlot").startswith("ota_") and c.text("#sysUp") != "–" and "kB" in c.text("#sysHeap"),
            f"{c.text('#sysVer')} {c.text('#sysSlot')} {c.text('#sysUp')} {c.text('#sysHeap')}")
    c.tap("#logBtn")
    ok = c.wait("() => !document.querySelector('#log').hidden")
    c.check("log: shown, button becomes 'Aktualisieren'", ok and c.text("#logBtn") == "Aktualisieren" and len(c.text("#log")) > 10, c.text("#log")[:60])
    c.shot("settings_log")
    c.overflow_check("settings with log")
    c.tap("#rebootBtn")
    ok = c.wait_dlg("Laufwerk neu starten?")
    c.check("restart: confirmation dialog", ok and c.dlg()["yes"] == "Neu starten", c.dlg()["title"])
    c.dlg_no()
    c.wait_dlg_closed()
    c.check("system card has the maintenance mode button", c.text("#flashBtn") == "Starten …")
    # Wi-Fi section
    open_card(c, "net")
    c.ev("() => document.querySelector('#s-net').scrollIntoView()")
    n0 = c.ev("() => document.querySelectorAll('#wifiList .li').length")
    active = c.text("#netTitle")
    c.check("saved networks: 3 listed, 'Verbunden' only once (status row, not again in the list), count text", n0 == 3
            and not c.ev("() => document.querySelectorAll('#wifiList .pill').length") and c.text("#netPill") == "Verbunden"
            and c.text("#wifiCount") == "3 von 5 gespeichert" and c.ev("a => !!document.querySelector(`[data-wdel=\"${a}\"]`)", active), c.text("#wifiCount"))
    c.tap(f'[data-wdel="{active}"]')
    c.wait_dlg("entfernen?")
    c.check("removing the connected network warns about it", "gerade mit diesem WLAN verbunden" in c.dlg()["body"], c.dlg()["body"][:60])
    c.dlg_no()
    c.shot("settings_wifi")
    c.tap("#wifiAdd")
    ok = c.wait_dlg("WLAN hinzufügen")
    c.check("add network dialog: name and password fields", ok and [f["type"] for f in c.dlg()["fields"]] == ["text", "password"], c.dlg()["fields"])
    c.shot("wifi_add_dialog")
    c.overflow_check("wifi add dialog")
    for ssid, pw, msg in (("", "", "Bitte den WLAN-Namen eingeben."), ("Testnetz", "kurz", "Das Passwort muss 8 bis 63 Zeichen lang sein"), ("ü" * 20, "", "Der WLAN-Name darf höchstens 32")):
        c.fill_dlg(ssid, pw)
        c.dlg_yes()
        c.wait_derr()
        c.check(f"wifi add, input {ssid[:8]!r}/{pw!r}: validation text", c.dlg()["err"].startswith(msg) and c.dlg()["open"], c.dlg()["err"][:60])
    c.fill_dlg("Testnetz ü", "geheim1234")
    c.dlg_yes()
    ok = c.wait_dlg_closed() and c.wait("() => document.querySelectorAll('#wifiList .li').length === 4")
    c.check("network added: listed, toast, mock stores it", ok and "Testnetz ü" in c.text("#wifiList") and c.wait_toast("„Testnetz ü“ gespeichert.")
            and any(n["ssid"] == "Testnetz ü" for n in c.mock.api("/api/wifi/list")["networks"]) and c.text("#wifiCount") == "4 von 5 gespeichert", c.toast_texts())
    c.tap('[data-wdel="Testnetz ü"]')
    ok = c.wait_dlg("WLAN „Testnetz ü“ entfernen?")
    c.dlg_yes()
    ok = ok and c.wait_dlg_closed() and c.wait("() => document.querySelectorAll('#wifiList .li').length === 3")
    c.check("network removed: list, toast, mock", ok and "Testnetz ü" not in c.text("#wifiList") and c.wait_toast("„Testnetz ü“ entfernt.")
            and not any(n["ssid"] == "Testnetz ü" for n in c.mock.api("/api/wifi/list")["networks"]), c.toast_texts())
    c.mock.set(wifi_count=5)
    c.go("#/")
    c.go("#einstellungen")
    c.wait("() => document.querySelectorAll('#wifiList .li').length === 5")
    open_card(c, "net")
    c.tap("#wifiAdd")
    c.wait_dlg("WLAN hinzufügen")
    c.fill_dlg("Sechstes", "geheim1234")
    c.dlg_yes()
    c.wait_derr()
    c.check("6th network: WIFI_LIMIT text", "5 WLANs gespeichert" in c.dlg()["err"], c.dlg()["err"])
    c.dlg_no()
    c.mock.set(wifi_count=0)
    c.go("#/")
    c.go("#einstellungen")
    c.wait("() => document.querySelector('#wifiList').textContent.includes('Noch kein WLAN')")
    c.check("no networks: empty hint", "Noch kein WLAN gespeichert." in c.text("#wifiList"))
    c.no_console_errors()


@test
def t05b_users_and_devices(c: Ctx):
    p = c.start("two_users", trust=True, wifi_count=1)
    c.go("#einstellungen")
    c.wait("() => document.querySelectorAll('#userList .li').length === 2")
    open_card(c, "users")
    c.ev("() => document.querySelector('#s-users').scrollIntoView()")
    rows = c.ev("() => [...document.querySelectorAll('#userList .li')].map(l => l.querySelector('.txt').textContent)")
    c.check("users listed with role, own account marked 'du'", len(rows) == 2 and any("admin" in r and "du" in r and "Administrator" in r for r in rows) and any("gast" in r and "Benutzer" in r for r in rows)
            and c.text("#userCount") == "2 von 8 Benutzern", rows)
    c.shot("settings_users")
    c.overflow_check("users section")
    c.tap("#userAdd")
    ok = c.wait_dlg("Benutzer hinzufügen")
    d = c.dlg()
    c.check("add user dialog: name, password, role (default 'Benutzer')", ok and [f["type"] for f in d["fields"]] == ["text", "password", "select-one"] and d["fields"][2]["value"] == "user", f"{ok} {d['fields']}")
    c.shot("user_add_dialog")
    c.overflow_check("user add dialog")
    for name, pw, msg in (("a b", "secret1", "Ungültiger Benutzername"), ("tester", "abc", "Das Passwort muss 5 bis 64 Zeichen lang sein."), ("GAST", "secret1", "Diesen Benutzernamen gibt es schon")):
        c.fill_dlg(name, pw, "user")
        c.dlg_yes()
        c.wait_derr()
        c.check(f"add user {name!r}: error text", c.dlg()["err"].startswith(msg) and c.dlg()["open"], c.dlg()["err"][:60])
    c.fill_dlg("tester", "secret1", "user")
    c.dlg_yes()
    ok = c.wait_dlg_closed() and c.wait("() => document.querySelectorAll('#userList .li').length === 3")
    c.check("user added: list, toast, mock, usable password", ok and c.wait_toast("„tester“ wurde angelegt.") and c.mock.api("/api/status", ("tester", "secret1")).get("version")
            and c.text("#userCount") == "3 von 8 Benutzern", c.toast_texts())
    # role change
    c.tap('[data-umore="tester"]')
    c.wait("() => !document.querySelector('#menu').hidden")
    m = c.menu()
    c.check("user menu: make admin, set password, delete", m["items"] == ["Zum Administrator machen", "Neues Passwort festlegen", "Löschen"] and m["head"] == "tester", m)
    c.shot("user_menu")
    c.tap('#menu [data-act="role"]')
    ok = c.wait_dlg("„tester“ zum Administrator machen?")
    c.dlg_yes()
    tester_role = "() => [...document.querySelectorAll('#userList .li')].find(l => l.textContent.includes('tester')).querySelector('small').textContent"
    ok = ok and c.wait_dlg_closed() and c.wait(f"() => ({tester_role[6:]}) === 'Administrator'")
    c.check("role changed to administrator (list and mock)", ok and any(u["name"] == "tester" and u["role"] == "admin" for u in c.mock.state()["users"]) and c.wait_toast("Die Rolle von „tester“ wurde geändert."), c.toast_texts())
    c.tap('[data-umore="tester"]')
    c.wait("() => !document.querySelector('#menu').hidden")
    c.check("admin user: menu offers 'Administratorrechte entziehen'", c.menu()["items"][0] == "Administratorrechte entziehen")
    c.tap('#menu [data-act="role"]')
    c.wait_dlg("die Administratorrechte entziehen?")
    c.dlg_yes()
    c.wait_dlg_closed()
    c.check("role back to user", c.wait(f"() => ({tester_role[6:]}) === 'Benutzer'"))
    # password of another user
    c.tap('[data-umore="tester"]')
    c.wait("() => !document.querySelector('#menu').hidden")
    c.tap('#menu [data-act="pw"]')
    c.wait_dlg("Neues Passwort für „tester“")
    c.fill_dlg("neu", "neu")
    c.dlg_yes()
    c.wait_derr()
    c.check("password of another user: length check", "5 bis 64" in c.dlg()["err"], c.dlg()["err"])
    c.fill_dlg("neupass1", "anders")
    c.dlg_yes()
    c.wait_derr()
    c.check("password of another user: mismatch", "stimmen nicht überein" in c.dlg()["err"], c.dlg()["err"])
    c.fill_dlg("neupass1", "neupass1")
    c.dlg_yes()
    ok = c.wait_dlg_closed() and c.wait_toast("Das Passwort von „tester“ wurde geändert.")
    c.check("password set: new one works over the API, old one does not", ok and c.mock.api("/api/status", ("tester", "neupass1")).get("version")
            and c.mock.api("/api/status", ("tester", "secret1")).get("code") in ("UNAUTHORIZED", "BAD_LOGIN"), c.toast_texts())
    # delete
    c.tap('[data-umore="tester"]')
    c.wait("() => !document.querySelector('#menu').hidden")
    c.tap('#menu [data-act="del"]')
    ok = c.wait_dlg("Benutzer „tester“ löschen?")
    c.dlg_yes()
    ok = ok and c.wait_dlg_closed() and c.wait("() => document.querySelectorAll('#userList .li').length === 2")
    c.check("user deleted (list, toast, mock)", ok and c.wait_toast("„tester“ wurde gelöscht.") and all(u["name"] != "tester" for u in c.mock.state()["users"]), c.toast_texts())
    # last admin
    c.tap('[data-umore="admin"]')
    c.wait("() => !document.querySelector('#menu').hidden")
    c.check("own row: no 'Neues Passwort festlegen' (the user menu has 'Passwort ändern')", c.menu()["items"] == ["Administratorrechte entziehen", "Löschen"], c.menu()["items"])
    c.tap('#menu [data-act="del"]')
    c.wait_dlg("Benutzer „admin“ löschen?")
    c.check("deleting your own account warns that you will be signed out", "dein eigenes Konto" in c.dlg()["body"], c.dlg()["body"][-60:])
    c.dlg_yes()
    ok = c.wait_derr()
    c.check("last administrator cannot be deleted (LAST_ADMIN text)", ok and "letzte Administrator" in c.dlg()["err"], c.dlg()["err"])
    c.dlg_no()
    c.wait_dlg_closed()
    c.tap('[data-umore="admin"]')
    c.wait("() => !document.querySelector('#menu').hidden")
    c.tap('#menu [data-act="role"]')
    c.wait_dlg("die Administratorrechte entziehen?")
    c.check("demoting yourself warns about losing access", "verlierst" in c.dlg()["body"], c.dlg()["body"][-60:])
    c.dlg_yes()
    ok = c.wait_derr()
    c.check("last administrator cannot be demoted", ok and "letzte Administrator" in c.dlg()["err"], c.dlg()["err"])
    c.dlg_no()
    c.wait_dlg_closed()
    # change own password via the user menu (its only place)
    c.ev("() => scrollTo(0, 0)")
    c.tap("#userBtn")
    c.wait("() => !document.querySelector('#menu').hidden")
    c.tap('#menu [data-act="pw"]')
    ok = c.wait_dlg("Passwort ändern")
    c.check("own password dialog: current, new, repeat", ok and len(c.dlg()["fields"]) == 3, c.dlg()["fields"])
    c.shot("own_password_dialog")
    c.fill_dlg("falsch", "neuespw1", "neuespw1")
    c.dlg_yes()
    c.wait_derr()
    c.check("own password: wrong current one", c.dlg()["err"] == "Das aktuelle Passwort stimmt nicht.", c.dlg()["err"])
    c.fill_dlg(ADMIN[1], ADMIN[1], ADMIN[1])
    c.dlg_yes()
    c.wait_derr()
    c.check("own password: new equals current", "unterscheiden" in c.dlg()["err"], c.dlg()["err"])
    c.fill_dlg(ADMIN[1], "neuespw1", "neuespw1")
    c.dlg_yes()
    ok = c.wait_dlg_closed() and c.wait_toast("Das Passwort wurde geändert.")
    c.check("own password changed: this session and its trusted device stay valid", ok and c.ui()["auth"] == "ok" and c.mock.api("/api/session", ("admin", "neuespw1")).get("ok"), c.toast_texts())

    # ---- trusted devices
    c.start("two_users", trust=True, wifi_count=1)
    c.go("#einstellungen")
    c.wait("() => document.querySelectorAll('#devList .li').length >= 1")
    open_card(c, "sec")
    c.ev("() => document.querySelector('#s-dev').scrollIntoView()")
    li = c.ev("() => [...document.querySelectorAll('#devList .li')].map(l => l.textContent.replace(/\\s+/g, ' ').trim())")
    c.check("trusted devices: this device listed with a mark and the account (admin sees accounts)", len(li) == 1 and "Dieses Gerät" in li[0] and "Konto admin" in li[0]
            and c.text("#devCount") == "1 von 8 Plätzen belegt.", li)
    c.shot("settings_devices")
    c.overflow_check("devices section")
    c.start("two_users", wifi_count=1)                       # untrusted session: stays valid when the mock replaces all devices
    c.mock.set(devices=3)
    c.go("#einstellungen")
    c.go("#/")
    c.go("#einstellungen")
    c.wait("() => document.querySelectorAll('#devList .li').length === 3")
    open_card(c, "sec")
    li = c.ev("() => [...document.querySelectorAll('#devList .li')].map(l => l.textContent.replace(/\\s+/g, ' ').trim())")
    c.check("three devices listed, none is the current one", len(li) == 3 and not any("Dieses Gerät" in x for x in li) and c.text("#devCount") == "3 von 8 Plätzen belegt.", li)
    first = c.ev("() => document.querySelector('#devList [data-ddel]').getAttribute('aria-label')")
    c.tap("#devList [data-ddel] >> nth=0")
    ok = c.wait_dlg("Gerät „")
    c.check("remove device: confirmation names it", ok and "entfernen?" in c.dlg()["title"] and first.startswith("„"), c.dlg()["title"])
    c.dlg_yes()
    ok = c.wait_dlg_closed() and c.wait("() => document.querySelectorAll('#devList .li').length === 2")
    c.check("device removed: list, toast and mock", ok and c.wait_toast("entfernt.") and len(c.mock.state()["devices"]) == 2, c.toast_texts())
    # remove the current device: signed out
    c.start("two_users", trust=True, wifi_count=1)
    c.go("#einstellungen")
    c.wait("() => document.querySelectorAll('#devList .li').length >= 1")
    open_card(c, "sec")
    c.tap("#devList [data-ddel] >> nth=0")
    c.wait_dlg("entfernen?")
    c.check("removing this device warns that you will be signed out", "wirst hier abgemeldet" in c.dlg()["body"], c.dlg()["body"])
    c.dlg_yes()
    ok = c.wait("() => S.auth === 'login'")
    c.check("removing the current device signs out", ok and c.ui()["login"] and len(c.mock.state()["devices"]) == 0)
    c.start("two_users", wifi_count=1)
    c.go("#einstellungen")
    c.wait("() => document.querySelector('#devList').textContent.includes('Noch kein vertrautes Gerät')")
    c.check("no trusted device: friendly hint", "Noch kein vertrautes Gerät" in c.text("#devList"))
    c.no_console_errors()


@test
def t05c_firmware_update(c: Ctx):
    p = c.start("two_users", wifi_count=1)
    c.go("#einstellungen")
    c.wait("() => !document.querySelector('#vSettings').hidden")
    open_card(c, "sys")
    ver0 = c.mock.state()["version"]
    slot0 = c.mock.state()["status"]["ota"]["partition"]
    new_ver = f"0.9.{int(time.time()) % 1000}"
    c.mock.set(next_version=new_ver)
    c.shot("settings_system")
    c.overflow_check("system section")
    for key, msg in (("fw_txt", "Bitte eine Firmware-Datei mit der Endung .bin wählen."), ("fw_small", "Die Datei ist zu klein"), ("fw_bad", "Diese Datei ist keine Gerätesoftware")):
        p.set_input_files("#otaPick", str(c.run.f[key]))
        c.wait("() => document.querySelector('#otaMsg').textContent !== ''")
        c.check(f"update with {c.run.f[key].name}: message, no dialog", c.text("#otaMsg").startswith(msg) and not c.ev("() => document.querySelector('#dlg').open"), c.text("#otaMsg"))
        c.ev("() => { document.querySelector('#otaMsg').textContent = ''; }")
    c.shot("ota_message")
    p.set_input_files("#otaPick", str(c.run.f["fw"]))
    ok = c.wait_dlg("Gerätesoftware aktualisieren?")
    d = c.dlg()
    c.check("update dialog: names the file and size, 'Aktualisieren'", ok and "printdrive.bin" in d["body"] and "kB" in d["body"] and d["yes"] == "Aktualisieren", d["body"])
    c.shot("ota_dialog")
    c.overflow_check("update dialog")
    c.dlg_no()
    c.wait_dlg_closed()
    c.check("cancel: nothing sent", not any("/api/ota" in u for _, u in c.requests) and c.mock.state()["version"] == ver0)
    # the mock rejects the image (IO) -> message under the button
    c.mock.set(ota_fail="IO")
    p.set_input_files("#otaPick", str(c.run.f["fw"]))
    c.wait_dlg("Gerätesoftware aktualisieren?")
    c.dlg_yes()
    ok = c.wait("() => document.querySelector('#otaMsg').textContent !== ''", None, 15000)
    c.check("update fails (IO): specific message, button usable again", ok and c.text("#otaMsg").startswith("Beim Schreiben der neuen Version") and not c.ev("() => document.querySelector('#otaBtn').disabled"), c.text("#otaMsg"))
    c.mock.set(ota_fail="none")
    # success: progress, restart notice, back at the sign-in view (sessions end with the restart)
    p.set_input_files("#otaPick", str(c.run.f["fw"]))
    c.wait_dlg("Gerätesoftware aktualisieren?")
    c.dlg_yes()
    ok = c.wait("() => /Das Laufwerk startet neu/.test(document.querySelector('#notice').textContent)", None, 15000)
    c.check("update sent: 'Das Laufwerk startet neu …' notice", ok, c.ui()["notice"])
    c.shot("ota_restarting")
    ok = c.wait("() => S.auth === 'login'", None, 40000)
    c.check("after the restart: sign-in view 'Das Laufwerk wurde neu gestartet'", ok and login_note(c).startswith("Das Laufwerk wurde neu gestartet"), login_note(c))
    c.login(ADMIN)
    c.go("#einstellungen")
    c.wait("v => document.querySelector('#sysVer').textContent === v", new_ver)
    pend = c.ev("() => !document.querySelector('#otaPend').hidden")
    c.check("new version shown (other slot) with the trial notice", c.text("#sysVer") == new_ver and c.text("#sysSlot") not in (slot0, "–") and pend, f"{c.text('#sysVer')} {c.text('#sysSlot')} pend={pend}")
    c.shot("ota_done")
    c.no_console_errors(net=True)


@test
def t05d_restart_and_offline(c: Ctx):
    p = c.start("files", wifi_count=1)
    c.mock.set(online=0)
    ok = c.wait("() => document.querySelector('#notice').textContent.includes('Keine Antwort vom Laufwerk')", None, 20000)
    c.check("device unreachable: notice after two failed polls", ok, c.ui()["notice"])
    c.shot("offline_notice")
    c.overflow_check("offline notice")
    c.mock.set(online=1)
    ok = c.wait("() => document.querySelector('#notice').hidden", None, 12000)
    c.check("device back: notice disappears, still signed in", ok and c.ui()["auth"] == "ok")
    c.go("#einstellungen")
    c.wait("() => !document.querySelector('#vSettings').hidden")
    open_card(c, "sys")
    c.tap("#rebootBtn")
    c.wait_dlg("Laufwerk neu starten?")
    c.dlg_yes()
    ok = c.wait("() => document.querySelector('#notice').textContent.includes('startet neu')", None, 8000)
    c.check("restart: notice 'Das Laufwerk startet neu …'", ok, c.ui()["notice"])
    ok = c.wait("() => S.auth === 'login'", None, 40000)
    c.check("restart ends all sessions: sign-in view with the restart reason", ok and login_note(c).startswith("Das Laufwerk wurde neu gestartet"), login_note(c))
    c.shot("login_after_restart")
    # a trusted device survives the restart
    c.start("files", trust=True, wifi_count=1)
    c.go("#einstellungen")
    c.wait("() => !document.querySelector('#vSettings').hidden")
    open_card(c, "sys")
    c.tap("#rebootBtn")
    c.wait_dlg("Laufwerk neu starten?")
    c.dlg_yes()
    ok = c.wait_toast("wieder bereit", 40000)
    c.check("trusted device: still signed in after the restart ('wieder bereit' toast)", ok and c.ui()["auth"] == "ok", c.ui()["auth"])
    c.no_console_errors(net=True)


# ================================================================== 6. role user

@test
def t06_role_user(c: Ctx):
    c.start("two_users", who=GUEST, trust=False)
    ui = c.ui()
    c.check("role user: file management works (list, folders, new folder, upload)", ui["app"] and ui["title"] == T_APP and len(ui["rows"]) >= 3 and (ui["mkBtn"] or c.phone) and ui["upBtn"], f"{ui['title']} {len(ui['rows'])} rows")
    c.check("avatar shows the user", c.text("#userAv") == "g")
    c.shot("files_user_role")
    c.tap("#userBtn")
    c.wait("() => !document.querySelector('#menu').hidden")
    c.check("account menu shows the role 'Benutzer'", "Benutzer" in c.menu()["head"] and "gast" in c.menu()["head"], c.menu()["head"])
    c.page.keyboard.press("Escape")
    c.go("#einstellungen")
    c.wait("() => !document.querySelector('#vSettings').hidden")
    c.wait("() => document.querySelector('#devList .li') !== null")
    tabs = c.ev(JS_TABS)
    c.check("settings cards for a user: Speicher & USB, Sicherheit, Darstellung & Sprache", tabs == ["Speicher & USB", "Sicherheit", "Darstellung & Sprache"], tabs)
    shown = []
    for k in ("usb", "sec", "gen"):
        open_card(c, k)
        shown += c.ev("() => ['#swWrite', '#wifiAdd', '#userAdd', '#rebootBtn', '#logBtn', '#otaBtn', '#fmtBtn', '#flashBtn', '#wifiList', '#userList', '#swHttps', '#alItem'].filter(s => document.querySelector(s).getClientRects().length > 0)")
    c.check("no admin control visible in any open card (USB write switch, Wi-Fi, users, restart, log, update, format, maintenance, HTTPS)", not shown, shown)
    c.check("no account block in the settings", not c.ev("() => !!document.querySelector('#acctX, #pwBtn, #outBtn')"))
    open_card(c, "usb")
    ok = vis(c, "#swAuto")
    open_card(c, "gen")
    c.check("the user's own settings work: auto switch and theme", ok and vis(c, "#theme"))
    open_card(c, "sec")
    reqs = [u for m, u in c.requests if u.split("?")[0].endswith(("/api/users", "/api/wifi/list", "/api/log"))]
    c.check("the page does not even ask for admin data", not reqs, reqs)
    c.check("trusted devices: a user sees no account line", not c.ev("() => document.querySelector('#devList').textContent.includes('Konto ')"))
    c.shot("settings_user_role")
    c.shot("settings_user_role_full", full=True)
    c.overflow_check("settings as user")
    r = c.ev("async () => { const r = await fetch('/api/users', {credentials: 'same-origin'}); return [r.status, (await r.json()).code]; }")
    c.check("API: /api/users answers 403 FORBIDDEN_ROLE", r == [403, "FORBIDDEN_ROLE"], r)
    # broken storage as a user: no format link
    c.start("two_users", who=GUEST, fs_ok=0, needs_format=1)
    c.check("broken storage as user: hint to ask an administrator, no format link", c.ui()["stateH"] == "Der Speicher muss eingerichtet werden"
            and "Ein Administrator kann ihn" in c.text("#list .state p") and not c.ev("() => !!document.querySelector('#list .state a')"), c.text("#list .state p")[:60])
    c.shot("no_fs_user")
    c.no_console_errors()


# ================================================================== 7. language

@test
def t07_language(c: Ctx):
    c.reset("two_users")
    p = c.open()
    r = c.ev(JS_UNTRANSLATED)
    c.check("German sign-in view: no foreign or raw key texts", not r["bad"] and r["n"] >= 5, r)
    c.tap("#loginForm [data-lang]")
    c.wait("() => lang === 'en'")
    r = c.ev(JS_UNTRANSLATED)
    texts = c.ev("() => ({u: document.querySelector('label[for=lUser]').textContent, btn: document.querySelector('#loginBtn').textContent, t: document.querySelector('#lTrust').nextElementSibling.textContent, "
                 "lang: document.documentElement.lang, other: document.querySelector('#loginForm [data-lang]').textContent})")
    c.check("switch on the sign-in view: English texts, <html lang>, link now offers Deutsch", texts == {"u": "User name", "btn": "Sign in", "t": "Trust this device", "lang": "en", "other": "Deutsch"} and not r["bad"], f"{texts} {r['bad']}")
    c.shot("login_en")
    c.overflow_check("sign-in in English")
    p.fill("#lUser", "admin")
    p.fill("#lPass", "x")
    c.tap("#loginBtn")
    c.wait("() => document.querySelector('#loginMsg').textContent !== ''")
    c.check("English error text for a wrong password", c.text("#loginMsg") == "Wrong user name or password.", c.text("#loginMsg"))
    p.fill("#lPass", ADMIN[1])
    p.set_checked("#lTrust", False)
    c.tap("#loginBtn")
    c.wait_ready()
    ui = c.ui()
    c.check("language kept after sign-in: English file view", ui["title"] == T_APP and ui["status"] == "Ready for uploads" and ui["h1"] == "My files" and ui["btn"] == "USB device", f"{ui['status']} / {ui['h1']}")
    r = c.ev(JS_UNTRANSLATED)
    c.check("English file view: no German leftovers or raw keys", not r["bad"], r)
    c.shot("files_en")
    c.overflow_check("file list in English")
    c.open_menu("README")
    c.check("English row menu", c.menu()["items"] == ["Download", "Rename", "Cut", "Copy", "Select", "Delete"], c.menu()["items"])
    r = c.ev(JS_UNTRANSLATED)
    c.check("English menu: no foreign texts", not r["bad"], r)
    c.shot("row_menu_en")
    c.page.keyboard.press("Escape")
    checks = []

    def dialog(label, opener, title_part):
        opener()
        ok = c.wait_dlg(title_part)
        r = c.ev(JS_UNTRANSLATED)
        d = c.dlg()
        checks.append((label, ok, r["bad"], d["title"]))
        c.shot("dlg_en_" + label)
        c.overflow_check(f"English dialog {label}")
        c.dlg_no()
        c.wait_dlg_closed()

    dialog("mkdir", lambda: c.new_folder(), "New folder")
    dialog("rename", lambda: c.act("README", "ren"), "Rename file")
    dialog("delete", lambda: c.act("README", "del"), "Delete file?")
    for label, ok, bad, title in checks:
        c.check(f"English dialog {label}: title '{title}', no foreign or raw texts", ok and not bad, bad)
    c.act("README", "cut")
    c.wait("() => !document.querySelector('#clipbar').hidden")
    r = c.ev(JS_UNTRANSLATED)
    c.check("English clipboard bar", c.ui()["clipTxt"] == "1 item ready to move" and not r["bad"], f"{c.ui()['clipTxt']} {r['bad']}")
    c.tap("#clipX")
    c.select_rows("README", "LEER.TXT")
    r = c.ev(JS_UNTRANSLATED)
    c.check("English selection bar", c.ui()["selCount"] == "2 selected" and not r["bad"], f"{c.ui()['selCount']} {r['bad']}")
    c.shot("selection_en")
    c.tap("#sDel")
    c.wait_dlg("Delete 2 items?")
    r = c.ev(JS_UNTRANSLATED)
    c.check("English multi delete dialog", not r["bad"] and c.dlg()["yes"] == "Delete", f"{c.dlg()['title']} {r['bad']}")
    c.dlg_no()
    c.wait_dlg_closed()
    c.tap("#sX")
    # settings in English
    c.mock.set(devices=2, wifi_count=2, scan_ms=200)
    c.go("#einstellungen")
    c.wait("() => document.querySelector('#userList .li') !== null && document.querySelector('#wifiList .li') !== null && document.querySelectorAll('#devList .li').length === 2")
    bad, n = [], 0
    for k in ("net", "usb", "sec", "users", "gen", "sys"):
        open_card(c, k)
        r = c.ev(JS_UNTRANSLATED)
        bad += r["bad"]
        n += r["n"]
    c.check("English settings (every card opened): no German leftovers or raw keys", not bad and n > 60, f"{n} {bad[:6]}")
    tabs = c.ev(JS_TABS)
    c.check("English settings cards", tabs == ["Wi-Fi", "Storage & USB", "Security", "Users", "Appearance & language", "System"], tabs)
    c.shot("settings_en", full=True)
    c.overflow_check("settings in English")
    pressed = c.ev("() => [...document.querySelectorAll('#langSeg button')].map(b => b.getAttribute('aria-pressed'))")
    c.check("language buttons show English selected", pressed == ["false", "true"], pressed)
    c.check("English devices texts", "slots used" in c.text("#devCount") and "Remove" in c.ev("() => document.querySelector('#devList [data-ddel]').getAttribute('aria-label')"), c.text("#devCount"))
    def via_user_menu():
        c.tap("#userBtn")
        c.wait("() => !document.querySelector('#menu').hidden")
        c.tap('#menu [data-act="pw"]')
    for label, opener, part in (("wifi", lambda: (open_card(c, "net"), c.tap("#wifiAdd")), "Add network"), ("user", lambda: (open_card(c, "users"), c.tap("#userAdd")), "Add user"),
                                ("pw", via_user_menu, "Change password"), ("format", lambda: (open_card(c, "usb"), c.tap("#fmtBtn")), "Format the storage?"),
                                ("restart", lambda: (open_card(c, "sys"), c.tap("#rebootBtn")), "Restart the drive?")):
        opener()
        ok = c.wait_dlg(part)
        r = c.ev(JS_UNTRANSLATED)
        c.check(f"English dialog {label}: '{c.dlg()['title']}', no foreign or raw texts", ok and not r["bad"], r["bad"])
        c.shot("dlg_en_" + label)
        c.dlg_no()
        c.wait_dlg_closed()
    open_card(c, "users")
    c.tap('[data-umore="gast"]')
    c.wait("() => !document.querySelector('#menu').hidden")
    m = c.menu()
    c.check("English user menu", m["items"] == ["Make administrator", "Set new password", "Delete"], m["items"])
    c.page.keyboard.press("Escape")
    open_card(c, "sys")
    c.tap("#logBtn")
    c.wait("() => !document.querySelector('#log').hidden")
    p.set_input_files("#otaPick", str(c.run.f["fw_bad"]))
    c.wait("() => document.querySelector('#otaMsg').textContent !== ''")
    c.check("English firmware message", c.text("#otaMsg") == "This file is not firmware for this drive.", c.text("#otaMsg"))
    # persistence and switching back
    p.reload()
    c.wait_ready()
    c.check("English persists across a reload (stored per browser)", c.ui()["lang"] == "en" and c.ev("() => localStorage.getItem('dd-lang')") == "en")
    c.go("#einstellungen")
    c.wait("() => !document.querySelector('#vSettings').hidden")
    open_card(c, "gen")
    c.tap('#langSeg button[data-v="de"]')
    c.wait("() => lang === 'de'")
    r = c.ev(JS_UNTRANSLATED)
    c.check("back to German via the settings: German texts, no English leftovers", c.ui()["lang"] == "de" and c.text("#vSettings h1") == "Einstellungen"
            and c.text("#sumGen") == "Automatisch · Deutsch" and not r["bad"], r["bad"])
    c.go("#/")
    c.wait_dir("")
    c.check("German list again", c.ui()["h1"] == "Meine Dateien" and c.ui()["title"] == T_APP)
    # first visit with an English browser picks English
    c.reset("two_users")
    c.open(locale="en-GB")
    c.check("browser language English: sign-in view starts in English", c.ui()["lang"] == "en" and c.text("#loginBtn") == "Sign in", c.text("#loginBtn"))
    c.no_console_errors()


# ================================================================== 8. layout checks, phone and desktop

@test
def t08_layout(c: Ctx):
    c.start("files")
    long_name = "Überlanger Dateiname für die Größenprüfung der Oberfläche, Lüfterhaube mit Düsenkühlung, Maße geändert v7.3mf"
    c.check("long name: extension stays visible", c.ev("n => { const r = document.querySelector('#list .row[data-name=\"' + n + '\"] .ne'); return !!r && r.getClientRects().length > 0 && r.textContent === '.3mf'; }", long_name))
    c.overflow_check("long file names")
    c.shot("layout_list_long_names")
    if c.phone:
        res = c.ev(JS_TARGETS, ["#userBtn", "#navSettings", "#segApp", "#segUsb", "#modeInfo", "#encInfo", "#encSetup", "#upBtn", "#search", "#sortM", "#list .row [data-more]"])
        c.open_menu("README")
        res += c.ev(JS_TARGETS, ["#menu [data-act]"])
        c.page.keyboard.press("Escape")
        c.select_rows("README")
        res += c.ev(JS_TARGETS, ["#sX", "#sAll", "#sCut", "#sCopy", "#sDel"])
        c.tap("#sX")
        for r in res:
            if r.get("missing"):
                c.check(f"touch target {r['sel']}: found", False, "element not visible")
            else:
                c.check(f"touch target {r['sel']} at least 44x44 px", r["w"] >= 43.5 and r["h"] >= 43.5, f"{r['w']}x{r['h']}")
    # small phone / narrow window
    for vw, vh in ((320, 568), (360, 740)) if c.phone else ((900, 700), (721, 700)):
        c.reset("files")
        c.open("", viewport={"width": vw, "height": vh})
        c.login(ADMIN)
        c.overflow_check(f"{vw} px wide: file list")
        c.go("#/Projekte/Drachen/Teile")
        c.wait_dir("Projekte/Drachen/Teile")
        c.overflow_check(f"{vw} px wide: deep folder")
        first = c.ev("() => S.entries.length ? S.entries[0].name : ''")
        if first:
            c.select_rows(first)
            c.overflow_check(f"{vw} px wide: selection")
        c.go("#einstellungen")
        c.wait("() => !document.querySelector('#vSettings').hidden")
        c.wait("() => document.querySelector('#userList .li') !== null")
        c.overflow_check(f"{vw} px wide: settings")
        for k in ("net", "usb", "sec", "users", "gen", "sys"):
            open_card(c, k)
            c.page.wait_for_timeout(350)                    # the card body fades in (transform/opacity only)
            c.overflow_check(f"{vw} px wide: settings card {k} open")
        c.go("#!hilfe")
        c.wait("() => !document.querySelector('#vHelp').hidden")
        c.page.wait_for_timeout(350)
        c.overflow_check(f"{vw} px wide: help page")
        c.shot(f"w{vw}_settings")
    if not c.phone:
        c.reset("files")
        c.open("", viewport={"width": 1920, "height": 1080})
        c.login(ADMIN)
        w = c.ev("() => document.querySelector('#app').getBoundingClientRect().width")
        c.check("wide window: content column limited to 984 px (952 px content, as in the mockup)", w <= 984 + 1, w)
        c.shot("wide_1920")
    c.no_console_errors()


# ================================================================== 9. API v4: encrypted drive, Wi-Fi scan, lock-out wording

DEF_PW = "Geheimes-Passwort-12"                         # drive password of the mock scenarios crypt_enabled / crypt_locked
DEF_KEY = "7K3M-Q9XD-4WTB-HN2C-5RYE-8FGA-ZP6V-JC3S"     # their recovery key
NEW_PW = "Neues-Laufwerk-Pw-99"
KEY_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{4}(-[0-9A-HJKMNP-TV-Z]{4}){7}$")
T_LOCKED_DONE = "Das Laufwerk ist jetzt gesperrt. Zum Weiterarbeiten bitte entsperren."
LOGIN_LOCK_DE_RE = re.compile(r"^Zu viele Fehlversuche\. Bitte warte noch \d+ Sekunden\. Jede weitere Sperre dauert länger\.$")
LOGIN_LOCK_EN_RE = re.compile(r"^Too many failed attempts\. Please wait \d+ seconds\. Each further lock-out lasts longer\.$")
# drive password throttle (one counter for the whole device; the first two wrong inputs are free, then 5 s, doubling, at most 300 s)
LOCK_DE_RE = re.compile(r"^Zu viele falsche Eingaben\. Nächster Versuch in \d+ Sekunden?\. Jeder weitere Fehlversuch verdoppelt die Wartezeit \(höchstens 5 Minuten\); sie gilt für alle Geräte\.$")
LOCK_EN_RE = re.compile(r"^Too many wrong entries\. Next try in \d+ seconds?\. Each further wrong entry doubles the wait \(at most 5 minutes\); it applies to all devices\.$")
WRONG_PW_DE = "Das Laufwerks-Passwort ist falsch."
AUTH_DE = ("Die Benutzerverwaltung des Laufwerks ist nicht lesbar, deshalb ist keine Anmeldung möglich. Abhilfe: die Taste am "
           "Laufwerk 10 Sekunden lang halten – danach gilt wieder admin / admin. Die Dateien bleiben erhalten.")
AUTH_EN = ("The drive can’t read its user database, so nobody can sign in. To fix this, hold the button on the drive for 10 "
           "seconds — the sign-in is then reset to admin / admin. Your files are kept.")

JS_LOCK = r"""() => {
  const q = s => document.querySelector(s);
  const vis = el => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
  return {unlock: vis(q('#unlock')), list: vis(q('#list')), toolbar: vis(q('#toolbar')), crumbs: vis(q('#crumbs')),
    h2: q('#unlock h2').textContent, lbl: q('#ulLbl').textContent, ftype: q('#ulPass').type, fval: q('#ulPass').value,
    btn: q('#ulBtn').textContent.trim(), btnDisabled: q('#ulBtn').disabled, msg: q('#ulMsg').textContent,
    mode: q('#ulMode').textContent, eye: vis(q('#ulEye')), eyePressed: q('#ulEye').getAttribute('aria-pressed'),
    lockBtn: vis(q('#lockBtn')), encTag: vis(q('#encCard')) && !q('#encCard').classList.contains('off'), encTagTxt: q('#encTitle').textContent,
    encSub: q('#encSub').textContent, encInfo: q('#encInfo').dataset.x || '', encSetup: vis(q('#encSetup')),
    modeTitle: q('#modeTitle').textContent, modeSub: q('#modeInfo').dataset.x || '',
    modeBtn: [...document.querySelectorAll('.seg2 button')].some(x => !x.disabled && x.getAttribute('aria-pressed') !== 'true'),
    stoSub: q('#stoSub').textContent, stoTip: q('#stoCell').title, stateH: q('#list .state h2') ? q('#list .state h2').textContent.trim() : null,
    unlockBtns: [...document.querySelectorAll('#app button, #app a')].filter(e => vis(e) && e.textContent.trim() === (lang === 'de' ? 'Entsperren' : 'Unlock')).length,
    lost: vis(q('#ulLost')), lostTxt: q('#ulLost').textContent, focus: document.activeElement ? document.activeElement.id : ''};
}"""

JS_SCAN = r"""() => {
  const q = s => document.querySelector(s), box = q('#scanList');
  const m = box && box.querySelector('.scanmsg');
  return {msg: m ? m.textContent.trim() : '', spin: !!(m && m.querySelector('.spin')), againDisabled: q('#scanAgain').disabled,
    scanning: S.scanning, scrollable: box ? box.scrollHeight > box.clientHeight + 1 : false,
    nets: box ? [...box.querySelectorAll('.net')].map(b => ({name: b.querySelector('.nn').textContent, title: b.title,
      aria: b.getAttribute('aria-label'), pill: b.querySelector('.pill') ? b.querySelector('.pill').textContent : '',
      svgs: b.querySelectorAll('svg').length, on: b.classList.contains('on')})) : [],
    foreign: box ? box.querySelectorAll('img,script,iframe,b,i,a').length : 0};
}"""


def ul(c: Ctx) -> dict:
    return c.ev(JS_LOCK)


def ul_wait_msg(c: Ctx, timeout=9000) -> bool:
    return c.wait("() => document.querySelector('#ulMsg').textContent !== ''", None, timeout)


def ul_submit(c: Ctx, value: str, wait=True) -> bool:
    c.page.fill("#ulPass", value)
    c.tap("#ulBtn")
    return ul_wait_msg(c) if wait else True


def wait_unlock_view(c: Ctx, shown=True, timeout=9000) -> bool:
    return c.wait("s => (document.querySelector('#unlock').getClientRects().length > 0) === s", shown, timeout)


def n_post(c: Ctx, suffix: str) -> int:
    return len([1 for m, u in c.requests if m == "POST" and u.split("?")[0].endswith(suffix)])


def secs(msg: str) -> int:
    m = re.search(r"(\d+) (?:Sekunden|seconds)", msg)
    return int(m.group(1)) if m else -1


def crypt_btns(c: Ctx) -> list:
    return c.ev("() => ['cUnlock','cLock','cSetup','cPw','cRec','cOff','cErase'].filter(id => document.getElementById(id).getClientRects().length > 0)")


def to_settings(c: Ctx):
    """Settings, card 'Sicherheit' open, scrolled to the encryption item."""
    c.go("#einstellungen")
    c.wait("() => !document.querySelector('#vSettings').hidden")
    open_card(c, "sec")
    c.wait("() => !!document.querySelector('#cryptItem') && !document.querySelector('#cryptItem').hidden")
    c.ev("() => document.querySelector('#cryptItem').scrollIntoView()")


def open_card(c: Ctx, key: str):
    """Opens the settings card #s-<key> (the cards start closed; one is open at a time)."""
    c.ev("k => { const h = document.getElementById('h-' + k); if (h && h.getAttribute('aria-expanded') !== 'true') h.click(); }", key)
    c.wait("k => !document.getElementById('b-' + k).hidden", key, 3000)


def key_ok_label(c: Ctx) -> str:
    return c.ev("() => { const i = document.querySelector('#dF0'); const l = i && i.closest('label'); return l ? l.textContent.trim() : ''; }")


def dlg_wait_err(c: Ctx, timeout=9000) -> bool:
    return c.wait("() => document.querySelector('#dErr').textContent.trim() !== ''", None, timeout)


def lock_out_via_ui(c: Ctx, wrong="falsch-falsch-falsch") -> int:
    """Wrong drive passwords until the unlock view starts a waiting time (third wrong input). Returns the requests sent."""
    for _ in range(6):
        if ul(c)["btnDisabled"]:
            break
        ul_submit(c, wrong)
    return n_post(c, "/api/crypt/unlock")


def force_429(c: Ctx, wrong="falsch-falsch-falsch"):
    """As if another device had caused the waiting time: the page does not know it, the device answers 429 LOCKED."""
    c.ev("() => { ulUntil = 1; ulTick(); }")
    c.page.fill("#ulPass", wrong)
    c.tap("#ulBtn")
    ul_wait_msg(c)


def clear_ul_lock(c: Ctx):
    """Ends the waiting time on the mock and in the page (the real wait grows up to 5 minutes)."""
    c.mock.set(crypt="locked")
    c.ev("() => { ulUntil = 1; ulTick(); }")


@test
def t09a_unlock_view(c: Ctx):
    c.start("crypt_locked", crypt_kdf_ms=50)
    u = ul(c)
    ui = c.ui()
    c.check("locked drive: unlock view instead of list, toolbar and breadcrumb", u["unlock"] and not u["list"] and not u["toolbar"] and not u["crumbs"]
            and u["h2"] == "Laufwerk gesperrt" and not ui["mkBtn"] and not ui["fab"] and not ui["upBtn"], u)
    c.check("locked drive: the mode card does not repeat 'gesperrt' (it says what the USB device sees), segments off, no 'format' offer",
            u["modeTitle"] == "USB-Gerät sieht kein Laufwerk" and u["modeSub"] == "" and not u["modeBtn"] and u["stateH"] is None
            and not c.ev("() => document.querySelector('#status').textContent.includes('gesperrt')"),
            f"{u['modeTitle']} / {u['modeSub']} / {u['stateH']}")
    c.check("locked drive: storage line without size (reason as tooltip); encryption card 'Verschlüsselt · Gesperrt', no lock button",
            u["stoSub"] == "" and u["stoTip"] == "Größe erst nach dem Entsperren sichtbar" and u["encTag"] and u["encTagTxt"] == "Verschlüsselt"
            and u["encSub"] == "Gesperrt" and not u["lockBtn"], f"{u['stoSub']} {u['encTagTxt']} {u['encSub']}")
    c.check("'Entsperren' exists once on the screen (in the panel, not in the card)", u["unlockBtns"] == 1 and not c.ev("() => !!document.querySelector('#encCard .btn:not([hidden])')"), u["unlockBtns"])
    c.check("unlock form: drive password field (password type, label for screen readers, same placeholder), 'Entsperren', recovery key link; explanation behind the card's ⓘ",
            u["lbl"] == "Laufwerks-Passwort" and u["ftype"] == "password" and c.ev("() => document.querySelector('#ulPass').placeholder") == "Laufwerks-Passwort"
            and u["btn"] == "Entsperren" and u["mode"] == "Stattdessen den Wiederherstellungsschlüssel verwenden" and u["eye"]
            and "nicht das Anmelde-Passwort" in u["encInfo"] and "auch der Drucker oder PC kein Laufwerk" in u["encInfo"] and not u["msg"]
            and not c.ev("() => !!document.querySelector('#unlock p.small')") and u["lost"] and u["lostTxt"] == "Passwort und Wiederherstellungsschlüssel verloren?", u)
    if not c.phone:
        c.check("desktop: focus starts in the drive password field", u["focus"] == "ulPass", u["focus"])
    r = c.ev(JS_UNTRANSLATED)
    c.check("unlock view (German): no foreign or raw key texts", not r["bad"] and r["n"] > 6, r)
    nm = c.ev(JS_NAMES)
    c.check("unlock view: every control has an accessible name", not nm["bad"], f"{nm['n']} controls {nm['bad'][:3]}")
    c.shot("unlock_view")
    c.overflow_check("unlock view")
    if c.phone:
        res = c.ev(JS_TARGETS, ["#ulBtn", "#ulEye", "#ulMode"])
        for t in res:
            c.check(f"touch target {t['sel']} at least 44x44 px" if t["sel"] != "#ulMode" else "touch target #ulMode (link) at least 40 px high",
                    not t.get("missing") and (t["h"] >= (40 if t["sel"] == "#ulMode" else 43.5) and (t["w"] >= 43.5 or t["sel"] == "#ulMode")), t)
    # empty submit
    c.tap("#ulBtn")
    c.check("empty submit: hint, no request", ul(c)["msg"] == "Bitte das Laufwerks-Passwort eingeben." and n_post(c, "/api/crypt/unlock") == 0, ul(c)["msg"])
    # eye button
    c.page.fill("#ulPass", "geheim")
    c.tap("#ulEye")
    u = ul(c)
    c.check("eye button shows the typed password (type text, aria-pressed)", u["ftype"] == "text" and u["eyePressed"] == "true", u["ftype"])
    c.tap("#ulEye")
    c.check("eye button hides it again", ul(c)["ftype"] == "password" and ul(c)["eyePressed"] == "false")
    # wrong password
    ok = ul_submit(c, "falsches-passwort-1")
    u = ul(c)
    c.check("wrong password: German text, button usable again, still locked, focus back in the field",
            ok and u["msg"] == "Das Laufwerks-Passwort ist falsch." and not u["btnDisabled"] and u["unlock"] and u["btn"] == "Entsperren"
            and c.mock.state()["crypt"]["unlocked"] is False and (c.phone or u["focus"] == "ulPass"), f"{u['msg']} {u['focus']}")
    c.shot("unlock_wrong")
    c.overflow_check("unlock view with error")
    # waiting time: the first two wrong inputs are free (already one above), the third starts a wait
    ul_submit(c, "falsches-passwort-2")
    c.check("2nd wrong input: still free, no waiting time", ul(c)["msg"] == WRONG_PW_DE and not ul(c)["btnDisabled"], ul(c)["msg"])
    ul_submit(c, "falsches-passwort-3")
    u = ul(c)
    c.check("3rd wrong input: right away the text says how long to wait (5 s), button disabled (the page asks GET /api/crypt for the wait)",
            u["btnDisabled"] and u["msg"].startswith(WRONG_PW_DE + " Nächster Versuch in ") and 3 <= secs(u["msg"]) <= 5, u["msg"])
    n_crypt = len([1 for m, x in c.requests if m == "GET" and x.split("?")[0].endswith("/api/crypt")])
    c.check("after each wrong drive password the page asked GET /api/crypt (3 times so far)", n_crypt >= 3, n_crypt)
    c.shot("unlock_wait")
    if not u["btnDisabled"]:
        ul_submit(c, "falsches-passwort-4")                  # 4th input inside the wait: the device answers 429
    else:
        force_429(c)
    u = ul(c)
    sent = n_post(c, "/api/crypt/unlock")
    c.check("429 LOCKED: 'Zu viele falsche Eingaben' text with countdown and the rule for all devices (4 requests so far)", LOCK_DE_RE.match(u["msg"]) and u["btnDisabled"] and sent == 4, f"{sent} {u['msg']}")
    s0 = secs(u["msg"])
    # switch to the recovery key (waiting text and disabled button survive the switch)
    c.tap("#ulMode")
    u = ul(c)
    c.check("recovery key mode: label and placeholder, text field, switch-back link, no eye, no help paragraph (it is on the help page); waiting text stays",
            u["lbl"] == "Wiederherstellungsschlüssel" and c.ev("() => document.querySelector('#ulPass').placeholder") == "Wiederherstellungsschlüssel" and u["ftype"] == "text"
            and u["mode"] == "Stattdessen das Laufwerks-Passwort verwenden" and not u["eye"] and not c.ev("() => !!document.querySelector('#unlock p.small')")
            and u["msg"].startswith("Zu viele falsche Eingaben.") and u["btnDisabled"], u)
    c.shot("unlock_recovery_key")
    c.overflow_check("unlock view, recovery key")
    c.shot("unlock_locked_out")
    c.overflow_check("unlock view with the waiting text")
    if s0 >= 2:
        ok = c.wait("n => { const m = /(\d+) Sekunden?/.exec(document.querySelector('#ulMsg').textContent); return !!m && +m[1] < n; }", s0, 5000)
        c.check("waiting time: the countdown ticks down", ok, ul(c)["msg"])
    c.ev("() => { ulUntil = Date.now() + 60000; ulTick(); }")
    c.ev("() => document.querySelector('#ulForm').requestSubmit()")
    c.page.wait_for_timeout(300)
    c.check("while waiting, submitting does not send a request", n_post(c, "/api/crypt/unlock") == sent, n_post(c, "/api/crypt/unlock"))
    clear_ul_lock(c)
    u = ul(c)
    c.check("waiting time over: button enabled, text gone", not u["btnDisabled"] and u["msg"] == "", u)
    c.tap("#ulBtn")
    c.check("empty recovery key: hint", ul(c)["msg"] == "Bitte den Wiederherstellungsschlüssel eingeben.", ul(c)["msg"])
    ul_submit(c, "AAAA-BBBB-CCCC")
    c.check("wrong recovery key: its own text", ul(c)["msg"] == "Der Wiederherstellungsschlüssel ist falsch." and ul(c)["unlock"], ul(c)["msg"])
    c.tap("#ulMode")
    u = ul(c)
    c.check("back to the drive password: field emptied, password type", u["lbl"] == "Laufwerks-Passwort" and u["ftype"] == "password" and u["fval"] == "" and u["eye"], u)
    c.tap("#ulMode")
    # success with a sloppily typed recovery key (lower case, spaces): the mock gives the drive to the USB device
    c.clear_toasts()
    ul_submit(c, DEF_KEY.lower().replace("-", " ", 2), wait=False)
    ok = c.wait_toast("Das Laufwerk ist entsperrt.") and wait_unlock_view(c, False)
    c.check("unlocked with the recovery key: the toast tells how to set a new drive password", any("als bisheriges Passwort den Wiederherstellungsschlüssel eingeben" in x for x in c.toast_texts()), c.toast_texts())
    c.wait("() => S.st.crypt.unlocked && S.st.drive.owner === 'usb'")
    u = ul(c)
    st = c.mock.state()
    c.check("unlock with the recovery key: toast, unlock view gone, drive belongs to the USB device (host connected)",
            ok and st["owner"] == "usb" and st["crypt"]["unlocked"] and c.ui()["title"] == T_USB and u["stateH"] == "Die Dateien gehören gerade dem USB-Gerät"
            and not u["unlock"], f"{st['owner']} {u['modeTitle']} {u['stateH']}")
    c.check("after unlocking: lock button and 'verschlüsselt' tag are there, form reset to the password", u["lockBtn"] and u["encTag"] and u["lbl"] == "Laufwerks-Passwort"
            and u["fval"] == "" and u["stoSub"] != "Größe erst nach dem Entsperren sichtbar", u)
    c.shot("unlocked_usb_view")
    c.overflow_check("unlocked, USB view")
    c.no_console_errors()
    # password unlock, no USB host: file management takes the drive and the list appears
    c.start("crypt_locked", usb_connected=0, crypt_kdf_ms=50)
    u = ul(c)
    c.check("locked without a USB host: status line 'Kein USB-Gerät angeschlossen', no ⓘ", u["modeTitle"] == "Kein USB-Gerät angeschlossen" and u["modeSub"] == ""
            and not vis(c, "#modeInfo"), u["modeTitle"])
    c.page.fill("#ulPass", DEF_PW)
    if c.phone:
        c.tap("#ulBtn")
    else:
        c.page.press("#ulPass", "Enter")
    ok = wait_unlock_view(c, False) and c.wait("() => S.st.drive.owner === 'app' && S.entries !== null && document.querySelectorAll('#list .row').length > 0")
    u, ui = ul(c), c.ui()
    c.check("unlock with the drive password (Enter / tap): toast, file list is back, toolbar there, owner app",
            ok and c.wait_toast("Das Laufwerk ist entsperrt.") and ui["title"] == T_APP and ui["upBtn"] and len(ui["rows"]) > 0 and c.mock.state()["owner"] == "app", f"{ui['title']} {len(ui['rows'])}")
    c.check("unlocked in file mode: encryption card 'Entsperrt' with its one action 'Sperren'", u["lockBtn"] and u["encTag"] and u["encSub"] == "Entsperrt"
            and c.text("#lockBtn").strip() == "Sperren", u)
    c.shot("unlocked_files")
    c.overflow_check("unlocked, file list")
    c.no_console_errors()


@test
def t09b_locked_during_use(c: Ctx):
    c.start("crypt_enabled", crypt_kdf_ms=50)
    resp = []
    c.page.on("response", lambda r: resp.append((r.url, r.status)))
    u, ui = ul(c), c.ui()
    c.check("encrypted and unlocked: normal file list, lock button, 'verschlüsselt' tag, unlock view hidden",
            not u["unlock"] and ui["title"] == T_APP and len(ui["rows"]) > 0 and u["lockBtn"] and u["encTag"], f"{ui['title']} {len(ui['rows'])}")
    # 423 on a file request: back to the unlock view
    c.clear_toasts()
    c.mock.set(crypt="locked")
    c.ev("() => loadFiles()")
    ok = wait_unlock_view(c)
    u = ul(c)
    c.check("423 DRIVE_LOCKED on a file request: unlock view, list gone, toast", ok and not u["list"] and c.wait_toast(T_LOCKED_DONE) and u["encSub"] == "Gesperrt", c.toast_texts())
    c.check("the 423 really came from /api/files (or the status poll saw the lock first)", any("/api/files" in x and s == 423 for x, s in resp) or c.ev("() => S.st.crypt.unlocked") is False, resp[-3:])
    c.shot("locked_during_use")
    c.overflow_check("locked during use")
    ul_submit(c, DEF_PW, wait=False)
    ok = wait_unlock_view(c, False) and c.wait("() => S.entries !== null || S.st.drive.owner !== 'app'")
    c.check("unlock again: the view that was open is back", ok and not ul(c)["unlock"], ul(c)["msg"])
    c.wait("() => S.st.drive.owner === 'usb' || S.entries !== null")
    # a dialog is open when the drive gets locked and the request in it answers 423
    c.mock.set(owner="app")
    c.ev("() => refresh(true)")
    c.wait("() => S.st.drive.owner === 'app' && S.entries !== null")
    c.new_folder()
    c.wait_dlg("Neuer Ordner")
    c.clear_toasts()
    c.mock.set(crypt="locked")
    c.ev("() => { document.querySelector('#dF0').value = 'Zu spät'; document.querySelector('#dForm').requestSubmit(); }")
    ok = wait_unlock_view(c) and c.wait_dlg_closed()
    c.check("dialog open while the drive gets locked: dialog closed, unlock view, toast", ok and c.wait_toast(T_LOCKED_DONE) and not c.ev("() => document.querySelector('#dlg').open"), c.toast_texts())
    # a selection is dropped
    ul_submit(c, DEF_PW, wait=False)
    wait_unlock_view(c, False)
    c.mock.set(owner="app")
    c.ev("() => refresh(true)")
    c.wait("() => S.st.drive.owner === 'app' && S.entries !== null && S.entries.length > 0")
    first = c.ev("() => S.entries[0].name")
    c.select_rows(first)
    c.mock.set(crypt="locked")
    c.ev("() => refresh()")
    ok = wait_unlock_view(c)
    c.check("locked while items are selected: selection and bar are gone", ok and not c.ui()["selbar"] and c.ev("() => S.sel.size") == 0, c.ui()["selbar"])
    # upload while locked: refused with the locked text
    c.page.set_input_files("#picker", [str(c.run.f["txt"])])
    c.wait("() => S.ups.length >= 1 && !S.working", None, 10000)
    c.check("upload against a locked drive: error in the panel, nothing stored", "gesperrt" in c.text("#upList") and c.run.f["txt"].name not in [f["path"] for f in c.mock.state()["files"]], c.text("#upList"))
    c.tap("#upClose")
    # settings stay reachable while locked
    to_settings(c)
    c.check("settings while locked: state text, 'Entsperren' link, no lock button, format disabled with reason",
            c.text("#cState") == "Ein, gesperrt" and "cUnlock" in crypt_btns(c) and "cLock" not in crypt_btns(c) and c.ev("() => document.querySelector('#fmtBtn').disabled")
            and "Erst nach dem Entsperren möglich." in c.text("#fmtSub"), f"{c.text('#cState')} {crypt_btns(c)}")
    c.shot("settings_locked")
    c.overflow_check("settings, locked drive")
    c.tap("#cUnlock")
    ok = wait_unlock_view(c)
    c.check("'Entsperren' in the settings leads to the unlock view", ok and c.ui()["hash"] in ("#/", "") and c.ui()["files"], c.ui()["hash"])
    c.no_console_errors()


@test
def t09c_lock_now(c: Ctx):
    c.start("crypt_enabled", crypt_kdf_ms=50)
    c.tap("#lockBtn")
    ok = c.wait_dlg("Laufwerk jetzt sperren?")
    d = c.dlg()
    c.check("lock now: confirmation with consequences, 'Sperren' (no danger colour needed), 'Abbrechen'", ok and d["yes"] == "Sperren" and d["no"] == "Abbrechen"
            and "Ein angeschlossener Drucker oder PC verliert das Laufwerk sofort" in d["body"] and not d["fields"], d)
    c.shot("lock_dialog")
    c.overflow_check("lock dialog")
    c.dlg_no()
    c.wait_dlg_closed()
    c.check("cancel: still unlocked", c.mock.state()["crypt"]["unlocked"] and not ul(c)["unlock"])
    c.tap("#lockBtn")
    c.wait_dlg("Laufwerk jetzt sperren?")
    c.page.keyboard.press("Escape")
    c.check("Escape cancels the lock dialog", c.wait_dlg_closed() and c.mock.state()["crypt"]["unlocked"])
    # host writing / another upload: refused inside the dialog
    c.mock.set(owner="usb", host_writing="hold")
    c.ev("() => refresh()")
    c.wait("() => S.st.drive.owner === 'usb'")
    c.tap("#lockBtn")
    c.wait_dlg("Laufwerk jetzt sperren?")
    c.dlg_yes()
    ok = c.wait_derr()
    c.check("host writing: HOST_WRITING text in the dialog, dialog stays, drive still unlocked", ok and "schreibt gerade" in c.dlg()["err"] and c.dlg()["open"]
            and c.mock.state()["crypt"]["unlocked"], c.dlg()["err"])
    c.mock.set(host_writing=0, other_upload=1)
    c.dlg_yes()
    ok = c.wait_derr()
    c.check("another upload running: BUSY text in the dialog", ok and "Dateiübertragung" in c.dlg()["err"] and c.mock.state()["crypt"]["unlocked"], c.dlg()["err"])
    c.mock.set(other_upload=0)
    c.clear_toasts()
    c.dlg_yes()
    ok = c.wait_dlg_closed() and wait_unlock_view(c)
    st = c.mock.state()
    c.check("confirmed while the USB device uses the drive: toast, unlock view, the drive is locked and the device sees no medium",
            ok and c.wait_toast("Das Laufwerk ist gesperrt.") and st["crypt"]["enabled"] and not st["crypt"]["unlocked"] and st["owner"] == "none", f"{st['owner']} {c.toast_texts()}")
    c.check("no 'locked elsewhere' toast after locking here", not any(T_LOCKED_DONE in x for x in c.toast_texts()), c.toast_texts())
    c.shot("locked_now")
    # the same from the settings
    c.start("crypt_enabled", crypt_kdf_ms=50)
    to_settings(c)
    c.tap("#cLock")
    c.wait_dlg("Laufwerk jetzt sperren?")
    c.dlg_yes()
    ok = c.wait_dlg_closed() and c.wait("() => document.querySelector('#cState').textContent === 'Ein, gesperrt'")
    c.check("lock from the settings: section shows 'gesperrt' and offers 'Entsperren'", ok and crypt_btns(c) == ["cUnlock", "cOff"], crypt_btns(c))
    c.no_console_errors()


@test
def t09d_crypt_setup(c: Ctx):
    c.start("usb_active", crypt_kdf_ms=600)
    to_settings(c)
    c.check("unencrypted: section says 'Aus' and offers only 'Einrichten'", c.text("#cState") == "Aus" and crypt_btns(c) == ["cSetup"], f"{c.text('#cState')[:30]} {crypt_btns(c)}")
    c.shot("settings_crypt_off")
    c.overflow_check("settings, encryption off")
    # the USB device uses the drive: take-over question first
    c.tap("#cSetup")
    ok = c.wait_dlg(T_TAKEOVER)
    c.dlg_no()
    c.wait_dlg_closed()
    c.page.wait_for_timeout(300)
    c.check("set up while the host uses the drive: take-over question; 'Abbrechen' ends it (no setup dialog, still USB)", ok and not c.mock.state()["crypt"]["enabled"]
            and c.mock.state()["owner"] == "usb" and not c.ev("() => document.querySelector('#dlg').open"))
    c.tap("#cSetup")
    c.wait_dlg(T_TAKEOVER)
    c.dlg_yes()
    ok = c.wait_dlg("Laufwerk verschlüsseln?")
    d = c.dlg()
    r = c.ev(JS_UNTRANSLATED)
    c.check("setup dialog: warnings, three fields (password, repeat, confirmation word), danger button", ok and d["yes"] == "Löschen und verschlüsseln" and "danger" in d["yesCls"]
            and [f["type"] for f in d["fields"]] == ["password", "password", "text"] and d["fields"][0]["label"] == "Laufwerks-Passwort (mindestens 12 Zeichen)"
            and d["fields"][1]["label"] == "Laufwerks-Passwort wiederholen" and d["fields"][2]["label"] == "Zur Bestätigung LÖSCHEN eintippen"
            and "Alle Dateien auf" in d["body"] and "endgültig verloren" in d["body"] and d["focus"] == "dF0" and not r["bad"]
            and "nur noch in diesem Stick lesbar" in c.ev("() => document.querySelector('#dInfo').dataset.x"), f"{d['fields']} {r['bad']}")
    c.shot("crypt_setup_dialog")
    c.overflow_check("setup dialog")
    c.ev("() => document.querySelector('#dlg').scrollTo(0, 99999)")
    c.overflow_check("setup dialog, scrolled")
    for a, b, w, msg in (("kurz", "kurz", "LÖSCHEN", "Das Laufwerks-Passwort muss mindestens 12 Zeichen lang sein."),
                         ("elf-zeichen", "elf-zeichen", "LÖSCHEN", "Das Laufwerks-Passwort muss mindestens 12 Zeichen lang sein."),
                         ("ü" * 33, "ü" * 33, "LÖSCHEN", "höchstens 64 Bytes"),
                         (DEF_PW, "Anderes-Passwort-12", "LÖSCHEN", "Die beiden Laufwerks-Passwörter stimmen nicht überein."),
                         (DEF_PW, DEF_PW, "", "Bitte genau das Wort LÖSCHEN eingeben."),
                         (DEF_PW, DEF_PW, "ERASE", "Bitte genau das Wort LÖSCHEN eingeben.")):
        c.fill_dlg(a, b, w)
        c.dlg_yes()
        c.wait_derr()
        c.check(f"setup, input {a[:12]!r}/{b[:6]!r}/{w!r}: '{msg[:40]}'", msg in c.dlg()["err"] and c.dlg()["open"], c.dlg()["err"][:80])
    c.check("no setup request was sent by the failed inputs", n_post(c, "/api/crypt/setup") == 0 and not c.mock.state()["crypt"]["enabled"])
    c.shot("crypt_setup_error")
    # cancel: the drive goes back to the USB device
    c.clear_toasts()
    c.dlg_no()
    ok = c.wait_dlg_closed() and c.wait_toast("Das USB-Laufwerk ist bereitgestellt.")
    c.check("cancel after the take-over: drive handed back to the USB device, nothing encrypted", ok and c.mock.state()["owner"] == "usb" and not c.mock.state()["crypt"]["enabled"], c.toast_texts())
    # the real setup (the word may be typed in lower case)
    c.mock.set(owner="app")
    c.ev("() => refresh(true)")
    c.wait("() => S.st.drive.owner === 'app'")
    c.tap("#cSetup")
    c.wait_dlg("Laufwerk verschlüsseln?")
    c.fill_dlg(NEW_PW, NEW_PW, "löschen")
    c.dlg_yes()
    ok = c.wait("() => /Wird eingerichtet/.test(document.querySelector('#dYes').textContent) && document.querySelector('#dYes').disabled && document.querySelector('#dNo').disabled", None, 4000)
    prog = c.ev("() => { const p = document.querySelector('#dProg'); return p.getClientRects().length > 0 ? document.querySelector('#dProgX').textContent : ''; }")
    c.check("setup running: button 'Wird eingerichtet …' with both buttons disabled, progress text says it takes up to minutes", ok and "löscht jetzt den gesamten Speicher" in prog and "Minuten" in prog, f"{c.dlg()['yes']} | {prog[:60]}")
    c.shot("crypt_setup_running")
    ok = c.wait_dlg("Wiederherstellungsschlüssel aufschreiben", timeout=15000)
    d = c.dlg()
    key = c.text("#rkey")
    r = c.ev(JS_UNTRANSLATED)
    no_hidden = c.ev("() => document.querySelector('#dNo').hidden")
    st = c.mock.state()
    c.check("setup done: recovery key dialog appears; mock is encrypted and unlocked, files gone", ok and st["crypt"]["enabled"] and st["crypt"]["unlocked"] and st["files"] == [], f"{ok} {st['crypt']}")
    c.check("recovery key dialog: key in the valid format (8 x 4), one field 'Ich habe den Schlüssel aufgeschrieben', 'Fertig' disabled, no cancel button",
            KEY_RE.match(key) and d["yes"] == "Fertig" and d["yesDisabled"] and no_hidden and [f["type"] for f in d["fields"]] == ["checkbox"]
            and key_ok_label(c) == "Ich habe den Schlüssel aufgeschrieben" and "nur dieses eine Mal" in d["body"] and not r["bad"], f"{key} {d['yes']} {d['fields']} {r['bad']}")
    c.shot("crypt_recovery_key")
    c.overflow_check("recovery key dialog")
    r2 = c.ev("() => { const b = document.querySelector('#rkey').getBoundingClientRect(), v = document.querySelector('#dlg').getBoundingClientRect(); return b.left >= v.left && b.right <= v.right; }")
    c.check("the key lines fit inside the dialog", r2)
    # not closable until the box is ticked
    c.page.keyboard.press("Escape")
    c.page.wait_for_timeout(200)
    c.check("Escape does not close the key dialog", c.dlg()["open"])
    c.ev("() => document.querySelector('#dlg').click()")
    c.page.wait_for_timeout(200)
    c.check("a click on the backdrop does not close it", c.dlg()["open"])
    c.ev("() => document.querySelector('#dForm').requestSubmit()")
    ok = dlg_wait_err(c)
    c.check("submitting without the tick: 'Bitte bestätige …' and stays open", ok and c.dlg()["err"] == "Bitte bestätige, dass du den Schlüssel aufgeschrieben hast." and c.dlg()["open"], c.dlg()["err"])
    c.clear_toasts()
    c.tap("#rkCopy")
    ok = c.wait("() => window.__toasts.some(t => t.text === 'Kopiert.' || t.text.startsWith('Kopieren geht hier nicht'))", None, 4000)
    c.check("copy button: answers with a toast ('Kopiert.' or the hint)", ok, c.toast_texts())
    c.fill_dlg(True)
    c.check("ticked: 'Fertig' enabled", not c.dlg()["yesDisabled"])
    c.fill_dlg(False)
    c.check("untick: disabled again", c.dlg()["yesDisabled"])
    c.fill_dlg(True)
    c.clear_toasts()
    c.dlg_yes()
    ok = c.wait_dlg_closed() and c.wait_toast("Das Laufwerk ist jetzt verschlüsselt und entsperrt.")
    c.wait("() => document.querySelector('#cState').textContent.startsWith('Ein, entsperrt')")
    c.check("key confirmed: dialog closes, toast, section shows 'Ein, entsperrt' with lock / change password / turn off", ok and crypt_btns(c) == ["cLock", "cPw", "cRec", "cOff"]
            and c.text("#cState") == "Ein, entsperrt", f"{crypt_btns(c)} {c.text('#cState')}")
    c.shot("settings_crypt_on")
    c.overflow_check("settings, encryption on")
    c.go("#/")
    c.wait("() => S.st.drive.owner === 'app' && S.entries !== null")
    c.check("after the setup the drive is empty and shows the tag", c.ui()["stateH"] == "Noch keine Dateien" and ul(c)["encTag"] and ul(c)["lockBtn"], c.ui()["stateH"])
    # end to end: lock, then open with the key that was shown
    c.tap("#lockBtn")
    c.wait_dlg("Laufwerk jetzt sperren?")
    c.dlg_yes()
    wait_unlock_view(c)
    c.tap("#ulMode")
    ul_submit(c, key, wait=False)
    ok = wait_unlock_view(c, False) and c.wait_toast("Das Laufwerk ist entsperrt.")
    c.check("the key from the dialog opens the locked drive", ok and c.mock.state()["crypt"]["unlocked"], ul(c)["msg"])
    c.no_console_errors()


@test
def t09e_crypt_manage(c: Ctx):
    c.start("crypt_enabled", crypt_kdf_ms=50)
    to_settings(c)
    c.check("encrypted and unlocked: lock, change password, new recovery key, turn off", crypt_btns(c) == ["cLock", "cPw", "cRec", "cOff"], crypt_btns(c))
    c.tap("#cPw")
    ok = c.wait_dlg("Laufwerks-Passwort ändern")
    d = c.dlg()
    r = c.ev(JS_UNTRANSLATED)
    c.check("change drive password dialog: current, new (12+), repeat; reassuring text; 'Speichern'", ok and [f["type"] for f in d["fields"]] == ["password"] * 3
            and d["fields"][0]["label"] == "Bisheriges Laufwerks-Passwort oder Wiederherstellungsschlüssel" and d["fields"][1]["label"] == "Neues Laufwerks-Passwort (mindestens 12 Zeichen)"
            and not d["body"].strip() and "Wiederherstellungsschlüssel bleibt gültig" in c.ev("() => document.querySelector('#dInfo').dataset.x") and vis(c, "#dInfo")
            and d["yes"] == "Speichern" and not r["bad"], f"{d['fields']} {r['bad']}")
    c.shot("crypt_pw_dialog")
    c.overflow_check("change drive password dialog")
    for o, a, b, msg in (("", NEW_PW, NEW_PW, "Bitte das Laufwerks-Passwort eingeben."),
                         (DEF_PW, "kurz", "kurz", "mindestens 12 Zeichen"),
                         (DEF_PW, NEW_PW, "Anderes-Laufwerk-Pw-9", "stimmen nicht überein"),
                         (DEF_PW, DEF_PW, DEF_PW, "muss sich vom bisherigen unterscheiden"),
                         ("falsches-passwort-xx", NEW_PW, NEW_PW, "Das bisherige Laufwerks-Passwort ist falsch.")):
        c.fill_dlg(o, a, b)
        c.dlg_yes()
        c.wait_derr()
        c.check(f"change drive password, input {o[:8]!r}/{a[:8]!r}: '{msg}'", msg in c.dlg()["err"] and c.dlg()["open"], c.dlg()["err"][:80])
    c.shot("crypt_pw_error")
    c.clear_toasts()
    c.fill_dlg(DEF_PW, NEW_PW, NEW_PW)
    c.dlg_yes()
    ok = c.wait_dlg_closed() and c.wait_toast("Das Laufwerks-Passwort wurde geändert.")
    c.check("changed: toast, drive stays unlocked and encrypted", ok and c.mock.state()["crypt"]["enabled"] and c.mock.state()["crypt"]["unlocked"] and crypt_btns(c) == ["cLock", "cPw", "cRec", "cOff"], c.toast_texts())
    # prove it: lock, old password fails, new one opens
    c.tap("#cLock")
    c.wait_dlg("Laufwerk jetzt sperren?")
    c.dlg_yes()
    c.wait_dlg_closed()
    c.tap("#cUnlock")
    wait_unlock_view(c)
    ul_submit(c, DEF_PW)
    c.check("after the change the old drive password is refused", ul(c)["msg"] == "Das Laufwerks-Passwort ist falsch.", ul(c)["msg"])
    ul_submit(c, NEW_PW, wait=False)
    ok = wait_unlock_view(c, False) and c.wait_toast("Das Laufwerk ist entsperrt.")
    c.check("the new drive password unlocks", ok and c.mock.state()["crypt"]["unlocked"], ul(c)["msg"])
    # forgotten password: the recovery key serves as the 'current' password, then a new recovery key
    c.start("crypt_enabled", crypt_kdf_ms=50)
    to_settings(c)
    c.tap("#cPw")
    c.wait_dlg("Laufwerks-Passwort ändern")
    c.check("change dialog: its ⓘ explains the forgotten-password route (recovery key in the first field)",
            "Passwort vergessen? Dann ins erste Feld den Wiederherstellungsschlüssel eintragen." in c.ev("() => document.querySelector('#dInfo').dataset.x"))
    c.fill_dlg(DEF_KEY, NEW_PW, NEW_PW)
    c.dlg_yes()
    ok = c.wait_dlg_closed() and c.wait_toast("Das Laufwerks-Passwort wurde geändert.")
    c.check("the recovery key is accepted as the current password", ok, c.toast_texts())
    c.tap("#cRec")
    ok = c.wait_dlg("Neuen Wiederherstellungsschlüssel erzeugen?")
    d = c.dlg()
    r = c.ev(JS_UNTRANSLATED)
    c.check("new recovery key dialog: warns that the old key stops working, one field, 'Erzeugen'", ok and d["yes"] == "Erzeugen" and [f["type"] for f in d["fields"]] == ["password"]
            and d["fields"][0]["label"] == "Laufwerks-Passwort oder aktueller Wiederherstellungsschlüssel" and "funktioniert danach nicht mehr" in d["body"] and not r["bad"], f"{d['fields']} {r['bad']}")
    c.shot("crypt_rec_dialog")
    c.overflow_check("new recovery key dialog")
    c.dlg_yes()
    c.wait_derr()
    c.check("new recovery key without a password: hint", c.dlg()["err"] == "Bitte das Laufwerks-Passwort eingeben.", c.dlg()["err"])
    c.fill_dlg("falsches-passwort-xx")
    c.dlg_yes()
    c.wait_derr()
    c.check("new recovery key with a wrong password: its error", c.dlg()["err"] == "Das Laufwerks-Passwort ist falsch." and c.dlg()["open"], c.dlg()["err"])
    c.fill_dlg(NEW_PW)
    c.dlg_yes()
    ok = c.wait_dlg("Wiederherstellungsschlüssel aufschreiben", timeout=15000)
    key2 = c.text("#rkey")
    c.check("the new key is shown in the same one-time dialog, with the note that the old one is void", ok and KEY_RE.match(key2) and key2 != DEF_KEY and "funktioniert ab sofort nicht mehr" in c.dlg()["body"]
            and c.dlg()["yesDisabled"] and c.ev("() => document.querySelector('#dNo').hidden"), f"{key2} {c.dlg()['body'][-80:]}")
    c.shot("crypt_rec_key")
    c.overflow_check("new recovery key shown")
    c.fill_dlg(True)
    c.clear_toasts()
    c.dlg_yes()
    ok = c.wait_dlg_closed() and c.wait_toast("Der neue Wiederherstellungsschlüssel gilt.")
    c.check("new key confirmed: toast", ok, c.toast_texts())
    c.tap("#cLock")
    c.wait_dlg("Laufwerk jetzt sperren?")
    c.dlg_yes()
    c.wait_dlg_closed()
    c.tap("#cUnlock")
    wait_unlock_view(c)
    c.tap("#ulMode")
    ul_submit(c, DEF_KEY)
    c.check("the old recovery key no longer opens the drive", ul(c)["msg"] == "Der Wiederherstellungsschlüssel ist falsch.", ul(c)["msg"])
    ul_submit(c, key2, wait=False)
    ok = wait_unlock_view(c, False) and c.wait_toast("Das Laufwerk ist entsperrt.")
    c.check("the new recovery key opens it", ok and c.mock.state()["crypt"]["unlocked"], ul(c)["msg"])
    # a locked drive offers no password change / new key (they need an unlocked drive)
    c.start("crypt_locked", crypt_kdf_ms=50)
    to_settings(c)
    c.check("locked: only unlock and turn off are offered, format disabled", crypt_btns(c) == ["cUnlock", "cOff"] and c.ev("() => document.querySelector('#fmtBtn').disabled"), crypt_btns(c))
    # turn off while locked: no change of owner needed
    c.tap("#cOff")
    ok = c.wait_dlg("Verschlüsselung ausschalten?")
    c.check("turn off on a locked drive: straight to the dialog (no take-over question)", ok and not any(T_TAKEOVER in x["title"] for x in c.ev("() => window.__dialogs")))
    c.fill_dlg("nope-nope-nope-1", "LÖSCHEN")
    c.dlg_yes()
    c.wait_derr()
    c.check("turn off with a wrong drive password: its error, stays open", c.dlg()["err"] == "Das Laufwerks-Passwort ist falsch." and c.dlg()["open"], c.dlg()["err"])
    c.fill_dlg(DEF_PW, "LÖSCHEN")
    c.dlg_yes()
    ok = c.wait_dlg_closed() and c.wait_toast("Die Verschlüsselung ist aus. Das Laufwerk ist leer.")
    st = c.mock.state()
    c.check("turned off on a locked drive: unencrypted, owner app, empty", ok and not st["crypt"]["enabled"] and st["owner"] == "app" and st["files"] == [], f"{st['crypt']} {st['owner']}")
    # turn off an unlocked drive
    c.start("crypt_enabled", crypt_kdf_ms=50)
    to_settings(c)
    c.tap("#cOff")
    ok = c.wait_dlg("Verschlüsselung ausschalten?")
    d = c.dlg()
    r = c.ev(JS_UNTRANSLATED)
    c.check("turn off dialog: warning, drive password + confirmation word, danger button", ok and d["yes"] == "Löschen und ausschalten" and "danger" in d["yesCls"]
            and [f["type"] for f in d["fields"]] == ["password", "text"] and d["fields"][0]["label"] == "Laufwerks-Passwort" and d["fields"][1]["label"] == "Zur Bestätigung LÖSCHEN eintippen"
            and "Alle Dateien auf" in d["body"] and not r["bad"], f"{d['fields']} {r['bad']}")
    c.shot("crypt_off_dialog")
    c.overflow_check("turn off dialog")
    for p_, w, msg in (("", "LÖSCHEN", "Bitte das Laufwerks-Passwort eingeben."), (DEF_PW, "falsch", "Bitte genau das Wort LÖSCHEN eingeben."),
                       ("nope-nope-nope-1", "LÖSCHEN", "Das Laufwerks-Passwort ist falsch.")):
        c.fill_dlg(p_, w)
        c.dlg_yes()
        c.wait_derr()
        c.check(f"turn off, input {p_[:8]!r}/{w!r}: '{msg}'", c.dlg()["err"] == msg and c.dlg()["open"] and c.mock.state()["crypt"]["enabled"], c.dlg()["err"])
    c.fill_dlg(DEF_PW, "löschen")
    c.dlg_yes()
    ok = c.wait_dlg_closed() and c.wait_toast("Die Verschlüsselung ist aus. Das Laufwerk ist leer.")
    c.wait("() => document.querySelector('#cState').textContent.startsWith('Aus')")
    st = c.mock.state()
    c.check("turned off: unencrypted, drive empty, section offers 'Einrichten', tag and lock button gone", ok and not st["crypt"]["enabled"] and st["files"] == [] and crypt_btns(c) == ["cSetup"]
            and not ul(c)["encTag"] and not ul(c)["lockBtn"], f"{st['crypt']} {crypt_btns(c)}")
    c.no_console_errors()


@test
def t09f_crypt_role_user(c: Ctx):
    c.start("two_users", who=GUEST)
    u = ul(c)
    c.check("role user, unencrypted drive: card 'Unverschlüsselt', no lock button, no setup action", not u["lockBtn"] and not u["encTag"] and not u["encSetup"]
            and u["encTagTxt"] == "Unverschlüsselt")
    to_settings(c)
    c.check("role user, unencrypted: the section is read-only (no setup, no buttons)", crypt_btns(c) == [] and c.text("#cState") == "Aus", crypt_btns(c))
    r = c.ev("async () => { const o = {method: 'POST', credentials: 'same-origin', headers: {'X-PrintDrive': '1', 'Content-Type': 'application/x-www-form-urlencoded'}};"
             "const out = []; for (const [p, b] of [['/api/crypt/setup', 'password=Geheimes-Passwort-12'], ['/api/crypt/password', 'old=a&new=b'], ['/api/crypt/disable', 'password=x']]) {"
             "const x = await fetch(p, Object.assign({body: b}, o)); out.push([x.status, (await x.json()).code]); } return out; }")
    c.check("API: a user gets 403 FORBIDDEN_ROLE for setup, password change and disable", r == [[403, "FORBIDDEN_ROLE"]] * 3, r)
    c.check("API: a user may read /api/crypt", c.ev("async () => { const x = await fetch('/api/crypt', {credentials: 'same-origin'}); const j = await x.json(); return x.status === 200 && j.ok && j.enabled === false; }"))
    c.shot("settings_crypt_user_off")
    c.overflow_check("settings, encryption off, user")
    # encrypted: user may lock and unlock, sees no setup / change / turn off
    c.start("crypt_enabled", who=GUEST, crypt_kdf_ms=50)
    to_settings(c)
    c.check("role user, encrypted: only 'Laufwerk sperren' (no change password, no turn off)", crypt_btns(c) == ["cLock"], crypt_btns(c))
    c.shot("settings_crypt_user_on")
    c.overflow_check("settings, encryption on, user")
    c.go("#/")
    c.wait("() => S.entries !== null")
    c.tap("#lockBtn")
    c.wait_dlg("Laufwerk jetzt sperren?")
    c.dlg_yes()
    ok = c.wait_dlg_closed() and wait_unlock_view(c)
    c.check("role user can lock the drive", ok and not c.mock.state()["crypt"]["unlocked"])
    to_settings(c)
    c.check("role user, locked: only 'Entsperren'", crypt_btns(c) == ["cUnlock"], crypt_btns(c))
    c.tap("#cUnlock")
    wait_unlock_view(c)
    ul_submit(c, "falsch-falsch-falsch")
    c.check("role user: wrong drive password text", ul(c)["msg"] == "Das Laufwerks-Passwort ist falsch.", ul(c)["msg"])
    ul_submit(c, DEF_PW, wait=False)
    ok = wait_unlock_view(c, False)
    c.check("role user can unlock the drive", ok and c.mock.state()["crypt"]["unlocked"])
    c.no_console_errors()


@test
def t09g_wifi_scan(c: Ctx):
    c.start("two_users", wifi_count=1, scan_ms=1200)
    c.go("#einstellungen")
    c.wait("() => !document.querySelector('#vSettings').hidden")
    c.wait("() => document.querySelector('#wifiList .li') !== null")
    open_card(c, "net")
    c.ev("() => document.querySelector('#s-net').scrollIntoView()")
    c.tap("#wifiAdd")
    ok = c.wait_dlg("WLAN hinzufügen")
    s = c.ev(JS_SCAN)
    d = c.dlg()
    c.check("scan dialog while searching: spinner text, 'Erneut suchen' disabled, polling paused",
            ok and s["spin"] and s["msg"] == "Suche nach WLANs …" and s["againDisabled"] and s["scanning"], s)
    c.check("dialog still has name and password fields and the hint for hidden networks", [f["type"] for f in d["fields"]] == ["text", "password"]
            and "WLANs in Reichweite" in d["body"] and "Erneut suchen" in d["body"] and "Versteckte WLANs von Hand eintragen" in c.ev("() => document.querySelector('#dInfo').dataset.x"), d["body"][:120])
    c.shot("wifi_scan_loading")
    c.overflow_check("wifi dialog, searching")
    ok = c.wait("() => document.querySelectorAll('#scanList .net').length > 0", None, 15000)
    s = c.ev(JS_SCAN)
    names = [n["name"] for n in s["nets"]]
    c.check("scan finished: list of 20 networks (the mock has more), strongest first, search button usable, polling resumes",
            ok and len(names) == 20 and names[0] == "MeinWLAN" and not s["againDisabled"] and not s["scanning"], f"{len(names)} {names[:3]}")
    literal = ['Café "Zur Linde"', "Müller's Größe-WLAN", 'Gäste-Netz "Zimmer 4"', "FRITZ!Box 7590 KL", "Büro Gast"]
    c.check("SSIDs with quotes, apostrophes, umlauts and '!' are shown literally (text and tooltip)", all(x in names for x in literal) and all(n["title"] == n["name"] for n in s["nets"])
            and s["foreign"] == 0, [x for x in literal if x not in names])
    mein = next(n for n in s["nets"] if n["name"] == "MeinWLAN")
    cafe = next(n for n in s["nets"] if n["name"] == 'Café "Zur Linde"')
    buero = next(n for n in s["nets"] if n["name"] == "Büro Gast")
    c.check("saved network carries the 'gespeichert' tag, others do not", mein["pill"] == "gespeichert" and cafe["pill"] == "" and buero["pill"] == "" and sum(1 for n in s["nets"] if n["pill"]) == 1,
            [n["pill"] for n in s["nets"]][:5])
    c.check("secured networks show a lock icon, open ones do not", mein["svgs"] == 2 and buero["svgs"] == 2 and cafe["svgs"] == 1, f"{mein['svgs']} {buero['svgs']} {cafe['svgs']}")
    c.check("screen reader labels name signal, security and saved state", "verschlüsselt" in mein["aria"] and "gespeichert" in mein["aria"] and "Empfang" in mein["aria"] and "offen" in cafe["aria"], f"{mein['aria']} | {cafe['aria']}")
    r = c.ev(JS_UNTRANSLATED)
    c.check("scan dialog (German): no foreign or raw key texts", not r["bad"], r["bad"])
    c.check("20 entries scroll inside the list, the dialog stays inside the viewport", s["scrollable"] and c.ev("() => { const r = document.querySelector('#dlg').getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight + 1; }"))
    c.shot("wifi_scan_list")
    c.overflow_check("wifi dialog with the scan list")
    if c.phone:
        res = c.ev(JS_TARGETS, ["#scanList .net", "#scanAgain"])
        c.check("touch targets: network row at least 40 px high, 'Erneut suchen' at least 36 px high", res[0]["h"] >= 40 and res[1]["h"] >= 36, res)
    # secured network: name taken over, focus in the password field, password required
    c.tap(c.page.locator("#scanList .net", has_text="Büro Gast"))
    d = c.dlg()
    ph = c.ev("() => document.querySelector('#dF1').placeholder")
    s = c.ev(JS_SCAN)
    c.check("pick a secured network: name filled in, row marked, focus in the password field", d["fields"][0]["value"] == "Büro Gast" and d["focus"] == "dF1" and ph == "" and sum(1 for n in s["nets"] if n["on"]) == 1, f"{d['fields'][0]['value']} {d['focus']}")
    c.dlg_yes()
    c.wait_derr()
    c.check("secured network without a password: 'bitte das Passwort eingeben'", c.dlg()["err"] == "Dieses WLAN ist verschlüsselt – bitte das Passwort eingeben." and c.dlg()["open"], c.dlg()["err"])
    c.clear_toasts()
    c.fill_dlg("Büro Gast", "geheim1234")
    c.dlg_yes()
    ok = c.wait_dlg_closed() and c.wait_toast("„Büro Gast“ gespeichert.")
    c.check("secured network saved with the password; list and mock updated", ok and any(n["ssid"] == "Büro Gast" for n in c.mock.api("/api/wifi/list")["networks"])
            and "Büro Gast" in c.text("#wifiList"), c.toast_texts())
    # open network with quotes: hint instead of a password, focus on Save
    c.tap("#wifiAdd")
    c.wait_dlg("WLAN hinzufügen")
    c.wait("() => document.querySelectorAll('#scanList .net').length > 0", None, 15000)
    s = c.ev(JS_SCAN)
    c.check("second search: networks saved meanwhile are tagged", next(n for n in s["nets"] if n["name"] == "Büro Gast")["pill"] == "gespeichert", [n["pill"] for n in s["nets"]][:4])
    c.page.fill("#dF1", "xyz12345")
    c.tap(c.page.locator("#scanList .net", has_text="Zur Linde"))
    d = c.dlg()
    ph = c.ev("() => document.querySelector('#dF1').placeholder")
    c.check("pick an open network: quotes kept in the field, typed password cleared, placeholder 'Offenes WLAN – kein Passwort nötig', focus on 'Speichern'",
            d["fields"][0]["value"] == 'Café "Zur Linde"' and d["fields"][1]["value"] == "" and ph == "Offenes WLAN – kein Passwort nötig" and d["focus"] == "dYes", f"{d['fields']} {ph} {d['focus']}")
    c.shot("wifi_scan_open_picked")
    c.dlg_yes()
    ok = c.wait_dlg_closed() and c.wait_toast("„Café \"Zur Linde\"“ gespeichert.")
    c.check("open network saved without a password; the mock keeps the quotes", ok and any(n["ssid"] == 'Café "Zur Linde"' for n in c.mock.api("/api/wifi/list")["networks"]), c.toast_texts())
    # typing over a picked name clears the marking; manual entry of a secured name still needs the password
    c.tap("#wifiAdd")
    c.wait_dlg("WLAN hinzufügen")
    c.wait("() => document.querySelectorAll('#scanList .net').length > 0", None, 15000)
    c.tap(c.page.locator("#scanList .net", has_text="MeinWLAN"))
    c.check("pick the saved network: selected", c.ev(JS_SCAN)["nets"][0]["on"] and c.dlg()["fields"][0]["value"] == "MeinWLAN")
    c.page.fill("#dF0", "Werkstatt")
    s = c.ev(JS_SCAN)
    c.check("typing a name by hand clears the row marking", not any(n["on"] for n in s["nets"]))
    c.dlg_yes()
    c.wait_derr()
    c.check("manual 'Werkstatt' (secured in range) without password: needs the password", c.dlg()["err"] == "Dieses WLAN ist verschlüsselt – bitte das Passwort eingeben.", c.dlg()["err"])
    # search again, BUSY, empty-ish states
    c.tap("#scanAgain")
    s = c.ev(JS_SCAN)
    c.check("'Erneut suchen': loading state again, button disabled", s["spin"] and s["againDisabled"] and len(s["nets"]) == 0, s)
    c.wait("() => document.querySelectorAll('#scanList .net').length > 0", None, 15000)
    c.check("search again finished: list is back, button usable", len(c.ev(JS_SCAN)["nets"]) == 20 and not c.ev(JS_SCAN)["againDisabled"])
    c.mock.set(other_upload=1)
    c.tap("#scanAgain")
    c.wait("() => !!document.querySelector('#scanList .scanmsg') && !document.querySelector('#scanList .spin')", None, 15000)
    s = c.ev(JS_SCAN)
    c.check("file transfer running (BUSY): explanation instead of the list, button usable, dialog stays", s["msg"] == "Gerade läuft eine Dateiübertragung – die Suche ist erst danach möglich."
            and not s["againDisabled"] and not s["nets"] and c.dlg()["open"], s["msg"])
    c.shot("wifi_scan_busy")
    c.overflow_check("wifi dialog, BUSY")
    c.mock.set(other_upload=0)
    c.tap("#scanAgain")
    ok = c.wait("() => document.querySelectorAll('#scanList .net').length > 0", None, 15000)
    c.check("after the transfer the search works again", ok)
    # manual entry
    c.clear_toasts()
    c.fill_dlg("Handnetz ü", "geheim1234")
    c.dlg_yes()
    ok = c.wait_dlg_closed() and c.wait_toast("„Handnetz ü“ gespeichert.")
    c.check("manual entry of a network that is not in range still works", ok and any(n["ssid"] == "Handnetz ü" for n in c.mock.api("/api/wifi/list")["networks"]), c.toast_texts())
    # closing during a search, and typing before the list arrives
    c.mock.set(wifi_count=1)
    c.go("#/")
    c.go("#einstellungen")
    c.wait("() => document.querySelectorAll('#wifiList .li').length === 1")
    open_card(c, "net")
    c.tap("#wifiAdd")
    c.wait_dlg("WLAN hinzufügen")
    c.dlg_no()
    c.wait_dlg_closed()
    ok = c.wait("() => !S.scanning", None, 15000)
    c.check("closing the dialog during the search: the page stops waiting (polling resumes), no error", ok and not c.ev("() => document.querySelector('#dlg').open"))
    c.tap("#wifiAdd")
    c.wait_dlg("WLAN hinzufügen")
    c.clear_toasts()
    c.fill_dlg("Sofort ü", "geheim1234")
    c.dlg_yes()
    ok = c.wait_dlg_closed() and c.wait_toast("„Sofort ü“ gespeichert.", 15000)
    c.check("saving while the search is still running works (the device answers after the search)", ok and any(n["ssid"] == "Sofort ü" for n in c.mock.api("/api/wifi/list")["networks"]), c.toast_texts())
    c.no_console_errors()


@test
def t09h_lockout_and_unavailable(c: Ctx):
    # sign-in lock-out: new wording with the countdown
    c.reset("two_users", login_locked=1)
    p = c.open()
    p.fill("#lUser", "admin")
    p.fill("#lPass", ADMIN[1])
    c.tap("#loginBtn")
    ok = c.wait("() => document.querySelector('#loginBtn').disabled && document.querySelector('#loginMsg').textContent !== ''")
    msg = c.text("#loginMsg")
    s0 = secs(msg)
    c.check("sign-in lock-out wording: 'Zu viele Fehlversuche. Bitte warte noch N Sekunden. Jede weitere Sperre dauert länger.'", ok and LOGIN_LOCK_DE_RE.match(msg) and 25 <= s0 <= 30, msg)
    ok = c.wait("n => { const m = /(\\d+) Sekunden/.exec(document.querySelector('#loginMsg').textContent); return !!m && +m[1] < n; }", s0, 5000)
    c.check("sign-in lock-out: countdown ticks down", ok, c.text("#loginMsg"))
    c.tap("#loginForm [data-lang]")
    ok = c.wait("() => /^Too many failed attempts/.test(document.querySelector('#loginMsg').textContent)", None, 4000)
    c.check("sign-in lock-out in English", ok and LOGIN_LOCK_EN_RE.match(c.text("#loginMsg")), c.text("#loginMsg"))
    c.shot("login_locked_en")
    c.overflow_check("sign-in locked, English")
    # 503 AUTH_UNAVAILABLE on the sign-in page
    c.reset("two_users", auth_unavailable=1)
    p = c.open()
    cls = c.ev("() => document.querySelector('#loginNote').className")
    c.check("AUTH_UNAVAILABLE at first visit: sign-in view with the full notice (warning style, not the grey info style)", c.ui()["auth"] == "login" and login_note(c) == AUTH_DE and "info" not in cls and c.ui()["login"], login_note(c)[:80])
    c.shot("login_unavailable")
    c.overflow_check("sign-in with AUTH_UNAVAILABLE")
    p.fill("#lUser", "admin")
    p.fill("#lPass", ADMIN[1])
    c.tap("#loginBtn")
    c.wait("() => !document.querySelector('#loginBtn').disabled")
    c.page.wait_for_timeout(500)
    c.check("signing in meanwhile: still the same notice, no extra error line, button usable", login_note(c) == AUTH_DE and c.text("#loginMsg") == "" and c.ui()["auth"] == "login", c.text("#loginMsg"))
    c.tap("#loginForm [data-lang]")
    c.wait("() => lang === 'en'")
    c.check("notice in English", login_note(c) == AUTH_EN, login_note(c)[:80])
    c.tap("#loginForm [data-lang]")
    c.wait("() => lang === 'de'")
    c.mock.set(auth_unavailable=0)
    ok = c.wait("() => document.querySelector('#loginNote').hidden", None, 12000)
    c.check("database readable again: the notice disappears by itself (retry every few seconds)", ok, login_note(c)[:60])
    c.tap("#loginBtn")
    ok = c.wait_ready()
    c.check("then signing in works", ok and c.ui()["app"])
    # becomes unavailable while signed in
    c.mock.set(auth_unavailable=1)
    c.ev("() => refresh()")
    ok = c.wait("() => S.auth === 'login'")
    c.check("AUTH_UNAVAILABLE while signed in: back to the sign-in view with the notice", ok and login_note(c) == AUTH_DE and not c.ui()["app"], login_note(c)[:60])
    c.mock.set(auth_unavailable=0)
    c.no_console_errors()


@test
def t09i_v4_english_and_layout(c: Ctx):
    c.reset("crypt_locked", crypt_kdf_ms=50)
    c.open(locale="en-GB")
    c.login(ADMIN)
    u = ul(c)
    r = c.ev(JS_UNTRANSLATED)
    c.check("English unlock view: texts, status strip and storage line", u["h2"] == "Drive locked" and u["lbl"] == "Drive password" and u["btn"] == "Unlock" and u["mode"] == "Use the recovery key instead"
            and u["modeTitle"] == "USB device sees no drive" and u["modeSub"] == "" and u["stoSub"] == "" and u["stoTip"] == "Size visible after unlocking"
            and u["encTagTxt"] == "Encrypted" and u["encSub"] == "Locked" and "not your sign-in password" in u["encInfo"], u)
    c.check("English unlock view: no German leftovers or raw keys", not r["bad"] and r["n"] > 6, r)
    c.shot("unlock_en")
    c.overflow_check("unlock view in English")
    c.tap("#ulBtn")
    c.check("English: empty hint", ul(c)["msg"] == "Please enter the drive password.", ul(c)["msg"])
    ul_submit(c, "falsches-passwort-1")
    c.check("English: wrong password", ul(c)["msg"] == "The drive password is wrong.", ul(c)["msg"])
    c.tap("#ulMode")
    c.tap("#ulBtn")
    c.check("English: recovery key mode and empty hint", ul(c)["lbl"] == "Recovery key" and ul(c)["msg"] == "Please enter the recovery key." and ul(c)["mode"] == "Use the drive password instead", ul(c))
    ul_submit(c, "AAAA-BBBB")
    c.check("English: wrong recovery key", ul(c)["msg"] == "The recovery key is wrong.", ul(c)["msg"])
    c.tap("#ulMode")
    lock_out_via_ui(c)
    force_429(c)
    c.check("English lock-out wording in the unlock view", LOCK_EN_RE.match(ul(c)["msg"]) and ul(c)["btnDisabled"], ul(c)["msg"])
    r = c.ev(JS_UNTRANSLATED)
    c.check("English locked-out view: no German leftovers", not r["bad"], r["bad"])
    c.shot("unlock_locked_out_en")
    clear_ul_lock(c)
    c.clear_toasts()
    ul_submit(c, DEF_PW, wait=False)
    ok = wait_unlock_view(c, False) and c.wait_toast("The drive is unlocked.")
    c.check("English: unlocked toast", ok, c.toast_texts())
    c.mock.set(owner="app")
    c.ev("() => refresh(true)")
    c.wait("() => S.st.drive.owner === 'app' && S.entries !== null")
    c.tap("#lockBtn")
    ok = c.wait_dlg("Lock the drive now?")
    r = c.ev(JS_UNTRANSLATED)
    c.check("English lock dialog: texts, no foreign ones", ok and c.dlg()["yes"] == "Lock" and "A connected printer or computer loses the drive at once" in c.dlg()["body"] and not r["bad"], r["bad"])
    c.shot("lock_dialog_en")
    c.dlg_no()
    c.wait_dlg_closed()
    # settings in English
    to_settings(c)
    r = c.ev(JS_UNTRANSLATED)
    c.check("English encryption section: texts and buttons", c.text("#cState") == "On, unlocked" and crypt_btns(c) == ["cLock", "cPw", "cRec", "cOff"]
            and c.text("#cLock") == "Lock drive" and c.text("#cPw") == "Change drive password …" and c.text("#cOff") == "Turn off …" and not r["bad"], f"{c.text('#cState')[:40]} {r['bad']}")
    c.shot("settings_crypt_en")
    c.tap("#cPw")
    ok = c.wait_dlg("Change drive password")
    r = c.ev(JS_UNTRANSLATED)
    c.check("English change-password dialog: no foreign texts", ok and not r["bad"] and c.dlg()["fields"][1]["label"] == "New drive password (at least 12 characters)", r["bad"])
    c.fill_dlg("falsches-passwort-xx", NEW_PW, NEW_PW)
    c.dlg_yes()
    c.wait_derr()
    c.check("English: current drive password wrong", c.dlg()["err"] == "The current drive password is wrong.", c.dlg()["err"])
    c.fill_dlg(DEF_PW, DEF_PW, DEF_PW)
    c.dlg_yes()
    c.wait_derr()
    c.check("English: new equals current", c.dlg()["err"] == "The new drive password must be different from the current one.", c.dlg()["err"])
    c.dlg_no()
    c.wait_dlg_closed()
    c.tap("#cOff")
    ok = c.wait_dlg("Turn off encryption?")
    r = c.ev(JS_UNTRANSLATED)
    c.check("English turn-off dialog: no foreign texts, word ERASE", ok and not r["bad"] and c.dlg()["fields"][1]["label"] == "Type ERASE to confirm" and c.dlg()["yes"] == "Erase and turn off", r["bad"])
    c.fill_dlg(DEF_PW, "LÖSCHEN")
    c.dlg_yes()
    c.wait_derr()
    c.check("English: wrong confirmation word", c.dlg()["err"] == "Please type exactly ERASE.", c.dlg()["err"])
    c.fill_dlg(DEF_PW, "erase")
    c.dlg_yes()
    ok = c.wait_dlg_closed() and c.wait_toast("Encryption is off. The drive is empty.")
    c.check("English: turned off", ok, c.toast_texts())
    c.mock.set(owner="app")
    c.ev("() => refresh(true)")
    c.wait("() => document.querySelector('#cSetup').getClientRects().length > 0")
    c.tap("#cSetup")
    ok = c.wait_dlg("Encrypt the drive?")
    r = c.ev(JS_UNTRANSLATED)
    c.check("English setup dialog: no foreign texts, labels", ok and not r["bad"] and c.dlg()["fields"][0]["label"] == "Drive password (at least 12 characters)" and c.dlg()["yes"] == "Erase and encrypt", r["bad"])
    c.shot("crypt_setup_dialog_en")
    for a, b, w, msg in (("kurz", "kurz", "ERASE", "The drive password must be at least 12 characters long."), ("ü" * 33, "ü" * 33, "ERASE", "at most 64 bytes"),
                         (DEF_PW, "Anderes-Passwort-12", "ERASE", "The two drive passwords do not match."), (DEF_PW, DEF_PW, "LÖSCHEN", "Please type exactly ERASE.")):
        c.fill_dlg(a, b, w)
        c.dlg_yes()
        c.wait_derr()
        c.check(f"English setup, input {a[:8]!r}/{w!r}: '{msg}'", msg in c.dlg()["err"], c.dlg()["err"])
    c.fill_dlg(NEW_PW, NEW_PW, "erase")
    c.dlg_yes()
    ok = c.wait_dlg("Write down the recovery key", timeout=15000)
    r = c.ev(JS_UNTRANSLATED)
    c.check("English recovery key dialog: no foreign texts, 'Done'", ok and not r["bad"] and c.dlg()["yes"] == "Done" and key_ok_label(c) == "I have written the key down", r["bad"])
    c.shot("crypt_recovery_key_en")
    c.overflow_check("recovery key dialog, English")
    c.ev("() => document.querySelector('#dForm').requestSubmit()")
    dlg_wait_err(c)
    c.check("English: tick hint", c.dlg()["err"] == "Please confirm that you have written the key down.", c.dlg()["err"])
    c.fill_dlg(True)
    c.dlg_yes()
    ok = c.wait_dlg_closed() and c.wait_toast("The drive is now encrypted and unlocked.")
    c.check("English: setup finished", ok, c.toast_texts())
    # Wi-Fi scan in English
    c.mock.set(wifi_count=1, scan_ms=700)
    c.go("#/")
    c.go("#einstellungen")
    c.wait("() => document.querySelectorAll('#wifiList .li').length === 1")
    open_card(c, "net")
    c.tap("#wifiAdd")
    ok = c.wait_dlg("Add network")
    s = c.ev(JS_SCAN)
    c.check("English scan dialog while searching", ok and s["msg"] == "Searching for networks …" and "Networks in range" in c.dlg()["body"] and "Search again" in c.dlg()["body"], s["msg"])
    c.wait("() => document.querySelectorAll('#scanList .net').length > 0", None, 15000)
    s = c.ev(JS_SCAN)
    r = c.ev(JS_UNTRANSLATED)
    mein = next(n for n in s["nets"] if n["name"] == "MeinWLAN")
    c.check("English scan list: 'saved' tag, labels in English, no German leftovers", mein["pill"] == "saved" and "excellent signal, secured, saved" in mein["aria"] and not r["bad"], f"{mein} {r['bad']}")
    c.shot("wifi_scan_list_en")
    c.tap(c.page.locator("#scanList .net", has_text="Werkstatt"))
    c.dlg_yes()
    c.wait_derr()
    c.check("English: secured network needs the password", c.dlg()["err"] == "This network is secured — please enter its password.", c.dlg()["err"])
    c.mock.set(other_upload=1)
    c.tap("#scanAgain")
    c.wait("() => !!document.querySelector('#scanList .scanmsg') && !document.querySelector('#scanList .spin')", None, 15000)
    c.check("English BUSY text", c.ev(JS_SCAN)["msg"] == "A file transfer is in progress — searching is possible once it has finished.", c.ev(JS_SCAN)["msg"])
    c.mock.set(other_upload=0)
    c.dlg_no()
    c.wait_dlg_closed()
    c.no_console_errors()
    # narrow windows: unlock view, encryption section, scan dialog
    for vw, vh in ((320, 568), (360, 740)) if c.phone else ((900, 700), (721, 700)):
        c.reset("crypt_locked", crypt_kdf_ms=50, scan_ms=300)
        c.open("", viewport={"width": vw, "height": vh})
        c.login(ADMIN)
        c.overflow_check(f"{vw} px wide: unlock view")
        ul_submit(c, "falsch-falsch-falsch")
        c.overflow_check(f"{vw} px wide: unlock view with error")
        c.tap("#ulMode")
        c.overflow_check(f"{vw} px wide: unlock view, recovery key")
        c.shot(f"w{vw}_unlock")
        to_settings(c)
        c.overflow_check(f"{vw} px wide: settings, locked drive")
        c.reset("crypt_enabled", crypt_kdf_ms=50, scan_ms=300)
        c.open("", viewport={"width": vw, "height": vh})
        c.login(ADMIN)
        c.overflow_check(f"{vw} px wide: encrypted file list with lock button")
        to_settings(c)
        c.overflow_check(f"{vw} px wide: settings, encryption on")
        c.tap("#cOff")
        c.wait_dlg("Verschlüsselung ausschalten?")
        c.overflow_check(f"{vw} px wide: turn off dialog")
        c.dlg_no()
        c.wait_dlg_closed()
        open_card(c, "net")
        c.ev("() => document.querySelector('#s-net').scrollIntoView()")
        c.tap("#wifiAdd")
        c.wait_dlg("WLAN hinzufügen")
        c.wait("() => document.querySelectorAll('#scanList .net').length > 0", None, 15000)
        c.overflow_check(f"{vw} px wide: wifi dialog with scan list")
        c.shot(f"w{vw}_wifi_scan")
        c.dlg_no()
        c.wait_dlg_closed()
    c.no_console_errors()


@test
def t09j_crypt_erase_damaged(c: Ctx):
    # password and recovery key lost: an administrator erases the drive from the unlock view
    c.start("crypt_locked", crypt_kdf_ms=50, crypt_setup_ms=900)
    c.tap("#ulLost")
    ok = c.wait_dlg("Laufwerk löschen und neu formatieren?")
    d = c.dlg()
    r = c.ev(JS_UNTRANSLATED)
    c.check("'Passwort und Wiederherstellungsschlüssel verloren?': erase dialog with the consequences, confirmation word field, danger button",
            ok and d["yes"] == "Endgültig löschen" and "danger" in d["yesCls"] and len(d["fields"]) == 1 and d["fields"][0]["label"] == "Zur Bestätigung LÖSCHEN eintippen"
            and "endgültig verloren" in d["body"] and "Ohne Laufwerks-Passwort und Wiederherstellungsschlüssel" in c.ev("() => document.querySelector('#dInfo').dataset.x")
            and not r["bad"], f"{d['fields']} {r['bad']}")
    c.shot("crypt_erase_dialog")
    c.overflow_check("erase dialog")
    for w in ("", "ERASE"):
        c.fill_dlg(w)
        c.dlg_yes()
        c.wait_derr()
        c.check(f"erase, word {w!r}: 'Bitte genau das Wort LÖSCHEN eingeben.'", c.dlg()["err"] == "Bitte genau das Wort LÖSCHEN eingeben." and c.dlg()["open"], c.dlg()["err"])
    c.dlg_no()
    c.wait_dlg_closed()
    c.check("cancel: nothing erased", c.mock.state()["crypt"]["enabled"] and not c.mock.state()["crypt"]["unlocked"] and c.mock.state()["files"] != [])
    c.tap("#ulLost")
    c.wait_dlg("Laufwerk löschen und neu formatieren?")
    c.fill_dlg("löschen")
    c.clear_toasts()
    c.dlg_yes()
    ok = c.wait("() => { const p = document.querySelector('#dProg'); return p.getClientRects().length > 0 && /gelöscht und neu eingerichtet/.test(document.querySelector('#dProgX').textContent); }", None, 4000)
    c.check("erasing: progress text instead of the form (can take minutes)", ok, c.dlg()["open"])
    ok = c.wait_dlg_closed(15000) and c.wait_toast("Das Laufwerk ist gelöscht und neu formatiert.")
    c.wait("() => S.st.crypt.enabled === false")
    st = c.mock.state()
    c.check("erased: unencrypted, empty, owner app; unlock view gone, no tag", ok and not st["crypt"]["enabled"] and st["files"] == [] and st["owner"] == "app"
            and not ul(c)["unlock"] and not ul(c)["encTag"] and c.ui()["stateH"] == "Noch keine Dateien", f"{st['crypt']} {st['owner']} {c.ui()['stateH']}")
    c.no_console_errors()
    # a user cannot erase
    c.start("crypt_locked", who=GUEST, crypt_kdf_ms=50)
    c.check("role user: no 'Passwort und Wiederherstellungsschlüssel verloren?' link", not ul(c)["lost"], ul(c)["lost"])
    # damaged key area
    c.start("crypt_damaged")
    dm = c.ev("""() => { const q = s => document.querySelector(s), vis = e => !!e && e.getClientRects().length > 0;
      return {dmg: vis(q('#damaged')), unlock: vis(q('#unlock')), h2: q('#damaged h2').textContent.trim(), who: vis(q('#dmgWho')), btn: vis(q('#dmgErase')) ? q('#dmgErase').textContent : null,
        title: q('#modeTitle').textContent, sub: q('#modeInfo').dataset.x || '', sto: q('#stoSub').textContent, tip: q('#stoCell').title, enc: q('#encSub').textContent,
        info: vis(q('#damaged .ib')), lock: vis(q('#lockBtn'))}; }""")
    r = c.ev(JS_UNTRANSLATED)
    c.check("damaged key area: its own view (no unlock form), what happened, what is left (admin), status strip and storage line",
            dm["dmg"] and not dm["unlock"] and dm["h2"] == "Schlüsselbereich des Laufwerks beschädigt" and not dm["who"] and dm["info"]
            and dm["btn"] == "Laufwerk löschen und neu formatieren" and "beschädigt" not in dm["title"] and dm["sub"] == "" and dm["enc"] == "Schlüsselbereich beschädigt"
            and dm["sto"] == "" and dm["tip"] == "Dateien nicht lesbar" and not dm["lock"] and not r["bad"], f"{dm} {r['bad']}")
    c.shot("crypt_damaged")
    c.overflow_check("damaged view")
    to_settings(c)
    c.check("damaged, settings: state text, only 'Löschen und neu formatieren', format disabled with the pointer",
            c.text("#cState") == "Ein, Schlüsselbereich beschädigt" and crypt_btns(c) == ["cErase"]
            and c.ev("() => document.querySelector('#fmtBtn').disabled") and "Bei beschädigtem Schlüsselbereich" in c.ev("() => document.querySelector('#fmtI').dataset.x"), f"{c.text('#cState')} {crypt_btns(c)}")
    c.shot("settings_crypt_damaged")
    c.overflow_check("settings, damaged")
    c.tap("#cErase")
    ok = c.wait_dlg("Laufwerk löschen und neu formatieren?")
    c.check("erase from the settings (damaged): wording for a damaged key area", ok and "Der Schlüsselbereich ist beschädigt, die Dateien sind nicht mehr lesbar." in c.ev("() => document.querySelector('#dInfo').dataset.x"), c.dlg()["body"][:80])
    c.fill_dlg("LÖSCHEN")
    c.dlg_yes()
    ok = c.wait_dlg_closed(15000) and c.wait_toast("Das Laufwerk ist gelöscht und neu formatiert.")
    c.wait("() => S.st.crypt.enabled === false")
    c.check("damaged drive erased: unencrypted again, the setup is offered", ok and not c.mock.state()["crypt"]["enabled"] and c.wait("() => document.querySelector('#cSetup').getClientRects().length > 0"), crypt_btns(c))
    # a user sees the damaged view without the erase button
    c.start("crypt_damaged", who=GUEST)
    dm = c.ev("() => ({who: document.querySelector('#dmgWho').textContent, btn: document.querySelector('#dmgErase').getClientRects().length > 0})")
    c.check("damaged, role user: text 'Ein Administrator kann …', no erase button", dm["who"].startswith("Ein Administrator kann das Laufwerk löschen") and not dm["btn"], dm)
    to_settings(c)
    c.check("damaged, role user: no buttons in the settings", crypt_btns(c) == [], crypt_btns(c))
    c.no_console_errors()


# ================================================================== 10. API v5 (docs/15_api_v5.md, version 0.5.0)

TZ_DE = "CET-1CEST,M3.5.0,M10.5.0/3"
HTTPS_FIELDS = ("https", "https_active", "https_restart", "cert_sha256", "ca_sha256", "cert_expires")
ANDROID_UA = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0.0.0 Mobile Safari/537.36")
T_LOST_R = "Verbindung unterbrochen. Der Upload lässt sich fortsetzen."
IST = timezone(timedelta(hours=5, minutes=30))          # Asia/Kolkata: no daylight saving, half-hour offset


def vis(c: Ctx, sel: str) -> bool:
    return c.ev("s => { const e = document.querySelector(s); return !!e && e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden'; }", sel)


def url_path(u: str) -> str:
    return urllib.parse.urlsplit(u).path


def url_q(u: str) -> dict:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(u).query, keep_blank_values=True))


def reqs(c: Ctx, method: str, path: str) -> list:
    """Query parameters of every request of that method to exactly that path (in order)."""
    return [url_q(u) for m, u in c.requests if m == method and url_path(u) == path]


def wait_req(c: Ctx, method: str, path: str, n=1, timeout=8.0) -> bool:
    end = time.time() + timeout
    while time.time() < end and len(reqs(c, method, path)) < n:
        c.page.wait_for_timeout(100)
    return len(reqs(c, method, path)) >= n


def route_status(c: Ctx, drop):
    """The mock always reports `webdav` and the HTTPS fields; to see the UI of a firmware without them the answer of
    /api/status is rewritten inside the browser (the mock itself is not changed)."""
    def handler(route):
        try:
            resp = route.fetch()
            j = resp.json()
            for k in drop:
                j.pop(k, None)
            route.fulfill(response=resp, json=j)
        except Exception:
            try:
                route.abort()
            except Exception:
                pass
    c.page.route(lambda u: url_path(u) == "/api/status", handler)


def to_sec(c: Ctx, sel: str, cond: str = ""):
    """Settings view, the card holding the element opened and scrolled to it (optionally after waiting for a condition)."""
    c.go("#einstellungen")
    c.wait("() => !document.querySelector('#vSettings').hidden")
    if cond:
        c.wait(cond, None, 9000)
    c.ev("""s => { const e = document.querySelector(s); if (!e) return; const sec = e.closest('.sec');
      if (sec) { const h = sec.querySelector('.sechead'); if (h.getAttribute('aria-expanded') !== 'true') h.click(); }
      e.scrollIntoView(); }""", sel)


def clock_is(c: Ctx, tz: str, suffix: str, timeout=9000) -> bool:
    """#sysTime shows the device time (de-DE, medium date, short time) in zone tz ('' = the browser's) plus the source."""
    return c.wait("([tz, suf]) => { const s = S.st && S.st.time; if (!s) return false;"
                  " const f = s.epoch > 1e9 ? new Intl.DateTimeFormat('de-DE', {dateStyle: 'medium', timeStyle: 'short', timeZone: tz || undefined}).format(s.epoch * 1000) : '–';"
                  " return document.querySelector('#sysTime').textContent === f + ' · ' + suf; }", [tz, suffix], timeout)


def up_done(c: Ctx, n=1, timeout=30000) -> bool:
    return c.wait("n => S.ups.length === n && !S.working && S.ups.every(u => u.state === 'done')", n, timeout)


def close_panel(c: Ctx):
    if vis(c, "#upClose"):
        c.tap("#upClose")
    c.wait("() => document.querySelector('#uploads').hidden", None, 9000)


def offsets(c: Ctx, path: str) -> list:
    return [int(q.get("offset", -1)) for q in reqs(c, "POST", "/api/upload") if q.get("path") == path]


def fdate_ist(mtime: int) -> str:
    """What the list shows for a time stamp in a browser running in Asia/Kolkata (German texts)."""
    if not mtime:
        return "–"
    d, now = datetime.fromtimestamp(mtime, IST), datetime.now(IST)
    diff = (now.date() - d.date()).days
    return f"Heute, {d:%H:%M}" if diff == 0 else f"Gestern, {d:%H:%M}" if diff == 1 else f"{d:%d.%m.%Y}"


def menu_items(c: Ctx, name: str) -> list:
    c.open_menu(name)
    items = c.menu()["items"]
    c.page.keyboard.press("Escape")
    return items


@test
def t10a_clock_and_time_zone(c: Ctx):
    c.start("files", wifi_count=1)
    to_sec(c, "#timeItem", "() => !document.querySelector('#timeItem').hidden")
    c.check("system section: clock line and time zone choice (admin)", vis(c, "#sysTime") and vis(c, "#tzSel") and c.text("#timeItem h3").strip() == "Zeitzone"
            and c.text("#timeItem .kv span") == "Uhrzeit des Laufwerks", c.text("#timeItem")[:80])
    c.check("clock synced by NTP: date and time in the device's zone (Berlin) and 'aus dem Netz'", clock_is(c, "Europe/Berlin", "aus dem Netz"), c.text("#sysTime"))
    c.check("synced clock: the page does not send its own time", not reqs(c, "POST", "/api/time"), reqs(c, "POST", "/api/time"))
    o = c.ev("() => ({v: document.querySelector('#tzSel').value, n: document.querySelectorAll('#tzSel option').length, first: document.querySelector('#tzSel option').textContent})")
    c.check("time zone list: 15 zones, Germany selected", o["v"] == TZ_DE and o["n"] == 15 and o["first"] == "(UTC+1) Berlin, Paris, Madrid", o)
    c.shot("v5_clock_synced")
    c.overflow_check("settings with clock and time zone")
    c.clear_toasts()
    c.page.select_option("#tzSel", "JST-9")
    ok = c.wait_toast("Gespeichert.")
    sent = reqs(c, "POST", "/api/settings/time")
    c.check("choose Tokyo: POST /api/settings/time?tz=JST-9, toast 'Gespeichert.', the mock stores it",
            ok and sent and sent[-1].get("tz") == "JST-9" and c.mock.state()["time"]["tz"] == "JST-9", f"{sent} {c.toast_texts()}")
    c.check("the clock line follows the new zone (Tokyo)", clock_is(c, "Asia/Tokyo", "aus dem Netz"), c.text("#sysTime"))
    # a zone outside the list (set by a script; '+' and ',' must arrive encoded)
    other = "<+0330>-3:30"
    r = c.mock.post("/api/settings/time?tz=" + urllib.parse.quote(other, safe=""))
    ok = c.wait("v => document.querySelector('#tzSel').value === v", other, 9000)
    lab = c.ev("() => document.querySelector('#tzSel').selectedOptions[0].textContent")
    c.check("a zone outside the list (set by a script) appears as an extra entry with its POSIX text", r.get("ok") and ok and lab == other, f"{r} {lab!r}")
    c.check("unknown zone: the clock line uses the browser's zone", clock_is(c, "", "aus dem Netz"), c.text("#sysTime"))
    c.page.select_option("#tzSel", TZ_DE)
    ok = c.wait("z => S.st.time.tz === z", TZ_DE, 9000)
    c.check("back to Germany through the list", ok and c.mock.state()["time"]["tz"] == TZ_DE, c.mock.state()["time"])
    c.no_console_errors()
    # clock unknown: the page sends the browser's time once after signing in
    c.start("files", clock="unsynced", wifi_count=1)
    ok = wait_req(c, "POST", "/api/time")
    q = reqs(c, "POST", "/api/time")
    ep = int(q[0].get("epoch", "0")) if q else 0
    c.check("clock unknown (epoch 0): after signing in the page sends POST /api/time?epoch= with the browser's time in UTC seconds",
            ok and abs(ep - time.time()) < 120, q)
    to_sec(c, "#timeItem", "() => !document.querySelector('#timeItem').hidden")
    ok = clock_is(c, "Europe/Berlin", "vom Browser gesetzt", 12000)
    c.check("clock set by the browser: time with 'vom Browser gesetzt', mock clock 'manual'", ok and c.mock.state()["clock"] == "manual",
            f"{c.text('#sysTime')} {c.mock.state()['clock']}")
    c.shot("v5_clock_browser")
    c.mock.set(clock="unsynced")
    ok = c.wait("() => document.querySelector('#sysTime').textContent === '– · nicht gestellt'", None, 12000)
    c.check("clock unknown again (no NTP): '– · nicht gestellt'; the page does not send its time a second time",
            ok and len(reqs(c, "POST", "/api/time")) == 1, f"{c.text('#sysTime')} {len(reqs(c, 'POST', '/api/time'))}")
    c.shot("v5_clock_unset")
    c.overflow_check("clock not set")
    # role user: no clock controls, but the browser time is still sent (allowed for role user)
    c.start("two_users", who=GUEST, clock="unsynced")
    ok = wait_req(c, "POST", "/api/time") and c.wait("() => S.st && S.st.time && S.st.time.epoch > 0", None, 9000)
    c.check("role user: POST /api/time is sent and sets the clock", ok and c.mock.state()["clock"] == "manual", c.mock.state()["clock"])
    to_sec(c, "#s-usb")
    c.check("role user: no clock and no time zone controls", not vis(c, "#sysTime") and not vis(c, "#tzSel"))
    c.no_console_errors()


@test
def t10b_dates_and_sort(c: Ctx):
    c.reset("files")
    c.open(timezone_id="Asia/Kolkata")
    c.login(ADMIN)
    tzn = c.ev("() => Intl.DateTimeFormat().resolvedOptions().timeZone")
    c.check("browser runs in Asia/Kolkata (UTC+5:30)", tzn in ("Asia/Kolkata", "Asia/Calcutta"), tzn)
    ls = c.mock.ls("")
    got = c.ev("() => Object.fromEntries([...document.querySelectorAll('#list .row')].map(r => [r.dataset.name, [r.querySelector('.date').textContent, r.querySelector('.meta').textContent]]))")
    bad = []
    for name in ("notizen.txt", "Getriebe_Gehäuse.gcode", "Benchy.3mf", "Würfel.3mf", "KURZNAME.GCO", "Anleitung Drucker.pdf"):
        want = fdate_ist(ls[name]["mtime"])
        if got.get(name, ["?"])[0] != want or not got[name][1].endswith(" · " + want):
            bad.append(f"{name}: {got.get(name)} != {want}")
    c.check("dates are true UTC shown in the browser's zone (UTC+5:30): 'Heute, HH:MM' / 'Gestern, HH:MM' / dd.mm.yyyy, column and meta line", not bad, bad[:3])
    c.check("unknown time (mtime 0, README): '–' in the column, no date in the meta line", got["README"][0] == "–" and " · " not in got["README"][1], got["README"])
    c.check("folders (mtime 0): '–' in the date column, meta line just 'Ordner'", all(got[d][0] == "–" and got[d][1] == "Ordner" for d in ROOT_DIRS), [got[d] for d in ROOT_DIRS])
    c.shot("v5_dates_kolkata")
    # sort by date
    if c.phone:
        c.page.select_option("#sortM", "mtime,-1")
    else:
        c.tap('.cols button[data-sort="mtime"]')
    c.wait("() => S.sort.k === 'mtime' && S.sort.d === -1")
    rows = c.ui()["rows"]
    files = [n for n in rows if n not in ROOT_DIRS]
    want = sorted(files, key=lambda n: -ls[n]["mtime"])
    c.check("sort by date, newest first: folders stay first, then notizen.txt (5 min) ... README (unknown) last",
            set(rows[:3]) == set(ROOT_DIRS) and files == want and files[0] == "notizen.txt" and files[-1] == "README", rows[:6])
    if c.phone:
        c.page.select_option("#sortM", "mtime,1")
    else:
        c.tap('.cols button[data-sort="mtime"]')
    c.wait("() => S.sort.k === 'mtime' && S.sort.d === 1")
    rows = c.ui()["rows"]
    files = [n for n in rows if n not in ROOT_DIRS]
    c.check("sort by date, oldest first: README (unknown) first, then KURZNAME.GCO (400 days)", set(rows[:3]) == set(ROOT_DIRS) and files[:2] == ["README", "KURZNAME.GCO"], rows[:6])
    if not c.phone:
        aria = c.ev("() => document.querySelector('.cols button[data-sort=\"mtime\"]').getAttribute('aria-label')")
        c.check("date column header tells the sort direction", aria == "Geändert, aufsteigend sortiert", aria)
    c.overflow_check("list sorted by date")
    c.no_console_errors()


@test
def t10c_resume(c: Ctx):
    c.start("files", upload_kbps=100)
    f = c.run.f["big"]
    data, size = f.read_bytes(), f.stat().st_size
    c.mock.set(upload_drop_after=100000)
    c.page.set_input_files("#picker", str(f))
    ok1 = c.wait("() => document.querySelector('#upList').textContent.includes('Verbindung unterbrochen – neuer Versuch …')", None, 10000)
    ok2 = c.wait("() => /Wird fortgesetzt ab \\d+ %/.test(document.querySelector('#upList').textContent)", None, 9000)
    c.shot("v5_resume_retry")
    done = up_done(c)
    offs = offsets(c, f.name)
    posts = [q for q in reqs(c, "POST", "/api/upload") if q.get("path") == f.name]
    gets = [q for q in reqs(c, "GET", "/api/upload/resume") if q.get("path") == f.name]
    c.check("dropped connection: 'Verbindung unterbrochen – neuer Versuch …', then 'Wird fortgesetzt ab N %'", ok1 and ok2, c.text("#upList")[:120])
    c.check("automatic retry: saved without user action; POST offset=0, then offset = device's state (whole 4 KiB blocks, <= 100000)",
            done and len(offs) == 2 and offs[0] == 0 and 0 < offs[1] <= 100000 and offs[1] % 4096 == 0, offs)
    c.check("every POST carries size= of the whole file; each attempt asked GET /api/upload/resume?path=&size= first",
            all(q.get("size") == str(size) for q in posts) and len(gets) >= 2 and all(g.get("size") == str(size) for g in gets), f"{posts} {gets}")
    st, body = c.mock.raw("/api/download?path=" + urllib.parse.quote(f.name))
    c.check("the stored file equals the original byte for byte", st == 200 and body == data, f"{st} {len(body)} of {size}")
    mt, want = int(posts[0].get("mtime", "0")) if posts else 0, int(f.stat().st_mtime)
    c.check("upload parameter mtime = lastModified in UTC seconds (no zone shift), the mock lists that time", abs(mt - want) <= 2
            and abs(c.mock.ls("").get(f.name, {}).get("mtime", 0) - want) <= 2, f"{mt} {want}")
    c.check("toast '1 Datei gespeichert.'", c.wait_toast("1 Datei gespeichert."), c.toast_texts())
    close_panel(c)
    # four drops in a row (first by the mock, then the browser aborts): 'Fortsetzen'
    f2 = c.run.f["big2"]
    data2 = f2.read_bytes()
    c.mock.set(upload_drop_after=100000, upload_kbps=0)
    n = [0]

    def drop(route):
        n[0] += 1
        if n[0] == 1:
            route.continue_()
        else:
            route.abort("connectionreset")
    is_up = lambda u: url_path(u) == "/api/upload"
    c.page.route(is_up, drop)
    c.clear_toasts()
    t0 = time.time()
    c.page.set_input_files("#picker", str(f2))
    ok = c.wait("() => !!document.querySelector('#upList [data-goon]')", None, 30000)
    took = time.time() - t0
    li = c.text("#upList")
    c.check("connection lost 4 times (1 try + 3 retries after 1.5 / 3 / 4.5 s): 'Verbindung unterbrochen. Der Upload lässt sich fortsetzen.' and 'Fortsetzen'",
            ok and T_LOST_R in li and c.text("#upList [data-goon]") == "Fortsetzen" and n[0] == 4 and took >= 8.5, f"{n[0]} tries, {took:.1f} s, {li[:100]}")
    c.check("failed: panel head '1 von 1 nicht gespeichert', no 'gespeichert' toast", c.text("#upHead") == "1 von 1 nicht gespeichert"
            and not any("gespeichert." in x for x in c.toast_texts()), f"{c.text('#upHead')} {c.toast_texts()}")
    part = c.mock.state()["resumable"]
    c.check("the device keeps the part that arrived (status upload.resumable)", part and part["path"] == f2.name and part["size"] == len(data2) and part["offset"] > 0, part)
    c.check("while the panel shows the failed upload there is no extra hint above the list", not vis(c, "#resHint"))
    c.shot("v5_resume_goon")
    c.overflow_check("upload panel with 'Fortsetzen'")
    c.page.unroute(is_up)
    c.tap("#upList [data-goon]")
    done = up_done(c)
    offs = offsets(c, f2.name)
    st, body = c.mock.raw("/api/download?path=" + urllib.parse.quote(f2.name))
    c.check("'Fortsetzen': continues at the device's offset, saved, content complete", done and part and offs[-1] == part["offset"] and st == 200 and body == data2,
            f"{offs} {st} {len(body)}")
    c.check("after 'Fortsetzen': toast '1 Datei gespeichert.'", c.wait_toast("1 Datei gespeichert."), c.toast_texts())
    close_panel(c)
    # RESUME_MISMATCH: the page believes 0, the device holds 8192 bytes
    f3 = c.run.f["big3"]
    data3 = f3.read_bytes()
    c.mock.set(resumable=f"{f3.name}|{len(data3)}|8192")
    is_res = lambda u: url_path(u) == "/api/upload/resume"
    c.page.route(is_res, lambda route: route.fulfill(status=200, content_type="application/json", body='{"ok":true,"offset":0}'))
    c.page.set_input_files("#picker", str(f3))
    done = up_done(c)
    c.page.unroute(is_res)
    offs = offsets(c, f3.name)
    st, body = c.mock.raw("/api/download?path=" + urllib.parse.quote(f3.name))
    c.check("RESUME_MISMATCH (409 with the device's offset 8192): the page continues there and the file is saved",
            done and offs == [0, 8192] and st == 200 and len(body) == len(data3) and body[8192:] == data3[8192:], f"{offs} {st} {len(body)}")
    close_panel(c)
    # the hint for an unfinished upload (in another folder)
    f4 = c.run.f["big4"]
    data4 = f4.read_bytes()
    c.mock.set(resumable=f"Projekte/{f4.name}|{len(data4)}|131072")
    ok = c.wait("() => document.querySelector('#resHint').getClientRects().length > 0", None, 9000)
    txt = c.text("#resHint")
    c.check("unfinished upload in the status: one line with the file and 50 %; how to resume behind its ⓘ; no second 'choose file' button",
            ok and f"Upload von „{f4.name}“ bei 50 % unterbrochen." in txt and not c.ev("() => !!document.querySelector('#resHint button:not(.ib)')")
            and c.ev("() => document.querySelector('#resHint .ib').dataset.i") == "i_res", txt)
    c.shot("v5_resume_hint")
    c.overflow_check("hint for an unfinished upload")
    c.choose_files(str(f4))
    done = up_done(c)
    q = [x for x in reqs(c, "POST", "/api/upload") if x.get("path", "").endswith(f4.name)]
    c.check("the same file chosen in the root goes into the folder of the unfinished upload and continues at 131072",
            done and q and q[0]["path"] == f"Projekte/{f4.name}" and q[0]["offset"] == "131072", q)
    st, body = c.mock.raw("/api/download?path=" + urllib.parse.quote(f"Projekte/{f4.name}"))
    c.check("stored in Projekte (not in the root), the part from byte 131072 on is the file's", f4.name in c.mock.ls("Projekte") and f4.name not in c.mock.ls("")
            and st == 200 and body[131072:] == data4[131072:], f"{st} {len(body)}")
    close_panel(c)
    c.check("hint gone after the upload", c.wait("() => document.querySelector('#resHint').hidden", None, 9000))
    # another upload first: the unfinished one is discarded
    c.mock.set(resumable="Projekte/Weg.3mf|500000|65536")
    c.wait("() => document.querySelector('#resHint').getClientRects().length > 0", None, 9000)
    c.page.set_input_files("#picker", str(c.run.f["txt"]))
    done = up_done(c)
    close_panel(c)
    ok = c.wait("() => document.querySelector('#resHint').hidden", None, 9000)
    c.check("uploading a different file discards the unfinished one (status resumable null) and the hint goes away", done and ok and c.mock.state()["resumable"] is None,
            c.mock.state()["resumable"])
    c.no_console_errors(net=True)


@test
def t10d_folder_upload(c: Ctx):
    if c.phone:
        c.start("files")
        c.tap("#upBtn")
        c.wait("() => !document.querySelector('#menu').hidden")
        c.check("iPhone user agent: upload menu without 'Ordner hochladen' (Safari has no folder picker)", c.menu()["items"] == ["Dateien hochladen", "Neuer Ordner"], c.menu()["items"])
        c.page.keyboard.press("Escape")
        c.reset("files")
        c.open(user_agent=ANDROID_UA)
        c.login(ADMIN)
    else:
        c.start("files")
    c.tap("#upBtn")
    c.wait("() => !document.querySelector('#menu').hidden")
    m = c.menu()
    want = ["Dateien hochladen", "Ordner hochladen", "Neuer Ordner"] if c.phone else ["Dateien hochladen", "Ordner hochladen"]
    c.check("folder upload is an item of the 'Hochladen' menu, no toolbar button of its own" + (" (Android)" if c.phone else ""), m["items"] == want
            and not c.ev("() => !!document.querySelector('#upDirBtn')"), m["items"])
    c.shot("v5_folder_button")
    c.overflow_check("upload menu with folder upload")
    c.mock.set(upload_kbps=40)
    with c.page.expect_file_chooser() as fc:
        c.tap('#menu [data-act="dir"]')
    c.check("the menu item opens a folder chooser (webkitdirectory)", c.ev("() => document.querySelector('#dirPicker').webkitdirectory") is True)
    fc.value.set_files(str(c.run.f["tree_new"]))
    ok = c.wait("() => /^„Neuer Upload Ordner“: \\d von 3 Dateien$/.test(document.querySelector('#upAllT').textContent) && !document.querySelector('#upAll').hidden", None, 10000)
    c.check("folder upload: one line with the folder's progress ('„Neuer Upload Ordner“: n von 3 Dateien')", ok, c.text("#upAllT"))
    c.shot("v5_folder_progress")
    c.overflow_check("upload panel with folder progress")
    done = up_done(c, 3, 40000)
    mk = [q.get("path") for q in reqs(c, "POST", "/api/mkdir")]
    c.check("folders created first, parents before children", mk == ["Neuer Upload Ordner", "Neuer Upload Ordner/Unter", "Neuer Upload Ordner/Unter/Tief"], mk)
    top, sub, deep = c.mock.ls("Neuer Upload Ordner"), c.mock.ls("Neuer Upload Ordner/Unter"), c.mock.ls("Neuer Upload Ordner/Unter/Tief")
    c.check("files land in their folders; hidden files (.DS_Store, .versteckt/) are left out", done and "a.txt" in top and "b.txt" in sub and "c.txt" in deep
            and ".DS_Store" not in top and ".versteckt" not in top, f"{list(top)} {list(sub)} {list(deep)}")
    c.check("summary: toast '3 Dateien gespeichert.', no error toast, folder in the list", c.wait_toast("3 Dateien gespeichert.") and c.wait_rows(["Neuer Upload Ordner"])
            and not any(t["err"] for t in c.toasts()), c.toast_texts())
    close_panel(c)
    c.mock.set(upload_kbps=0)
    # existing folders: 409 EXISTS counts as success
    resp = []
    c.page.on("response", lambda r: resp.append((r.request.method, url_path(r.url), url_q(r.url).get("path"), r.status)) if url_path(r.url) == "/api/mkdir" else None)
    c.clear_toasts()
    c.page.set_input_files("#dirPicker", str(c.run.f["tree_exist"]))
    done = up_done(c, 2)
    c.check("existing folders (mkdir answers 409 EXISTS): files go into them, no error", done and any(s == 409 for *_, s in resp) and "neu2.txt" in c.mock.ls("Projekte")
            and "neu.txt" in c.mock.ls("Projekte/Drachen") and c.wait_toast("2 Dateien gespeichert.") and not any(t["err"] for t in c.toasts()), f"{resp} {c.toast_texts()}")
    close_panel(c)
    # a file that fails: summary toast
    c.clear_toasts()
    c.page.set_input_files("#dirPicker", str(c.run.f["tree_fail"]))
    ok = c.wait("() => !S.working && S.ups.length === 2 && S.ups.every(u => u.state === 'done' || u.state === 'err')", None, 20000)
    c.check("one file fails: '1 Datei gespeichert.' and the summary '„Mit Fehler“: 1 von 2 Dateien nicht gespeichert.' (error toast); the failed file is listed",
            ok and c.wait_toast("1 Datei gespeichert.") and c.wait_toast("„Mit Fehler“: 1 von 2 Dateien nicht gespeichert.") and "leer.txt" in c.text("#upList")
            and "Die Datei ist leer." in c.text("#upList") and "ok.txt" in c.mock.ls("Mit Fehler"), f"{c.toast_texts()} {c.text('#upList')[:80]}")
    c.shot("v5_folder_failed")
    close_panel(c)
    # the folder name belongs to a file (docs/15_api_v5.md section 9: the UI should check with GET /api/files)
    c.clear_toasts()
    c.page.set_input_files("#dirPicker", str(c.run.f["tree_file"]))
    ok = c.wait("() => !S.working && S.ups.length === 1 && S.ups[0].state === 'skip'", None, 20000)
    ok = c.wait_toast("„README“: Ein gleichnamiges Element ist eine Datei.") and ok
    msgs = c.toast_texts()
    c.check("folder name taken by a FILE (README; mkdir 409 EXISTS): the listing tells it apart, one message for the folder "
            "'„README“: Ein gleichnamiges Element ist eine Datei.', not 'Der Zielordner existiert nicht mehr', no upload tried",
            ok and not any("Zielordner" in x or "nicht gespeichert" in x for x in msgs) and c.mock.ls("")["README"]["type"] == "file"
            and not [q for q in reqs(c, "POST", "/api/upload") if q.get("path", "").startswith("README/")], msgs)
    close_panel(c)
    c.no_console_errors()


@test
def t10e_zip_download(c: Ctx):
    p = c.start("files")
    with p.expect_download(timeout=10000) as dl:
        c.act("Projekte", "dl")
    d = dl.value
    names = zipfile.ZipFile(d.path()).namelist()
    want = {"Projekte/Drachen/Drachenkopf.3mf", "Projekte/Drachen/Teile/Schwanz/Segment 01.gcode", "Projekte/Halterungen/Wandhalter.3mf", "Projekte/Drachen/Teile/"}
    c.check("folder menu 'Als ZIP herunterladen': GET /api/zip?path=Projekte, file 'Projekte.zip' with the whole tree (entries start with 'Projekte/')",
            "/api/zip?path=Projekte" in d.url and d.suggested_filename == "Projekte.zip" and want <= set(names) and all(n.startswith("Projekte/") for n in names),
            f"{d.suggested_filename} {d.url} {names[:5]}")
    with p.expect_download(timeout=10000) as dl:
        c.act("Größen Prüfung äöü", "dl")
    d = dl.value
    names = zipfile.ZipFile(d.path()).namelist()
    c.check("folder with umlauts: UTF-8 file name 'Größen Prüfung äöü.zip', UTF-8 entries", d.suggested_filename == "Größen Prüfung äöü.zip"
            and "Größen Prüfung äöü/Zweiter Ordner/Notiz zum Drucken.txt" in names, f"{d.suggested_filename} {names[:4]}")
    # mixed selection: a file and a folder, one after the other
    got = []
    p.on("download", lambda x: got.append(x))
    c.select_rows("README", "Leerer Ordner")
    c.check("selection of a file and a folder: download button visible", vis(c, "#sDl"))
    c.tap("#sDl")
    end = time.time() + 8
    while time.time() < end and len(got) < 2:
        p.wait_for_timeout(150)
    names = sorted(x.suggested_filename for x in got)
    zips = [x for x in got if x.suggested_filename.endswith(".zip")]
    zn = zipfile.ZipFile(zips[0].path()).namelist() if zips else []
    c.check("mixed selection: the file directly, the folder as ZIP (an empty folder gives one folder entry)", names == ["Leerer Ordner.zip", "README"] and zn == ["Leerer Ordner/"], f"{names} {zn}")
    c.tap("#sX")
    n_if = c.ev("() => document.querySelectorAll('iframe').length")
    if n_if:
        c.run.info(f"{c.prof}: after 3 successful ZIP downloads {n_if} hidden iframe(s) stay in the page (removed only in onload, which a download does not fire)")
    c.no_console_errors()
    # refusals: the USB device owns the drive (real refusal of the mock), TOO_MANY_FILES
    c.ev("async () => { await fetch('/mock/set?owner=usb'); download([entry('Projekte')]); }")
    ok = c.wait_dlg("Als ZIP herunterladen", 9000)
    d = c.dlg()
    c.check("ZIP while the USB device owns the drive (409 WRONG_OWNER): dialog 'Als ZIP herunterladen' with the explanation, only 'OK'",
            ok and "Ein ZIP-Download ist nur möglich, solange die Dateiverwaltung aktiv ist." in d["body"] and d["yes"] == "OK" and c.ev("() => document.querySelector('#dNo').hidden"), d)
    c.shot("v5_zip_wrong_owner")
    c.overflow_check("ZIP refused dialog")
    c.dlg_yes()
    c.wait_dlg_closed()
    c.mock.set(owner="app")
    c.ev("() => refresh(true)")
    c.wait("() => S.st.drive.owner === 'app' && S.entries !== null && document.querySelectorAll('#list .row').length > 0", None, 9000)
    c.mock.set(fail_next="TOO_MANY_FILES@/api/zip")
    c.act("Projekte", "dl")
    ok = c.wait_dlg("Als ZIP herunterladen", 9000)
    c.check("413 TOO_MANY_FILES: 'Zu viele Dateien für ein ZIP (höchstens 4096). Bitte Unterordner einzeln laden.'",
            ok and c.dlg()["body"] == "Zu viele Dateien für ein ZIP (höchstens 4096). Bitte Unterordner einzeln laden.", c.dlg()["body"])
    c.dlg_yes()
    c.wait_dlg_closed()
    c.mock.set(fail_next="WRONG_OWNER@/api/zip")
    c.act("Leerer Ordner", "dl")
    ok = c.wait_dlg("Als ZIP herunterladen", 9000)
    c.check("WRONG_OWNER through the row menu: same explanation", ok and "solange die Dateiverwaltung aktiv ist" in c.dlg()["body"], c.dlg()["body"])
    c.dlg_yes()
    c.wait_dlg_closed()
    c.check("after the refusals the page still works (still signed in, list there)", c.ui()["auth"] == "ok" and len(c.ui()["rows"]) == ROOT_COUNT, c.ui()["auth"])
    c.no_console_errors()


@test
def t10f_autolock(c: Ctx):
    c.start("files")
    to_sec(c, "#cryptItem", "() => !document.querySelector('#cryptItem').hidden")
    c.check("unencrypted drive: no automatic lock setting", not vis(c, "#alItem"))
    c.start("crypt_enabled", crypt_kdf_ms=50)
    to_sec(c, "#alItem", "() => !document.querySelector('#alItem').hidden")
    o = c.ev("() => ({v: document.querySelector('#alSel').value, o: [...document.querySelectorAll('#alSel option')].map(x => [x.value, x.textContent]), det: document.querySelector('#swDet').checked,"
             " h: document.querySelector('#alItem h3').textContent.trim()})")
    c.check("encrypted (admin): 'Automatisch sperren' with Aus / 5 / 15 / 30 / 60 minutes / 'Eigene Zeit …', 'Aus' selected, USB switch off",
            o["h"] == "Automatisch sperren" and o["v"] == "0" and o["o"] == [["0", "Aus"], ["5", "nach 5 Minuten"], ["15", "nach 15 Minuten"], ["30", "nach 30 Minuten"],
                                                                         ["60", "nach 60 Minuten"], ["c", "Eigene Zeit …"]] and not o["det"], o)
    c.check("the USB switch text", c.text("#alItem .inner + .inner .txt") == "Sperren, wenn das USB-Gerät getrennt wird", c.text("#alItem .inner + .inner .txt"))
    c.tap("#alItem .ib")
    c.page.wait_for_timeout(350)
    pop = c.pop()
    c.check("the setting's ⓘ says the time only runs in file management, guide 'Automatisch sperren'", pop["open"]
            and "Die Zeit läuft nur in der Dateiverwaltung" in pop["text"] and pop["link"] == "#!hilfe/sperren", pop)
    c.page.keyboard.press("Escape")
    c.wait("() => document.querySelector('#pop').hidden", None, 3000)
    c.shot("v5_autolock")
    c.overflow_check("settings with automatic lock")
    c.clear_toasts()
    c.page.select_option("#alSel", "15")
    ok = c.wait_toast("Gespeichert.")
    q = reqs(c, "POST", "/api/settings/autolock")
    c.check("choose 15 minutes: POST minutes=15&on_detach=0, toast, mock stores it", ok and q and q[-1] == {"minutes": "15", "on_detach": "0"}
            and c.mock.state()["autolock"]["minutes"] == 15, f"{q} {c.toast_texts()}")
    # custom time
    n0 = len(reqs(c, "POST", "/api/settings/autolock"))
    c.page.select_option("#alSel", "c")
    ok = c.wait_dlg("Automatisch sperren")
    d = c.dlg()
    c.check("'Eigene Zeit …': dialog with 'Minuten (1 bis 1440)', prefilled with the current 15", ok and d["fields"] and d["fields"][0]["label"] == "Minuten (1 bis 1440)"
            and d["fields"][0]["value"] == "15" and d["yes"] == "Speichern", d)
    c.shot("v5_autolock_custom")
    c.overflow_check("custom minutes dialog")
    for v in ("0", "1441", "abc", "2.5", "-5", ""):
        c.fill_dlg(v)
        c.dlg_yes()
        c.wait_derr()
        c.check(f"custom minutes {v!r}: 'Bitte eine ganze Zahl von 1 bis 1440 eingeben.'", c.dlg()["err"] == "Bitte eine ganze Zahl von 1 bis 1440 eingeben." and c.dlg()["open"], c.dlg()["err"])
    c.check("invalid inputs sent nothing", len(reqs(c, "POST", "/api/settings/autolock")) == n0)
    c.fill_dlg("90")
    c.clear_toasts()
    c.dlg_yes()
    ok = c.wait_dlg_closed() and c.wait_toast("Gespeichert.")
    c.wait("() => document.querySelector('#alSel').value === '90'", None, 9000)
    sel = c.ev("() => [document.querySelector('#alSel').value, document.querySelector('#alSel').selectedOptions[0].textContent]")
    c.check("90 minutes saved: mock 90, the list shows 'nach 90 Minuten' selected", ok and c.mock.state()["autolock"]["minutes"] == 90 and sel == ["90", "nach 90 Minuten"], sel)
    n1 = len(reqs(c, "POST", "/api/settings/autolock"))
    c.page.select_option("#alSel", "c")
    c.wait_dlg("Automatisch sperren")
    c.dlg_no()
    c.wait_dlg_closed()
    ok = c.wait("() => document.querySelector('#alSel').value === '90'", None, 5000)
    c.check("cancel the custom dialog: list back at 90, nothing sent", ok and len(reqs(c, "POST", "/api/settings/autolock")) == n1, c.ev("() => document.querySelector('#alSel').value"))
    c.clear_toasts()
    c.tap("label.switch:has(#swDet)")
    ok = c.wait_toast("Gespeichert.")
    q = reqs(c, "POST", "/api/settings/autolock")
    c.check("USB switch on: POST minutes=90&on_detach=1, mock on_detach true, minutes kept", ok and q[-1] == {"minutes": "90", "on_detach": "1"}
            and c.mock.state()["autolock"] == {**c.mock.state()["autolock"], "minutes": 90, "on_detach": True}, q[-1:])
    # the automatic lock strikes
    c.page.select_option("#alSel", "5")
    c.wait("() => S.st.autolock.minutes === 5", None, 9000)
    c.go("#/")
    c.wait("() => S.entries !== null", None, 9000)
    c.clear_toasts()
    c.mock.set(idle_s=301)
    ok = wait_unlock_view(c, True, 12000)
    c.check("5 minutes without file access (mock idle_s=301): the page shows the unlock view and the 'locked' toast", ok and c.wait_toast(T_LOCKED_DONE), c.toast_texts())
    to_sec(c, "#alItem")
    c.check("locked (still encrypted): the setting stays available for the admin", vis(c, "#alItem"))
    c.no_console_errors()
    c.start("crypt_enabled", who=GUEST, crypt_kdf_ms=50)
    to_sec(c, "#cryptItem", "() => !document.querySelector('#cryptItem').hidden")
    c.check("role user: no automatic lock setting", not vis(c, "#alItem"))
    c.start("crypt_damaged")
    to_sec(c, "#cryptItem", "() => !document.querySelector('#cryptItem').hidden")
    c.check("damaged key area: no automatic lock setting", not vis(c, "#alItem"))
    c.no_console_errors()


@test
def t10g_display_language(c: Ctx):
    c.reset("two_users")
    c.open()
    c.tap("#loginForm [data-lang]")
    c.wait("() => lang === 'en'")
    c.login(ADMIN)
    ok = wait_req(c, "POST", "/api/settings/lang")
    q = reqs(c, "POST", "/api/settings/lang")
    c.check("English chosen on the sign-in page: after signing in POST /api/settings/lang?value=en, the mock's display language follows",
            ok and q[-1] == {"value": "en"} and c.mock.state()["lang"] == "en", q)
    to_sec(c, "#s-gen")
    c.tap("#langX")
    pop = c.pop()
    c.check("language item: its ⓘ says it also applies to the display, guide 'Taste und Display'", pop["open"] and pop["text"] == "Also applies to the display on the stick."
            and pop["link"] == "#!hilfe/stick", pop)
    c.page.keyboard.press("Escape")
    c.tap('#langSeg button[data-v="de"]')
    c.wait("() => lang === 'de'")
    ok = wait_req(c, "POST", "/api/settings/lang", 2)
    q = reqs(c, "POST", "/api/settings/lang")
    c.check("switching to Deutsch in the settings: POST value=de, mock 'de', note in German", ok and q[-1] == {"value": "de"} and c.mock.state()["lang"] == "de"
            and c.ev("() => (document.querySelector('#langX').click(), document.querySelector('#popT').textContent)") == "Gilt auch für das Display am Stick.", q)
    c.page.keyboard.press("Escape")
    c.tap('#langSeg button[data-v="de"]')
    c.page.wait_for_timeout(800)
    c.check("choosing the language that is already set sends nothing", len(reqs(c, "POST", "/api/settings/lang")) == 2, reqs(c, "POST", "/api/settings/lang"))
    c.no_console_errors()
    # no stored choice: the display language is left alone
    c.reset("two_users")
    c.mock.post("/api/settings/lang?value=en")
    c.open()
    c.login(ADMIN)
    c.page.wait_for_timeout(1500)
    c.check("German browser without a stored choice: no POST /api/settings/lang, the display keeps 'en'", not reqs(c, "POST", "/api/settings/lang") and c.mock.state()["lang"] == "en",
            reqs(c, "POST", "/api/settings/lang"))
    # role user may set it too
    c.reset("two_users")
    c.open()
    c.tap("#loginForm [data-lang]")
    c.wait("() => lang === 'en'")
    c.login(GUEST)
    ok = wait_req(c, "POST", "/api/settings/lang") and c.wait("() => S.st && S.st.lang === 'en'", None, 9000)
    c.check("role user: the display language follows the choice as well", ok and c.mock.state()["lang"] == "en", c.mock.state()["lang"])
    c.no_console_errors()


@test
def t10h_network_drive(c: Ctx):
    c.start("two_users", wifi_count=1)
    to_sec(c, "#davAddr", "() => document.querySelector('#davAddr').textContent !== '–'")
    lines = c.ev("() => document.querySelector('#davAddr').innerText.split('\\n').map(s => s.trim()).filter(Boolean)")
    c.check("network drive card: title, both addresses (name.local and IP) with /dav/", c.ev("() => document.querySelector('#davAddr').closest('.item').querySelector('h3').textContent.trim()") == "Als Netzlaufwerk verbinden"
            and lines == ["http://datadrive-mock.local/dav/", "http://192.0.2.152/dav/"], lines)
    c.check("no instructions in the card; its ⓘ explains and leads to the guide 'Netzlaufwerk'", not c.ev("() => !!document.querySelector('#davHow')")
            and c.ev("() => { const b = document.querySelector('#b-net [data-i=davX]'); return !!b && b.dataset.h === 'netzlaufwerk'; }"))
    how = c.ev("() => [...L.de.helpDoc].find(x => x[0] === 'netzlaufwerk')[2]")
    c.check("help page 'Netzlaufwerk': Windows steps (tools/webdav script) and phone note (iPhone 'Dateien' cannot)", "Windows:" in how and "tools/webdav" in how and "Handy:" in how, how[:80])
    c.check("switch shown because the status has 'webdav' (on)", vis(c, "label.switch:has(#swDav)") and c.ev("() => document.querySelector('#swDav').checked"))
    c.shot("v5_dav_card")
    c.overflow_check("network drive card")
    c.clear_toasts()
    c.tap("label.switch:has(#swDav)")
    ok = c.wait_toast("Gespeichert.")
    st, _ = c.mock.raw("/dav/", method="OPTIONS")
    c.check("switch off: POST /api/settings/webdav?value=0, toast, /dav/ answers 404", ok and reqs(c, "POST", "/api/settings/webdav")[-1] == {"value": "0"}
            and c.mock.state()["webdav"] is False and st == 404 and not c.ev("() => document.querySelector('#swDav').checked"), f"{st} {c.toast_texts()}")
    c.clear_toasts()
    c.tap("label.switch:has(#swDav)")
    ok = c.wait_toast("Gespeichert.")
    st, _ = c.mock.raw("/dav/", method="OPTIONS")
    c.check("switch on again: /dav/ answers 200", ok and c.mock.state()["webdav"] is True and st == 200, st)
    c.no_console_errors()
    # role user: no network section, no switch
    c.start("two_users", who=GUEST)
    to_sec(c, "#s-usb")
    c.check("role user: network section and switch not shown", not vis(c, "#s-net") and not vis(c, "label.switch:has(#swDav)"))
    # firmware without the switch: the field is missing
    c.reset("two_users", wifi_count=1)
    c.open()
    route_status(c, ("webdav",))
    c.login(ADMIN)
    to_sec(c, "#davAddr", "() => document.querySelector('#davAddr').textContent !== '–'")
    c.check("status without 'webdav': card with the addresses, but no switch", vis(c, "#davAddr") and not vis(c, "label.switch:has(#swDav)") and "/dav/" in c.text("#davAddr"))
    c.overflow_check("network drive card without switch")
    c.no_console_errors()


@test
def t10i_https_section(c: Ctx):
    c.start("two_users", wifi_count=1)
    to_sec(c, "#httpsBox", "() => !document.querySelector('#httpsBox').hidden")
    c.check("status with the HTTPS fields: HTTPS block in the card 'Sicherheit', state 'Aus', summary says it, explanation behind the ⓘ",
            vis(c, "#httpsBox") and c.text("#httpsState") == "Aus" and not c.ev("() => document.querySelector('#swHttps').checked")
            and "HTTPS aus" in c.text("#sumSec") and c.ev("() => !!document.querySelector('#httpsBox [data-i=i_https]')") and not c.ev("() => !!document.querySelector('#httpsList, #certHow')"),
            c.text("#httpsState"))
    c.tap("label.switch:has(#swHttps)")
    ok = c.wait_dlg("HTTPS einschalten?")
    c.check("switch: confirmation 'HTTPS einschalten?' with 'Einschalten'", ok and c.dlg()["yes"] == "Einschalten", c.dlg()["title"])
    c.dlg_yes()
    ok = c.wait_dlg("Jetzt neu starten?")
    c.check("after saving: offer to restart now", ok and c.dlg()["no"] == "Später", c.dlg()["title"])
    c.clear_toasts()
    c.dlg_no()
    ok = c.wait_toast("Gespeichert. Wirksam nach dem nächsten Neustart.") and c.wait("() => !document.querySelector('#httpsPend').hidden", None, 9000)
    st = c.mock.state()["status"]
    c.check("'Später': toast, pending notice, mock setting on (restart needed), fingerprints shown", ok and st["https"] is True and st["https_restart"] is True
            and c.text("#fpCa") not in ("", "–"), f"{st.get('https')} {st.get('https_restart')} {c.text('#fpCa')[:20]}")
    c.shot("v5_https_pending")
    c.overflow_check("HTTPS section")
    c.no_console_errors()
    # firmware built without PD_HTTPS_OPTION: the fields are missing
    c.run.info("the mock always reports the HTTPS fields (no switch for a build without PD_HTTPS_OPTION); the test removes them from /api/status in the browser")
    c.reset("two_users", wifi_count=1)
    c.open()
    route_status(c, HTTPS_FIELDS)
    c.login(ADMIN)
    to_sec(c, "#s-sys", "() => document.querySelector('#sysVer').textContent !== '–'")
    open_card(c, "sec")
    c.check("status without the HTTPS fields: no HTTPS block, the card 'Sicherheit' stays (trusted devices) and its summary has no HTTPS",
            not vis(c, "#httpsBox") and vis(c, "#s-dev") and "HTTPS" not in c.text("#sumSec"), c.text("#sumSec"))
    tabs = c.ev(JS_TABS)
    c.check("settings cards without HTTPS", tabs == ["WLAN", "Speicher & USB", "Sicherheit", "Benutzer", "Darstellung & Sprache", "System"], tabs)
    c.overflow_check("settings without HTTPS")
    c.no_console_errors()


@test
def t10j_captive(c: Ctx):
    c.reset("files")
    c.open(query="?captive=1")
    loc = c.ev("() => location.pathname + location.search")
    c.check("captive sheet (/?captive=1): address cleaned to '/', sign-in view with the hint about the phone's sign-in window",
            loc == "/" and vis(c, "#cpLogin") and c.text("#cpLogin").startswith("Anmeldefenster des Telefons – hier klappt nur die WLAN-Einrichtung.")
            and c.ev("() => document.querySelector('#cpLogin .ib').dataset.i") == "cpX" and c.ev("() => document.documentElement.classList.contains('cap')"),
            f"{loc} {c.text('#cpLogin')[:50]}")
    c.shot("v5_captive_login")
    c.overflow_check("captive sign-in")
    c.login(ADMIN)
    hint = c.text("#cpHint")
    c.check("signed in: hint at the top with the link 'Zur WLAN-Einrichtung' (admin)", vis(c, "#cpHint") and "hier klappt nur die WLAN-Einrichtung" in hint and c.text("#cpHint a") == "Zur WLAN-Einrichtung", hint[:80])
    ui = c.ui()
    c.check("captive: no upload buttons (upload, folder upload, FAB)", not ui["upBtn"] and not ui["fab"] and not vis(c, "#upDirBtn"), f"up {ui['upBtn']} fab {ui['fab']}")
    c.check("captive: file menu without 'Herunterladen'", menu_items(c, "README") == ["Umbenennen", "Ausschneiden", "Kopieren", "Auswählen", "Löschen"])
    c.check("captive: folder menu without 'Als ZIP herunterladen'", menu_items(c, "Projekte") == ["Öffnen", "Umbenennen", "Ausschneiden", "Kopieren", "Auswählen", "Löschen"])
    c.select_rows("README")
    c.check("captive: no download button in the selection bar", not vis(c, "#sDl"))
    c.tap("#sX")
    c.mock.set(resumable="Projekte/Teil.3mf|500000|65536")
    c.page.wait_for_timeout(3500)
    c.check("captive: no hint for an unfinished upload (choosing a file is not possible)", not vis(c, "#resHint") and c.ev("() => S.st.upload.resumable !== null"))
    c.shot("v5_captive_files")
    c.overflow_check("captive file view")
    c.tap("#cpHint a")
    ok = c.wait("() => !document.querySelector('#vSettings').hidden", None, 6000)
    c.check("'Zur WLAN-Einrichtung' opens the settings", ok)
    c.no_console_errors()
    # setup access point: add the home network inside the sheet
    c.reset("ap_setup", scan_ms=200)
    c.open(query="?captive=1")
    c.login(ADMIN)
    c.check("ap_setup + captive: hint shown, the USB view offers no upload", vis(c, "#cpHint") and not vis(c, "#lockedUp"))
    to_sec(c, "#s-net")
    c.tap("#wifiAdd")
    c.wait_dlg("WLAN hinzufügen")
    c.fill_dlg("Heimnetz", "geheim1234")
    c.dlg_yes()
    ok = c.wait("() => document.querySelector('#dlg').open && document.querySelector('#dBody').textContent.includes('Dieses Fenster trennt sich gleich.')", None, 9000)
    d = c.dlg()
    c.check("network added in the sheet: the note stays on screen (dialog, only 'OK'): saved, reconnecting, switch to the home Wi-Fi",
            ok and "„Heimnetz“ gespeichert." in d["body"] and "ins Heim-WLAN wechseln" in d["body"] and d["yes"] == "OK" and c.ev("() => document.querySelector('#dNo').hidden"), d["body"][:120])
    c.shot("v5_captive_added")
    c.overflow_check("captive note after adding a network")
    c.dlg_yes()
    c.wait_dlg_closed()
    c.no_console_errors(net=True)
    # role user: hint without the link
    c.reset("two_users")
    c.open(query="?captive=1")
    c.login(GUEST)
    c.check("role user in the sheet: hint without the setup link", vis(c, "#cpHint") and not c.ev("() => !!document.querySelector('#cpHint a')"))
    c.no_console_errors()


@test
def t10k_v5_english_and_layout(c: Ctx):
    c.reset("crypt_enabled", crypt_kdf_ms=50, wifi_count=1)
    c.open(locale="en-GB")
    c.login(ADMIN)
    c.mock.set(resumable="Projekte/Big part.3mf|1048576|262144")
    ok = c.wait("() => document.querySelector('#resHint').getClientRects().length > 0", None, 9000)
    r = c.ev(JS_UNTRANSLATED)
    c.check("English hint for an unfinished upload, no German leftovers", ok and "Upload of “Big part.3mf” stopped at 25 %." in c.text("#resHint") and not r["bad"], f"{c.text('#resHint')[:70]} {r['bad']}")
    c.check("English folder menu: 'Download as ZIP'", menu_items(c, "Projekte")[:2] == ["Open", "Download as ZIP"])
    c.tap("#upBtn")
    c.wait("() => !document.querySelector('#menu').hidden")
    c.check("English upload menu", c.menu()["items"] == (["Upload files", "New folder"] if c.phone else ["Upload files", "Upload folder"]), c.menu()["items"])
    c.page.keyboard.press("Escape")
    c.mock.set(fail_next="TOO_MANY_FILES@/api/zip")
    c.act("Projekte", "dl")
    ok = c.wait_dlg("Download as ZIP", 9000)
    r = c.ev(JS_UNTRANSLATED)
    c.check("English ZIP refusal", ok and c.dlg()["body"] == "Too many files for one ZIP (at most 4096). Please download the subfolders one by one." and not r["bad"], f"{c.dlg()['body']} {r['bad']}")
    c.dlg_yes()
    c.wait_dlg_closed()
    to_sec(c, "#alItem", "() => !document.querySelector('#alItem').hidden && !document.querySelector('#timeItem').hidden")
    bad = []
    for k in ("usb", "sys", "net", "gen"):
        open_card(c, k)
        bad += c.ev(JS_UNTRANSLATED)["bad"]
    c.tap("#langX")
    note = c.pop()["text"]
    c.page.keyboard.press("Escape")
    o = c.ev("() => [...document.querySelectorAll('#alSel option')].map(x => x.textContent)")
    c.check("English settings v5: automatic lock, clock, network drive, display note (ⓘ); no German leftovers or raw keys",
            c.text("#alItem h3").strip() == "Lock automatically" and o == ["Off", "after 5 minutes", "after 15 minutes", "after 30 minutes", "after 60 minutes", "Custom time …"]
            and c.text("#sysTime").endswith(" · from the network") and note == "Also applies to the display on the stick."
            and "Connect as a network drive" in c.text("#s-net") and c.text("#timeItem h3").strip() == "Time zone" and not bad, f"{o} {c.text('#sysTime')} {bad}")
    open_card(c, "usb")
    c.shot("v5_settings_en", full=True)
    c.page.select_option("#alSel", "c")
    ok = c.wait_dlg("Lock automatically")
    c.fill_dlg("0")
    c.dlg_yes()
    c.wait_derr()
    r = c.ev(JS_UNTRANSLATED)
    c.check("English custom minutes dialog", ok and c.dlg()["fields"][0]["label"] == "Minutes (1 to 1440)" and c.dlg()["err"] == "Please enter a whole number from 1 to 1440." and not r["bad"],
            f"{c.dlg()['err']} {r['bad']}")
    c.dlg_no()
    c.wait_dlg_closed()
    c.mock.set(clock="unsynced")
    c.check("English: clock not set", c.wait("() => document.querySelector('#sysTime').textContent === '– · not set'", None, 12000), c.text("#sysTime"))
    # English upload messages (the browser aborts every upload: 1 try + 3 retries)
    c.go("#/")
    c.wait("() => S.entries !== null", None, 9000)
    is_up = lambda u: url_path(u) == "/api/upload"
    c.page.route(is_up, lambda route: route.abort("connectionreset"))
    c.page.set_input_files("#picker", str(c.run.f["txt"]))
    ok1 = c.wait("() => document.querySelector('#upList').textContent.includes('Connection lost — trying again …')", None, 9000)
    ok2 = c.wait("() => !!document.querySelector('#upList [data-goon]')", None, 25000)
    r = c.ev(JS_UNTRANSLATED)
    c.check("English upload retry texts: 'Connection lost — trying again …', then 'Connection lost. The upload can be resumed.' and 'Resume'",
            ok1 and ok2 and "Connection lost. The upload can be resumed." in c.text("#upList") and c.text("#upList [data-goon]") == "Resume" and not r["bad"], f"{c.text('#upList')[:90]} {r['bad']}")
    c.page.unroute(is_up)
    c.tap("#upClose")
    # a resume that keeps failing with RESUME_MISMATCH (4 answers without a usable offset): the message must be English
    dev_text = "Der Upload kann an dieser Stelle nicht fortgesetzt werden."
    c.page.route(is_up, lambda route: route.fulfill(status=409, content_type="application/json",
                                                    body=json.dumps({"ok": False, "code": "RESUME_MISMATCH", "error": dev_text})))
    c.page.set_input_files("#picker", str(c.run.f["odd"]))
    ok = c.wait("() => S.ups.length === 1 && S.ups[0].state === 'err'", None, 15000)
    msg = c.ev("() => S.ups.length ? S.ups[0].msg : ''")
    n_mis = len([1 for q in reqs(c, "POST", "/api/upload") if q.get("path") == c.run.f["odd"].name])
    c.check("RESUME_MISMATCH that does not resolve (1 + 3 tries): error in English, not the device's German text", ok and n_mis == 4 and msg and dev_text not in msg,
            f"{n_mis} tries, {msg!r}")
    c.page.unroute(is_up)
    c.tap("#upClose")
    c.no_console_errors(net=True)
    # English captive hint
    c.reset("files")
    c.open(query="?captive=1", locale="en-GB")
    r = c.ev(JS_UNTRANSLATED)
    c.check("English captive hint on the sign-in view", c.text("#cpLogin").startswith("Phone sign-in window — only the Wi-Fi setup works here.") and not r["bad"], f"{c.text('#cpLogin')[:50]} {r['bad']}")
    # narrow windows
    for vw, vh in ((390, 844), (320, 568)):
        c.reset("crypt_enabled", crypt_kdf_ms=50, wifi_count=1)
        c.open("", viewport={"width": vw, "height": vh})
        c.login(ADMIN)
        c.mock.set(resumable="Projekte/Ein ziemlich langer Name für ein unterbrochenes Teil.3mf|1048576|262144")
        c.wait("() => document.querySelector('#resHint').getClientRects().length > 0", None, 9000)
        c.overflow_check(f"{vw} px wide: hint for an unfinished upload")
        c.ev("() => { S.ups.push({id: 9999, file: new File(['x'], 'x.txt'), name: 'Überlanger Dateiname eines abgebrochenen Uploads mit Fortsetzen.3mf', dir: '', size: 1,"
             " state: 'err', msg: t('upLostR'), resume: true, pct: 37}); renderUploads(); }")
        c.overflow_check(f"{vw} px wide: upload panel with 'Fortsetzen'")
        c.shot(f"w{vw}_v5_upload_panel")
        c.ev("() => clearUps()")
        c.open_menu("Projekte")
        c.overflow_check(f"{vw} px wide: folder menu with 'Als ZIP herunterladen'")
        c.page.keyboard.press("Escape")
        to_sec(c, "#alItem", "() => !document.querySelector('#alItem').hidden && !document.querySelector('#timeItem').hidden")
        c.overflow_check(f"{vw} px wide: settings (automatic lock)")
        open_card(c, "net")
        c.ev("() => document.querySelector('#s-net').scrollIntoView()")
        c.overflow_check(f"{vw} px wide: settings (network drive)")
        open_card(c, "sys")
        c.ev("() => document.querySelector('#timeItem').scrollIntoView()")
        c.overflow_check(f"{vw} px wide: settings (clock and time zone)")
        open_card(c, "usb")
        c.shot(f"w{vw}_v5_settings")
        c.page.select_option("#alSel", "c")
        c.wait_dlg("Automatisch sperren")
        c.overflow_check(f"{vw} px wide: custom minutes dialog")
        c.dlg_no()
        c.wait_dlg_closed()
        c.reset("files")
        c.open("", query="?captive=1", viewport={"width": vw, "height": vh})
        c.overflow_check(f"{vw} px wide: captive sign-in")
        c.login(ADMIN)
        c.overflow_check(f"{vw} px wide: captive file view")
        c.shot(f"w{vw}_v5_captive")
    c.no_console_errors(net=True)


# ================================================================== 11. redesign (0.5.x UI): ⓘ popovers, help page, no doubles, motion

@test
def t11a_info_popovers(c: Ctx):
    c.start("files")
    pop = c.pop()
    c.check("no popover open at first; every visible (i) is a button with aria-expanded=false and aria-controls=pop",
            not pop["open"] and c.ev("() => [...document.querySelectorAll('.ib')].filter(b => b.getClientRects().length).every(b => b.tagName === 'BUTTON' && b.getAttribute('aria-expanded') === 'false' && b.getAttribute('aria-controls') === 'pop' && b.getAttribute('aria-label'))"))
    c.tap("#modeInfo")
    c.page.wait_for_timeout(350)
    pop = c.pop()
    c.check("tap on the mode card's ⓘ: popover with 1-3 sentences and 'Anleitung öffnen', aria-expanded=true, inside the viewport",
            pop["open"] and pop["text"].startswith("Das USB-Gerät sieht das Laufwerk im Moment nicht.") and 1 <= pop["text"].count(".") <= 3
            and pop["linkTxt"] == "Anleitung öffnen" and pop["link"] == "#!hilfe/betriebsart" and pop["expanded"] == ["modeInfo"]
            and pop["l"] >= 0 and pop["r"] <= pop["vw"] and pop["t"] >= 0 and pop["b"] <= pop["vh"], pop)
    c.shot("popover_mode")
    c.check("the popover follows its button in the DOM (keyboard order)", c.ev("() => document.querySelector('#modeInfo').nextElementSibling === document.querySelector('#pop')"))
    c.ev("() => document.querySelector('#encInfo').click()")
    pop = c.pop()
    c.check("another ⓘ: one popover at a time (the first one closes)", pop["open"] and pop["expanded"] == ["encInfo"] and pop["text"].startswith("Wer die Speicherkarte entnimmt"), pop)
    c.ev("() => document.querySelector('#encInfo').click()")
    c.check("tapping the same ⓘ again closes it", not c.pop()["open"] and not c.pop()["expanded"])
    c.tap("#encInfo")
    c.tap("#stoCell")                                     # plain text above the popover (a target below could be covered by it)
    c.check("a tap outside closes the popover (and goes nowhere)", c.wait("() => document.querySelector('#pop').hidden", None, 2000) and not c.pop()["expanded"] and c.ev("() => document.querySelector('#vHelp').hidden"))
    c.tap("#encInfo")
    c.page.keyboard.press("Escape")
    pop = c.pop()
    c.check("Escape closes it and gives the focus back to its (i)", not pop["open"] and pop["focus"] == "encInfo", pop)
    c.page.keyboard.press("Enter")
    c.check("keyboard: Enter on the focused (i) opens it again", c.wait("() => !document.querySelector('#pop').hidden", None, 2000))
    c.page.keyboard.press("Escape")
    # 'Anleitung öffnen' lands on the right section of the help page
    c.tap("#modeInfo")
    c.tap("#popA")
    ok = c.wait("() => !document.querySelector('#vHelp').hidden && location.hash === '#!hilfe/betriebsart'", None, 4000)
    c.page.wait_for_timeout(400)
    top = c.ev("() => document.querySelector('#hilfe-betriebsart').getBoundingClientRect().top")
    c.check("'Anleitung öffnen' opens the help page at 'Betriebsart und Drucker' (scrolled to it), popover closed", ok and 0 <= top < 160 and not c.pop()["open"]
            and c.text("#hilfe-betriebsart h2") == "Betriebsart und Drucker", top)
    c.shot("help_betriebsart")
    c.check("help page: back link to where it came from, title, table of contents with every topic", c.ev("() => document.querySelector('#helpBack').getAttribute('href')") == "#/"
            and c.text("#vHelp h1") == "Hilfe & Anleitungen" and c.ev("() => document.querySelectorAll('#helpToc a').length") == 12, c.ev("() => document.querySelector('#helpBack').getAttribute('href')"))
    c.overflow_check("help page")
    nm = c.ev(JS_NAMES)
    c.check("help page: every link has an accessible name", not nm["bad"], nm["bad"][:3])
    c.tap("#helpToc a[href='#!hilfe/netzlaufwerk']")
    ok = c.wait("() => location.hash === '#!hilfe/netzlaufwerk'", None, 3000)
    c.page.wait_for_timeout(300)
    c.check("table of contents: jumps to the topic", ok and 0 <= c.ev("() => document.querySelector('#hilfe-netzlaufwerk').getBoundingClientRect().top") < 160)
    c.tap("#helpBack")
    c.check("back link: file view again", c.wait("() => document.querySelector('#vHelp').hidden && !document.querySelector('#vFiles').hidden", None, 4000))
    # settings: the ⓘ of an item, inside an open card
    c.go("#einstellungen")
    open_card(c, "sys")
    c.ev("() => document.querySelector('#b-sys [data-i=otaX]').scrollIntoView({block: 'center'})")
    c.tap("#b-sys [data-i=otaX]")
    pop = c.pop()
    c.check("settings item ⓘ (Gerätesoftware): explanation and guide 'update', inside the viewport", pop["open"] and "printdrive.bin" in pop["text"] and pop["link"] == "#!hilfe/update"
            and pop["l"] >= 0 and pop["r"] <= pop["vw"] and pop["b"] <= pop["vh"], pop)
    c.check("no description paragraphs in the settings items (only labels, values, buttons)",
            not c.ev("() => [...document.querySelectorAll('.secbody .item .txt p')].some(p => p.textContent.length > 70)"),
            c.ev("() => [...document.querySelectorAll('.secbody .item .txt p')].map(p => p.textContent).filter(t => t.length > 70)"))
    c.page.keyboard.press("Escape")
    c.go("#!hilfe/verschluesselung")
    c.check("a deep link '#!hilfe/verschluesselung' opens that section", c.wait("() => !document.querySelector('#vHelp').hidden", None, 3000)
            and c.text("#hilfe-verschluesselung h2") == "Verschlüsselung")
    c.check("from the settings the back link returns to the settings", c.ev("() => document.querySelector('#helpBack').getAttribute('href')") == "#einstellungen")
    c.go("#/hilfe")
    c.check("a folder called 'hilfe' (#/hilfe) is a folder route, not the help page", c.wait("() => document.querySelector('#vHelp').hidden && !document.querySelector('#vFiles').hidden", None, 4000))
    # inside a dialog: Escape closes the popover only
    c.go("#/")
    c.wait_dir("")
    c.tap("#encSetup")
    c.wait_dlg("Laufwerk verschlüsseln?")
    c.tap("#dInfo")
    pop = c.pop()
    c.check("dialog ⓘ: popover inside the dialog with the longer explanation", pop["open"] and "nur noch in diesem Stick lesbar" in pop["text"]
            and c.ev("() => document.querySelector('#dlg').contains(document.querySelector('#pop'))"), pop)
    c.shot("popover_dialog")
    c.page.keyboard.press("Escape")
    c.check("Escape in the dialog: first the popover closes, the dialog stays", not c.pop()["open"] and c.dlg()["open"])
    c.page.keyboard.press("Escape")
    c.check("second Escape closes the dialog", c.wait_dlg_closed())
    c.no_console_errors()
    # narrow phone: the popover stays inside 320 px
    if c.phone:
        c.reset("files")
        c.open("", viewport={"width": 320, "height": 568})
        c.login(ADMIN)
        for sel in ("#modeInfo", "#encInfo"):
            c.tap(sel)
            c.page.wait_for_timeout(300)
            pop = c.pop()
            c.check(f"320 px: popover of {sel} inside the viewport", pop["open"] and pop["l"] >= 0 and pop["r"] <= 320 and pop["b"] <= pop["vh"], pop)
            c.page.keyboard.press("Escape")


@test
def t11b_help_signed_out_and_doubles(c: Ctx):
    c.reset("two_users")
    c.open()
    c.check("sign-in view: no lead text; the password reset hint is behind an ⓘ", not c.ev("() => !!document.querySelector('#loginForm p.muted, #loginForm p.small')")
            and c.ev("() => !!document.querySelector('#loginForm [data-i=loginHelp]')"))
    c.tap("#loginForm [data-i=loginHelp]")
    pop = c.pop()
    c.check("sign-in ⓘ: '10 Sekunden … admin / admin', guide 'Taste und Display'", pop["open"] and "10 Sekunden" in pop["text"] and pop["link"] == "#!hilfe/stick", pop)
    c.tap("#popA")
    ok = c.wait("() => !document.querySelector('#vHelp').hidden && document.querySelector('#vLogin').getClientRects().length === 0", None, 4000)
    c.check("help page reachable signed out (static texts), back link to the sign-in", ok and c.text("#hilfe-stick h2") == "Taste und Display am Stick"
            and c.ev("() => document.querySelector('#helpBack').getAttribute('href')") == "#", c.ui()["auth"])
    c.overflow_check("help page signed out")
    c.shot("help_signed_out")
    c.tap("#helpBack")
    c.check("back: sign-in view again", c.wait("() => document.querySelector('#vLogin').getClientRects().length > 0 && document.querySelector('#vHelp').hidden", None, 4000))
    c.tap("#loginForm [data-lang]")
    c.wait("() => lang === 'en'")
    c.go("#!hilfe")
    c.wait("() => !document.querySelector('#vHelp').hidden")
    r = c.ev(JS_UNTRANSLATED)
    c.check("English help page: English titles, no German leftovers", c.text("#vHelp h1") == "Help & guides" and c.text("#hilfe-https h2") == "HTTPS and certificate" and not r["bad"], r["bad"])
    c.no_console_errors()
    # no doubles: 'Sperren' only in the encryption card (and the settings' encryption management), not in the user menu
    c.start("crypt_enabled", crypt_kdf_ms=50)
    c.tap("#userBtn")
    c.wait("() => !document.querySelector('#menu').hidden")
    m = c.menu()
    c.check("user menu: no 'Sperren' (only password and sign-out)", m["items"] == ["Passwort ändern", "Abmelden"], m["items"])
    c.page.keyboard.press("Escape")
    n = c.ev("() => [...document.querySelectorAll('#app button, #app a')].filter(e => e.getClientRects().length && /sperren$/i.test(e.textContent.trim()) && !/entsperren/i.test(e.textContent)).length")
    c.check("file view: exactly one 'Sperren' (the encryption card)", n == 1, n)
    c.check("phone and desktop: no floating upload button and no separate '+' next to the title", not c.ev("() => !!document.querySelector('#fab')")
            and not c.ev("() => [...document.querySelectorAll('#toolbar h1 ~ button')].some(b => b.getClientRects().length && !b.textContent.trim())"))
    c.no_console_errors()


@test
def t11c_reduced_motion(c: Ctx):
    c.reset("usb_no_host")
    c.open(reduced_motion="reduce")
    c.login(ADMIN)
    tr = c.ev("() => getComputedStyle(document.querySelector('#pop')).transitionProperty + ' | ' + getComputedStyle(document.querySelector('.seg2'), '::before').transitionProperty")
    c.check("reduced motion: transitions only cross-fade (no transform)", "transform" not in tr, tr)
    c.switch()
    ok = c.wait("() => S.st.drive.owner === 'app' && document.querySelector('#segApp').getAttribute('aria-pressed') === 'true'", None, 9000)
    c.check("reduced motion: switching the mode works", ok and c.ui()["thumb"] == "app")
    c.tap("#modeInfo")
    c.check("reduced motion: popover opens", c.wait("() => !document.querySelector('#pop').hidden", None, 2000))
    c.page.keyboard.press("Escape")
    c.tap("#upBtn")
    c.check("reduced motion: upload menu opens", c.wait("() => !document.querySelector('#menu').hidden", None, 2000))
    c.page.keyboard.press("Escape")
    c.new_folder()
    c.check("reduced motion: dialog opens", c.wait_dlg("Neuer Ordner"))
    c.dlg_no()
    c.wait_dlg_closed()
    c.go("#einstellungen")
    open_card(c, "gen")
    c.check("reduced motion: settings card opens", vis(c, "#theme"))
    c.go("#!hilfe")
    c.check("reduced motion: help page opens", c.wait("() => !document.querySelector('#vHelp').hidden", None, 3000))
    c.no_console_errors()


# ================================================================== 12. one request at a time, auto-lock countdown, network, signed updates

# The device with HTTPS keeps ONE connection: a new connection closes the older one, and a request that dies with its
# connection is gone (the browser does not repeat a POST). Modelled inside the page: every /api/ fetch is one connection;
# one that starts while another is open kills it (rejected like a reset; not sent at all if it was still in its 150 ms
# "handshake"). XHR uploads count as open connections too. __net.max = the most requests that were open at once.
DEVICE_JS = r"""
window.__net = {max: 0, killed: [], n: 0};
(() => {
  const real = window.fetch.bind(window), live = new Set(), N = window.__net;
  const open = me => { N.max = Math.max(N.max, live.size + 1); N.n++; for (const o of live) { o.dead = true; N.killed.push(o.url); } live.add(me); };
  window.fetch = function(input, init){
    const url = typeof input === 'string' ? input : input.url;
    if (!/\/api\//.test(url)) return real(input, init);
    const me = {url, dead: false};
    open(me);
    return new Promise((res, rej) => {
      const fail = () => { live.delete(me); rej(new TypeError('Failed to fetch')); };
      setTimeout(() => {
        if (me.dead) return fail();
        real(input, init).then(r => { if (me.dead) return fail(); live.delete(me); res(r); }, e => { live.delete(me); rej(e); });
      }, 150);
    });
  };
  const send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function(){
    const me = {url: 'xhr', dead: false};
    open(me);
    this.addEventListener('readystatechange', () => { if (this.readyState === 4) live.delete(me); });   // before load: the answer is complete
    return send.apply(this, arguments);
  };
})();
"""


def cd_secs(c: Ctx) -> int:
    """Seconds shown by the countdown ('Sperrt in 4:48' / 'Locks in 1:02:03'), -1 if none."""
    m = re.search(r"(\d+(?::\d\d)+)$", c.text("#alCd") or "")
    if not m:
        return -1
    s = 0
    for part in m.group(1).split(":"):
        s = s * 60 + int(part)
    return s


def net_state(c: Ctx) -> dict:
    return c.ev("() => ({max: __net.max, killed: __net.killed.slice(), n: __net.n})")


@test
def t12a_one_request_at_a_time(c: Ctx):
    c.reset("crypt_enabled", crypt_kdf_ms=50)
    c.open(init=DEVICE_JS)
    c.login(ADMIN)
    to_sec(c, "#alItem", "() => !document.querySelector('#alItem').hidden && S.wifi !== null")
    c.page.wait_for_timeout(1200)
    for v in ("15", "30", "60"):
        c.clear_toasts()
        # the change and a status poll at the same moment (the race that lost the owner's setting on the device)
        c.ev("v => { const s = document.querySelector('#alSel'); s.value = v; s.dispatchEvent(new Event('change')); poll(); refresh(); }", v)
        ok = c.wait_toast("Gespeichert.", 9000)
        c.check(f"one-connection device: auto-lock {v} min saved although a status poll was due at the same moment (toast, mock stores it, no error)",
                ok and c.mock.state()["autolock"]["minutes"] == int(v) and not any(t["err"] for t in c.toasts()), f"{c.toast_texts()} {c.mock.state()['autolock']}")
    c.clear_toasts()
    c.tap("label.switch:has(#swDet)")
    ok = c.wait_toast("Gespeichert.", 9000) and c.wait("() => S.st.autolock.on_detach === true", None, 9000)
    c.check("one-connection device: the USB switch is saved too", ok and c.mock.state()["autolock"]["on_detach"] is True, c.mock.state()["autolock"])
    # settings opened again (4 loads), folders entered, an upload while the page reloads its list
    c.go("#/")
    c.wait("() => S.entries !== null", None, 9000)
    c.go("#/Projekte")
    c.wait_dir("Projekte", 9000)
    c.go("#einstellungen")
    c.wait("() => S.users !== null", None, 9000)
    c.go("#/")
    c.wait_dir("", 9000)
    c.mock.set(upload_kbps=120)
    c.page.set_input_files("#picker", str(c.run.f["txt"]))
    c.wait("() => S.ups.length && S.ups[0].state === 'up'", None, 9000)
    c.ev("() => { loadFiles(); refresh(true); }")
    done = up_done(c, 1, 20000)
    c.mock.set(upload_kbps=0)
    n = net_state(c)
    c.check("never more than one request open at a time (polls, settings loads, saves, folder changes, an upload with list reloads); nothing was cut off",
            done and n["max"] == 1 and not n["killed"], n)
    c.check("no 'not reachable' notice", not c.ui()["notice"], c.ui()["notice"])
    c.no_console_errors()
    # a single dropped connection: requests that are safe to repeat go again once, quietly
    c.start("crypt_enabled", crypt_kdf_ms=50)
    to_sec(c, "#alItem", "() => !document.querySelector('#alItem').hidden")
    hits = {"status": 0, "al": 0, "mk": 0}

    def drop_first(key):
        def h(route):
            hits[key] += 1
            if hits[key] == 1:
                route.abort("connectionreset")
            else:
                route.continue_()
        return h
    is_st = lambda u: url_path(u) == "/api/status"
    c.page.route(is_st, drop_first("status"))
    c.ev("() => refresh()")
    c.page.wait_for_timeout(1500)
    c.check("status request reset once: repeated at once, counts as success (no failure, no notice)",
            hits["status"] >= 2 and c.ev("() => S.fails") == 0 and not c.ui()["notice"], f"{hits} fails={c.ev('() => S.fails')}")
    c.page.unroute(is_st)
    c.page.route(lambda u: url_path(u) == "/api/settings/autolock", drop_first("al"))
    c.clear_toasts()
    c.page.select_option("#alSel", "30")
    ok = c.wait_toast("Gespeichert.", 9000)
    c.check("settings POST reset once: sent again (setting a value twice is harmless), saved, no error toast",
            ok and hits["al"] == 2 and c.mock.state()["autolock"]["minutes"] == 30 and not any(t["err"] for t in c.toasts()), f"{hits} {c.toast_texts()}")
    c.go("#/")
    c.wait_dir("", 9000)
    c.page.route(lambda u: url_path(u) == "/api/mkdir", drop_first("mk"))
    c.new_folder()
    c.wait_dlg("Neuer Ordner")
    c.fill_dlg("Nur einmal")
    c.dlg_yes()
    ok = c.wait_derr()
    c.check("a POST that is not safe to repeat (mkdir) is not sent twice: one try, 'Keine Antwort vom Laufwerk.' in the dialog",
            ok and hits["mk"] == 1 and c.dlg()["err"] == "Keine Antwort vom Laufwerk.", f"{hits} {c.dlg()['err']}")
    c.dlg_no()
    c.wait_dlg_closed()
    c.no_console_errors(net=True)


@test
def t12b_autolock_countdown(c: Ctx):
    c.start("crypt_enabled", crypt_kdf_ms=50)
    c.check("timer off: no countdown in the encryption card", not vis(c, "#alBox"))
    c.mock.post("/api/settings/autolock?minutes=5&on_detach=0")
    c.page.wait_for_timeout(500)
    c.mock.set(autolock_remaining=125)
    ok = c.wait("() => !document.querySelector('#alBox').hidden && /^Sperrt in 2:0\\d$/.test(document.querySelector('#alCd').textContent)", None, 9000)
    c.check("timer 5 min, 125 s left: 'Sperrt in 2:0x' in the card 'Verschlüsselt · Entsperrt', next to 'Sperren'", ok and c.text("#encSub") == "Entsperrt"
            and c.ev("() => document.querySelector('#alBox').parentNode === document.querySelector('#lockBtn').parentNode"), c.text("#alCd"))
    c.shot("v6_countdown")
    c.overflow_check("encryption card with countdown")
    s0 = cd_secs(c)
    c.page.wait_for_timeout(2300)
    s1 = cd_secs(c)
    c.check("ticks every second between the polls", 1 <= s0 - s1 <= 4, f"{s0} -> {s1}")
    b = c.ev("() => { const b = document.querySelector('#alReset'); return {tag: b.tagName, label: b.getAttribute('aria-label'), n: document.querySelectorAll('#alBox button').length}; }")
    c.check("one compact reset control: icon button 'Zeit zurücksetzen'", b == {"tag": "BUTTON", "label": "Zeit zurücksetzen", "n": 1}, b)
    n_touch = len(reqs(c, "POST", "/api/autolock/touch"))
    c.page.wait_for_timeout(6500)
    rem = c.mock.state()["autolock"]["remaining_s"]
    c.check("the page's own polling does not reset the timer (no touch sent, the device's time keeps running)",
            len(reqs(c, "POST", "/api/autolock/touch")) == n_touch == 0 and rem < 120, rem)
    c.tap("#alReset")
    ok = c.wait("() => /^Sperrt in (5:00|4:5\\d)$/.test(document.querySelector('#alCd').textContent)", None, 6000)
    c.check("'Zeit zurücksetzen': POST /api/autolock/touch, countdown back at the full 5:00", ok and len(reqs(c, "POST", "/api/autolock/touch")) == 1
            and c.mock.state()["autolock"]["remaining_s"] >= 290, c.text("#alCd"))
    c.tap("#encInfo")
    pop = c.pop()
    c.check("the card's ⓘ explains the countdown (only in file management) and leads to 'Automatisch sperren'", pop["open"] and "Zeit zurücksetzen" in pop["text"]
            and "nur in der Dateiverwaltung" in pop["text"] and pop["link"] == "#!hilfe/sperren", pop)
    c.page.keyboard.press("Escape")
    how = c.ev("() => [...L.de.helpDoc].find(x => x[0] === 'sperren')[2]")
    c.check("help 'Automatisch sperren' describes the countdown, the reset, the rule 'only in file management' and the wait while busy",
            "Countdown" in how and "Zeit zurücksetzen" in how and "Die Zeit läuft nur in der Dateiverwaltung." in how
            and "Sperrt, sobald das Laufwerk frei ist" in how and "USB-Gerät fertig" not in how, how[-160:])
    # a file action restarts the period on the device: the page shows it after the action
    c.mock.set(autolock_remaining=100)
    c.wait("() => /^Sperrt in 1:/.test(document.querySelector('#alCd').textContent)", None, 9000)
    c.new_folder()
    c.wait_dlg("Neuer Ordner")
    c.fill_dlg("Countdown Test")
    c.dlg_yes()
    ok = c.wait_dlg_closed() and c.wait("() => /^Sperrt in (5:00|4:5\\d)$/.test(document.querySelector('#alCd').textContent)", None, 6000)
    c.check("after a real file action (new folder) the countdown is resynchronised at once (full time again)", ok, c.text("#alCd"))
    # last minute: warning colour
    c.mock.set(autolock_remaining=45)
    ok = c.wait("() => document.querySelector('#alBox').classList.contains('warn')", None, 9000)
    col = c.ev("() => [getComputedStyle(document.querySelector('#alCd')).color, getComputedStyle(document.documentElement).getPropertyValue('--warn').trim()]")
    c.check("below one minute the countdown turns to the warning colour", ok and cd_secs(c) < 60, f"{c.text('#alCd')} {col}")
    c.shot("v6_countdown_warn")
    # time is up: the page asks at once and shows the locked view
    c.clear_toasts()
    c.mock.set(autolock_remaining=3)
    ok = wait_unlock_view(c, True, 9000)
    c.check("countdown reaches 0: status checked at once, the unlock view is shown, countdown gone", ok and not vis(c, "#alBox") and c.wait_toast("gesperrt", 5000), c.toast_texts())
    c.no_console_errors()
    # USB mode: the timer does not run (0.6.0), the card shows no countdown; back in file management the period starts anew
    c.start("crypt_enabled", crypt_kdf_ms=50)
    c.mock.post("/api/settings/autolock?minutes=5&on_detach=0")
    c.mock.set(autolock_remaining=100)
    c.wait("() => /^Sperrt in 1:/.test(document.querySelector('#alCd').textContent)", None, 9000)
    c.mock.set(owner="usb")
    ok = c.wait("() => S.st.drive.owner === 'usb' && document.querySelector('#alBox').hidden", None, 9000)
    st = c.mock.state()["autolock"]
    c.check("USB mode: no countdown in the encryption card (still 'Entsperrt' with 'Sperren'), the device reports remaining_s -1",
            ok and not vis(c, "#alBox") and vis(c, "#encCard") and c.text("#encSub") == "Entsperrt" and vis(c, "#lockBtn")
            and st["remaining_s"] == -1 and st["waiting"] is False, f"{c.text('#alCd')} {st}")
    c.tap("#encInfo")
    c.page.wait_for_timeout(350)
    pop = c.pop()
    c.check("USB mode: the card's ⓘ explains the encryption, not the countdown", pop["open"] and "Zeit zurücksetzen" not in pop["text"]
            and pop["link"] == "#!hilfe/verschluesselung", pop)
    c.page.keyboard.press("Escape")
    c.wait("() => document.querySelector('#pop').hidden", None, 3000)
    c.shot("v6_countdown_usb")
    c.mock.set(idle_s=400)
    c.page.wait_for_timeout(1500)
    cr = c.mock.state()["crypt"]
    c.check("USB mode: no lock by time even long after the period (mock idle_s=400 with 5 minutes)",
            not c.ev("() => isLocked()") and cr["unlocked"] is True and not vis(c, "#alBox"), cr)
    c.mock.set(owner="app")
    ok = c.wait("() => S.st.drive.owner === 'app' && /^Sperrt in (5:00|4:5\\d)$/.test(document.querySelector('#alCd').textContent)", None, 9000)
    c.check("back in file management: the countdown starts at the full 5:00", ok, c.text("#alCd"))
    c.no_console_errors()
    # time up, but the lock attempt was refused (busy)
    c.start("crypt_enabled", crypt_kdf_ms=50)
    c.mock.post("/api/settings/autolock?minutes=5&on_detach=0")
    c.mock.set(autolock_waiting=1)
    c.mock.set(autolock_remaining=0)
    ok = c.wait("() => document.querySelector('#alCd').textContent === 'Sperrt, sobald das Laufwerk frei ist'", None, 9000)
    c.check("waiting: 'Sperrt, sobald das Laufwerk frei ist' (warning colour), still unlocked, the reset stays", ok and vis(c, "#alReset")
            and c.ev("() => document.querySelector('#alBox').classList.contains('warn')") and not c.ev("() => isLocked()"), c.text("#alCd"))
    c.shot("v6_countdown_waiting")
    c.overflow_check("encryption card while the lock waits")
    c.tap("#alReset")
    ok = c.wait("() => /^Sperrt in (5:00|4:5\\d)$/.test(document.querySelector('#alCd').textContent)", None, 6000)
    c.check("reset while waiting: back to a running countdown", ok, c.text("#alCd"))
    # English
    to_sec(c, "#langSeg")
    c.tap('#langSeg button[data-v="en"]')
    c.go("#/")
    ok = c.wait("() => /^Locks in \\d+:\\d\\d$/.test(document.querySelector('#alCd').textContent)", None, 6000)
    c.check("English: 'Locks in m:ss', reset labelled 'Reset time'", ok and c.ev("() => document.querySelector('#alReset').getAttribute('aria-label')") == "Reset time", c.text("#alCd"))
    r = c.ev(JS_UNTRANSLATED)
    c.check("English file view with countdown: no German leftovers", not r["bad"], r["bad"])
    c.no_console_errors()
    # role user sees the countdown and may reset it
    c.start("crypt_enabled", who=GUEST, crypt_kdf_ms=50)
    c.mock.post("/api/settings/autolock?minutes=10&on_detach=0")
    ok = c.wait("() => !document.querySelector('#alBox').hidden", None, 9000)
    c.tap("#alReset")
    c.check("role user: countdown shown, reset allowed", ok and wait_req(c, "POST", "/api/autolock/touch") and c.wait("() => /^Sperrt in (10:00|9:5\\d)$/.test(document.querySelector('#alCd').textContent)", None, 6000),
            c.text("#alCd"))
    c.no_console_errors()


@test
def t12c_network_settings(c: Ctx):
    c.start("two_users", wifi_count=1)
    to_sec(c, "#ipItem", "() => !document.querySelector('#ipItem').hidden")
    o = c.ev("""() => ({val: document.querySelector('#ipVal').textContent, host: document.querySelector('#hostVal').textContent,
      pressed: [...document.querySelectorAll('#ipSeg button')].map(b => [b.textContent, b.getAttribute('aria-pressed')]), form: !document.querySelector('#ipForm').hidden,
      h3: [...document.querySelectorAll('#b-net h3, #b-net .kv')].filter(e => e.getClientRects().length).map(e => e.textContent.trim())})""")
    c.check("WLAN card: item 'Adresse' (current address, 'Automatisch (DHCP) | Fest', DHCP pressed), item 'Name im Netzwerk' (datadrive-mock.local)",
            o["val"] == "http://192.0.2.152" and o["host"] == "datadrive-mock.local" and o["pressed"] == [["Automatisch (DHCP)", "true"], ["Fest", "false"]] and not o["form"], o)
    c.check("no doubled values: 'Adresse' and 'Name im Netzwerk' once each", sum(x.startswith("Adresse") for x in o["h3"]) == 1
            and sum(x.startswith("Name im Netzwerk") for x in o["h3"]) == 1, o["h3"])
    c.tap('#ipSeg button[data-v="0"]')
    vals = c.ev("() => ['ipA','ipM','ipG','ipD'].map(i => document.querySelector('#' + i).value)")
    c.check("'Fest': four fields, prefilled with what DHCP assigned (current)", vals == ["192.0.2.152", "255.255.255.0", "192.0.2.1", "192.0.2.1"], vals)
    c.shot("v6_net_static_form")
    c.overflow_check("static address form")
    cases = (
        (("192.0.2.300", "255.255.255.0", "192.0.2.1", ""), "ipAe", "Bitte eine IPv4-Adresse wie 192.168.1.50 eingeben."),
        (("192.0.02.7", "255.255.255.0", "192.0.2.1", ""), "ipAe", "Bitte eine IPv4-Adresse wie 192.168.1.50 eingeben."),
        (("192.0.2.7", "255.0.255.0", "192.0.2.1", ""), "ipMe", "Ungültige Subnetzmaske"),
        (("192.0.2.7", "255.255.255.255", "192.0.2.1", ""), "ipMe", "Ungültige Subnetzmaske"),
        (("10.0.0.7", "254.0.0.0", "10.0.0.1", ""), "ipMe", "Ungültige Subnetzmaske"),
        (("192.0.2.0", "255.255.255.0", "192.0.2.1", ""), "ipAe", "Das ist die Netz- oder Broadcast-Adresse"),
        (("192.0.2.255", "255.255.255.0", "192.0.2.1", ""), "ipAe", "Das ist die Netz- oder Broadcast-Adresse"),
        (("192.0.2.7", "255.255.255.0", "192.0.3.1", ""), "ipGe", "Das Gateway liegt nicht im Netz der IP-Adresse."),
        (("192.0.2.7", "255.255.255.0", "192.0.2.7", ""), "ipGe", "Gateway und IP-Adresse müssen sich unterscheiden."),
        (("192.168.4.1", "255.255.255.0", "192.168.4.2", ""), "ipAe", "Diese Adresse gehört dem Einrichtungs-WLAN des Sticks."),
        (("192.0.2.7", "255.255.255.0", "192.0.2.1", "dns.example"), "ipDe", "Bitte eine IPv4-Adresse"),
    )
    for v, fid, msg in cases:
        for i, x in enumerate(v):
            c.page.fill(f"#{('ipA', 'ipM', 'ipG', 'ipD')[i]}", x)
        c.tap("#ipSave")
        got = c.text(f"#{fid}")
        c.check(f"validation {v}: message at the field ({fid}): {msg[:40]}", got.startswith(msg) and not c.dlg()["open"]
                and c.ev("i => document.querySelector('#' + i.slice(0, 3)).getAttribute('aria-invalid')", fid) == "true", got)
    c.check("invalid input sent nothing", not reqs(c, "POST", "/api/settings/network"))
    c.shot("v6_net_field_error")
    c.overflow_check("static address form with a field message")
    for i, x in enumerate(("192.0.2.77", "255.255.255.0", "192.0.2.1", "")):
        c.page.fill(f"#{('ipA', 'ipM', 'ipG', 'ipD')[i]}", x)
    port = urllib.parse.urlsplit(c.run.base).port
    new = f"http://192.0.2.77:{port}"
    # the new address answers through the browser (the probe image and the page itself come from the mock)
    is_new = lambda u: u.startswith(new + "/")
    c.page.route(is_new, lambda route: route.fulfill(response=route.fetch(url=route.request.url.replace(new, c.run.base))))
    c.tap("#ipSave")
    ok = c.wait_dlg("Feste Adresse übernehmen?")
    d = c.dlg()
    c.check("confirmation names the new address", ok and new in d["body"] and d["yes"] == "Übernehmen", d["body"])
    c.shot("v6_net_confirm")
    c.dlg_yes()
    ok = c.wait_dlg("Neue Adresse", 9000)
    d = c.dlg()
    q = reqs(c, "POST", "/api/settings/network")
    c.check("POST /api/settings/network dhcp=0 with ip, mask, gateway, dns (empty); then the blocking notice 'Der Stick ist jetzt unter … erreichbar' with a link",
            ok and q and q[-1] == {"dhcp": "0", "ip": "192.0.2.77", "mask": "255.255.255.0", "gateway": "192.0.2.1", "dns": ""}
            and d["body"].startswith(f"Der Stick ist jetzt unter {new} erreichbar.") and c.ev("() => document.querySelector('#dBody a').getAttribute('href')") == new + "/"
            and c.ev("() => document.querySelector('#dNo').hidden"), f"{q} {d['body']}")
    c.shot("v6_net_moved")
    st = c.mock.state()["network"]
    c.check("the mock stores the static address", st["dhcp"] is False and st["ip"] == "192.0.2.77", st)
    end = time.time() + 15
    while time.time() < end and not c.page.url.startswith(new + "/"):
        c.page.wait_for_timeout(200)
    c.check("the page checks that the new address answers (image probe) and then moves there", c.page.url.startswith(new + "/"), c.page.url)
    c.page.unroute(is_new)
    # back to DHCP; the new name cannot be reached here: the notice stays with its link
    c.start("two_users", wifi_count=1, net_dhcp=0)
    to_sec(c, "#ipItem", "() => !document.querySelector('#ipItem').hidden")
    vals = c.ev("() => [!document.querySelector('#ipForm').hidden, ...['ipA','ipM','ipG','ipD'].map(i => document.querySelector('#' + i).value)]")
    c.check("stored static address: 'Fest' pressed, form with the stored values", vals[0] and vals[1] == c.mock.state()["network"]["ip"], vals)
    c.tap('#ipSeg button[data-v="1"]')
    ok = c.wait_dlg("Adresse automatisch beziehen?")
    host = f"http://datadrive-mock.local:{port}"
    c.check("'Automatisch (DHCP)': confirmation names name.local", ok and host in c.dlg()["body"], c.dlg()["body"])
    is_name = lambda u: "datadrive-mock.local" in u
    c.page.route(is_name, lambda route: route.abort("namenotresolved"))
    c.dlg_yes()
    ok = c.wait_dlg("Neue Adresse", 9000)
    c.check("DHCP saved (dhcp=1): notice with name.local, 'Wird gesucht …', button 'Jetzt öffnen'", ok and reqs(c, "POST", "/api/settings/network")[-1] == {"dhcp": "1"}
            and c.mock.state()["network"]["dhcp"] is True and host in c.dlg()["body"] and "Wird gesucht …" in c.dlg()["body"] and c.dlg()["yes"] == "Jetzt öffnen", c.dlg()["body"])
    c.page.wait_for_timeout(5000)
    c.check("not reachable (yet): the page stays, polling stopped, no 'not reachable' notice, the link is there", c.dlg()["open"] and not c.ui()["notice"]
            and c.ev("() => location.hostname") == urllib.parse.urlsplit(c.run.base).hostname, c.ui()["notice"])
    c.page.unroute(is_name)
    c.no_console_errors(net=True)
    # network name
    c.start("two_users", wifi_count=1)
    to_sec(c, "#hostItem", "() => !document.querySelector('#hostItem').hidden")
    c.tap("#hostBtn")
    ok = c.wait_dlg("Name im Netzwerk")
    d = c.dlg()
    c.check("'Ändern …': dialog with the current name, no certificate line while HTTPS is off", ok and d["fields"][0]["value"] == "datadrive-mock"
            and d["fields"][0]["label"] == "Name (a–z, 0–9, Bindestrich)" and "Zertifikat" not in d["body"], d)
    c.shot("v6_host_dialog")
    c.overflow_check("network name dialog")
    c.check("the name field takes at most 31 characters", c.ev("() => document.querySelector('#dF0').maxLength") == 31)
    for bad in ("-werk", "werk-", "werk_statt", "wörk", "werk statt", ""):
        c.fill_dlg(bad)
        c.dlg_yes()
        c.wait_derr()
        c.check(f"name {bad!r}: rule shown", c.dlg()["err"] == "1 bis 31 Zeichen: a–z, 0–9 und Bindestrich, nicht am Anfang oder Ende." and c.dlg()["open"], c.dlg()["err"])
    c.check("invalid names sent nothing", not reqs(c, "POST", "/api/settings/network"))
    c.fill_dlg("Werkstatt-1")
    c.dlg_yes()
    ok = c.wait_dlg("Neue Adresse", 9000)
    q = reqs(c, "POST", "/api/settings/network")
    c.check("valid name (upper case allowed, sent lower case with dhcp=1); notice names werkstatt-1.local; opened by address: only 'OK', no search",
            ok and q and q[-1] == {"dhcp": "1", "hostname": "werkstatt-1"} and f"http://werkstatt-1.local:{port}" in c.dlg()["body"]
            and c.dlg()["yes"] == "OK" and "Wird gesucht" not in c.dlg()["body"], f"{q} {c.dlg()['body']}")
    c.dlg_yes()
    ok = c.wait_dlg_closed() and c.wait("() => document.querySelector('#hostVal').textContent === 'werkstatt-1.local'", None, 9000)
    c.check("the page keeps working and shows the new name", ok and c.ui()["auth"] == "ok" and not c.ev("() => !!S.moved"), c.text("#hostVal"))
    # HTTPS set up: a new name means a new certificate authority
    c.mock.set(https=1)
    c.wait("() => S.st.https === true", None, 9000)
    c.tap("#hostBtn")
    c.wait_dlg("Name im Netzwerk")
    c.check("HTTPS on: the name dialog says in one line that the certificate must be installed again", "Danach muss das Zertifikat des Sticks auf jedem Gerät neu installiert werden." in c.dlg()["body"]
            and c.ev("() => document.querySelector('#dInfo').dataset.h") == "https", c.dlg()["body"])
    # the device renews the certificate for the next start (https_restart); the mock does not report that: rewritten in the browser
    def restart_due(route):
        try:
            resp = route.fetch()
            j = resp.json()
            j["https_restart"] = True
            route.fulfill(response=resp, json=j)
        except Exception:
            try:
                route.abort()
            except Exception:
                pass
    is_st = lambda u: url_path(u) == "/api/status"
    c.page.route(is_st, restart_due)
    c.fill_dlg("werkstatt-2")
    c.dlg_yes()
    c.wait_dlg("Neue Adresse", 9000)
    c.dlg_yes()
    ok = c.wait_dlg("Jetzt neu starten?", 9000)
    c.check("after the change with HTTPS set up (https_restart): the restart is offered", ok and c.dlg()["no"] == "Später", c.dlg()["title"])
    c.dlg_no()
    c.wait_dlg_closed()
    c.page.unroute(is_st)
    how = c.ev("() => [...L.de.helpDoc].find(x => x[0] === 'wlan')[2]") + c.ev("() => [...L.de.helpDoc].find(x => x[0] === 'https')[2]")
    c.check("help: fixed address, network name, recovery with the 10-s button (not the setup network), certificate after a new name",
            "Feste Adresse" in how and "Name im Netzwerk" in how and "10 Sekunden" in how and "neue Zertifizierungsstelle" in how, how[:60])
    c.tap("#b-net [data-i=ipX]")
    pop = c.pop()
    c.check("the address ⓘ carries the recovery hint (10-s button)", pop["open"] and "10 Sekunden" in pop["text"] and pop["link"] == "#!hilfe/wlan", pop)
    c.page.keyboard.press("Escape")
    c.no_console_errors(net=True)
    # firmware without the network API: the plain values stay, no controls
    c.reset("two_users", wifi_count=1)
    c.open()
    c.page.route(lambda u: url_path(u) == "/api/network", lambda route: route.fulfill(status=404, content_type="application/json", body='{"ok":false,"code":"NOT_FOUND"}'))
    c.login(ADMIN)
    to_sec(c, "#netHost", "() => S.wifi !== null")
    c.page.wait_for_timeout(800)
    c.check("no GET /api/network (older firmware): address and name as plain values, no choice, no 'Ändern …'", not vis(c, "#ipItem") and not vis(c, "#hostItem")
            and vis(c, "#netHost") and c.text("#netHost").endswith(".local"))
    # English
    c.reset("two_users", wifi_count=1)
    c.open(locale="en-GB")
    c.login(ADMIN)
    to_sec(c, "#ipItem", "() => !document.querySelector('#ipItem').hidden")
    c.tap('#ipSeg button[data-v="0"]')
    c.page.fill("#ipA", "192.0.2.0")
    c.tap("#ipSave")
    r = c.ev(JS_UNTRANSLATED)
    c.check("English network items and field message, no German leftovers", c.text("#ipAe") == "That is the network or broadcast address — please choose another one."
            and c.ev("() => [...document.querySelectorAll('#ipSeg button')].map(b => b.textContent)") == ["Automatic (DHCP)", "Fixed"] and not r["bad"], f"{c.text('#ipAe')} {r['bad']}")
    c.no_console_errors()


@test
def t12d_signed_update(c: Ctx):
    p = c.start("two_users", wifi_count=1)
    to_sec(c, "#otaBtn")
    p.set_input_files("#otaPick", str(c.run.f["fw"]))
    c.wait_dlg("Gerätesoftware aktualisieren?")
    c.check("unsigned updates allowed: no signature line in the dialog", "signierte" not in c.dlg()["body"], c.dlg()["body"])
    c.dlg_no()
    c.wait_dlg_closed()
    c.mock.set(ota_signed=1)
    c.wait("() => S.st.ota.signed_required === true", None, 9000)
    ver = c.mock.state()["version"]
    p.set_input_files("#otaPick", str(c.run.f["fw"]))
    ok = c.wait_dlg("Gerätesoftware aktualisieren?")
    c.check("signed updates required: value line 'Nur signierte Updates · Schlüssel 4d6f636b' in the dialog", ok and c.text("#otaSig") == "Nur signierte Updates · Schlüssel 4d6f636b", c.dlg()["body"])
    c.shot("v6_ota_signed")
    c.overflow_check("update dialog with signature line")
    for sig, want in (("bad", "Die Signatur der Datei passt nicht zum Schlüssel dieses Laufwerks."), ("none", "Dieses Laufwerk nimmt nur signierte Updates an")):
        c.mock.set(ota_sig=sig)
        c.ev("() => { document.querySelector('#otaMsg').textContent = ''; }")
        if not c.dlg()["open"]:
            p.set_input_files("#otaPick", str(c.run.f["fw"]))
            c.wait_dlg("Gerätesoftware aktualisieren?")
        c.dlg_yes()
        ok = c.wait("() => document.querySelector('#otaMsg').textContent !== ''", None, 15000)
        c.check(f"422 {'BAD_SIGNATURE' if sig == 'bad' else 'UNSIGNED'}: own message under the button, no restart", ok and c.text("#otaMsg").startswith(want)
                and c.mock.state()["version"] == ver and not c.ui()["notice"], c.text("#otaMsg"))
    how = c.ev("() => [...L.de.helpDoc].find(x => x[0] === 'update')[2]")
    c.check("help 'Update' explains signed updates", "Signierte Updates" in how, how[-100:])
    c.ev("() => { pref.set('lang', 'en'); setLang('en'); }")
    c.check("English texts of both refusals", c.ev("() => [errText({code: 'BAD_SIGNATURE'}, 'ota'), errText({code: 'UNSIGNED'}, 'ota'), t('otaSig', 'abcd1234')]")
            == ["The file’s signature does not match this drive’s key. The current version stays active.",
                "This drive only accepts signed updates — the file is not signed. The current version stays active.", "Signed updates only · key abcd1234"])
    c.ev("() => { pref.set('lang', 'de'); setLang('de'); }")
    c.no_console_errors()


# ================================================================== main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Browser test of the WiFi DataDrive web UI against tools/mock_server.py.")
    ap.add_argument("--base", default="http://localhost:8090", help="URL of the running mock server")
    ap.add_argument("--shots", default=str(DEFAULT_SHOTS), help=f"screenshot folder (default {DEFAULT_SHOTS})")
    ap.add_argument("--no-shots", action="store_true", help="take no screenshots")
    ap.add_argument("--headed", action="store_true", help="show the browser window")
    ap.add_argument("--channel", default="msedge", help="installed browser to use: msedge (default) or chrome")
    ap.add_argument("--profile", default="both", choices=["desktop", "phone", "both"])
    ap.add_argument("--only", default="", help="run only tests whose name contains this text")
    ap.add_argument("--list", action="store_true", help="list the tests and exit")
    a = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
    if a.list:
        for fn in TESTS:
            print(fn.__name__)
        return 0

    shots = None if a.no_shots else Path(a.shots)
    if shots:
        shots.mkdir(parents=True, exist_ok=True)
    run = Run(a.base, shots, a.headed)
    try:
        info = run.mock.get("/mock", timeout=5)
        if not info.get("mock"):
            raise ValueError("not the mock server")
    except (OSError, ValueError, urllib.error.URLError) as e:
        print(f"ui_test: no mock server at {run.base} ({e}). Start it with: python tools/mock_server.py --port 8090",
              file=sys.stderr)
        return 2
    start_scenario = info.get("scenario", "usb_active")
    profiles = ["desktop", "phone"] if a.profile == "both" else [a.profile]
    tests = [fn for fn in TESTS if a.only in fn.__name__]
    t_start = time.time()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel=a.channel, headless=not a.headed)
            print(f"browser: {a.channel} {browser.version}, mock: {run.base}, screenshots: {shots or 'off'}", flush=True)
            for prof in profiles:
                c = Ctx(run, browser, prof)
                for fn in tests:
                    print(f"--- {prof}: {fn.__name__}", flush=True)
                    try:
                        fn(c)
                    except Exception as e:                      # a broken test must not stop the others
                        traceback.print_exc()
                        try:
                            c.shot(f"crash_{fn.__name__}")
                        except Exception:
                            pass
                        run.check(f"{prof}: {fn.__name__} ran to the end", False, f"{type(e).__name__}: {str(e).splitlines()[0][:200]}")
                    finally:
                        c.close()
            browser.close()
    finally:
        try:
            run.mock.scenario(start_scenario)                   # leave the mock as it was found
        except Exception:
            pass
        shutil.rmtree(run.tmp, ignore_errors=True)

    failed = [n for n, ok in run.results if not ok]
    print(f"\n{len(run.results) - len(failed)}/{len(run.results)} checks passed in {time.time() - t_start:.0f} s")
    if failed:
        print("Failed:")
        for n in failed:
            print("  - " + n)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
