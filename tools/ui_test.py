#!/usr/bin/env python3
"""Browser test of the WiFi DataDrive web UI (index.html, API v3 + v4) against tools/mock_server.py.

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
    --shots DIR   screenshot folder; every significant state is saved in the light and in
                  the dark colour scheme as <profile>_<scheme>_<state>.png

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
entry), the new lock-out wording and the 503 AUTH_UNAVAILABLE notice.

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
from pathlib import Path

try:
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright
except ImportError:                                   # pragma: no cover
    print("ui_test: Playwright is missing - run `pip install playwright`", file=sys.stderr)
    sys.exit(2)

_SCRATCH_SHOTS = Path(tempfile.gettempdir()) / "datadrive-ui-test" / "shots"
DEFAULT_SHOTS = _SCRATCH_SHOTS if _SCRATCH_SHOTS.parent.is_dir() else Path(tempfile.gettempdir()) / "datadrive_ui_shots"

PHONE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1")
PROFILES = {
    "desktop": dict(viewport={"width": 1280, "height": 800}),
    "phone": dict(viewport={"width": 390, "height": 844}, device_scale_factor=3, is_mobile=True,
                  has_touch=True, user_agent=PHONE_UA),
}

ADMIN = ("admin", "admin123")
GUEST = ("gast", "gast123")
T_USB = "USB-Laufwerk aktiv"
T_APP = "Dateiverwaltung aktiv"
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
  const b = q('#modeBtn'), st = q('#list .state');
  const crumbs = [...document.querySelectorAll('#crumbs .cr')].filter(vis).map(e => e.textContent.trim());
  return {
    auth: S.auth, login: vis(q('#vLogin')), pw: vis(q('#vPw')), app: vis(q('#app')), header: vis(q('header.top')),
    title: q('#modeTitle').textContent, sub: q('#modeSub').textContent, modeCls: q('#modeCell').className,
    btn: vis(b) ? b.textContent.trim() : null, btnDisabled: b.disabled,
    stoTitle: q('#stoTitle').textContent, stoSub: q('#stoSub').textContent,
    count: q('#count').textContent, h1: q('#title').textContent, crumbs, crumbsVisible: vis(q('#crumbs')),
    rows: [...document.querySelectorAll('#list .row')].map(r => r.dataset.name),
    dirs: [...document.querySelectorAll('#list .row.dir')].map(r => r.dataset.name),
    skel: document.querySelectorAll('#list .skel').length,
    stateH: st && st.querySelector('h2') ? st.querySelector('h2').textContent : null,
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
JS_TABS = r"""() => [...document.querySelectorAll('.tabs a')].filter(a => a.getClientRects().length && a.getAttribute('href') !== '#s-sec').map(a => a.textContent)"""

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

    def check(self, name: str, cond, info=""):
        ok = bool(cond)
        self.results.append((name, ok))
        info = str(info)
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

    def open(self, hash_: str = "", **ctx_kw):
        """Open the page and wait until the sign-in view (or the app, with a trusted cookie) is there."""
        self.close()
        opts = dict(PROFILES[self.prof])
        opts.update(color_scheme="light", locale="de-DE", accept_downloads=True)
        opts.update(ctx_kw)
        self.context = self.browser.new_context(**opts)
        self.context.add_init_script(INIT_JS)
        p = self.page = self.context.new_page()
        p.set_default_timeout(8000)
        self.console, self.requests = [], []
        p.on("console", lambda m: self.console.append(f"{m.type}: {m.text}") if m.type == "error" else None)
        p.on("pageerror", lambda e: self.console.append(f"pageerror: {e}"))
        p.on("request", lambda r: self.requests.append((r.method, r.url)))
        p.goto(self.run.base + "/" + hash_, timeout=30000)
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
        return self.ev(JS_MENU)

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
    c.check("file view: status strip and storage", ui["title"] == T_APP and ui["sub"] == "Das USB-Gerät sieht das Laufwerk im Moment nicht."
            and ui["btn"] == "USB-Laufwerk bereitstellen" and ui["stoTitle"] == "microSD-Karte" and "belegt" in ui["stoSub"], f"{ui['title']} / {ui['stoSub']}")
    c.check("root: 17 entries, folders first, count shown", len(ui["rows"]) == ROOT_COUNT and ui["dirs"] == ROOT_DIRS and ui["rows"][:3] == ROOT_DIRS
            and ui["count"] == str(ROOT_COUNT) and ui["h1"] == "Meine Dateien" and not ui["crumbsVisible"], f"{len(ui['rows'])} rows, {ui['dirs']}")
    c.check("hidden system folders are not listed", "System Volume Information" not in ui["rows"] and ".Trash-1000" not in ui["rows"])
    c.check("toolbar: new folder and upload (FAB on the phone)", ui["mkBtn"] and (ui["fab"] and not ui["upBtn"] if c.phone else ui["upBtn"] and not ui["fab"]),
            f"mk {ui['mkBtn']} up {ui['upBtn']} fab {ui['fab']}")
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
    c.tap("#mkBtn")
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
    if c.phone:
        c.check("empty folder on the phone: no FAB, upload button in the empty state", not c.ui()["fab"] and c.ev("() => !!document.querySelector('#emptyUp')"))
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
    c.check("folder menu starts with 'Öffnen' instead of 'Herunterladen'", c.menu()["items"][0] == "Öffnen", c.menu()["items"])
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
    c.tap("#mkBtn")
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
    dl = c.ev("() => !document.querySelector('#sDl').hidden")
    c.check("two files selected: no single-file download button", not dl)
    c.tap("#sAll")
    c.wait("() => S.sel.size === 17")
    c.check("'Alle auswählen' selects every entry of the folder", c.ui()["selCount"] == "17 ausgewählt" and c.ev("() => document.querySelector('#sAll').disabled"), c.ui()["selCount"])
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
    c.check("usb_active: status 'USB-Laufwerk aktiv', locked state without list or upload buttons",
            ui["title"] == T_USB and ui["sub"] == "Das angeschlossene USB-Gerät hat Zugriff auf die Dateien." and ui["btn"] == "Dateiverwaltung aktivieren"
            and ui["stateH"] == "Die Dateien gehören gerade dem USB-Gerät" and not ui["rows"] and not ui["mkBtn"] and not ui["fab"], f"{ui['title']} / {ui['stateH']}")
    c.shot("usb_locked")
    c.overflow_check("locked state")
    c.tap("#modeBtn")
    ok = c.wait_dlg(T_TAKEOVER)
    d = c.dlg()
    c.check("activate while the host uses the drive: take-over dialog (danger button)", ok and d["yes"] == "Trennen und fortfahren" and d["no"] == "Abbrechen" and "danger" in d["yesCls"], d["title"])
    c.shot("takeover_dialog")
    c.overflow_check("take-over dialog")
    c.dlg_no()
    c.wait_dlg_closed()
    c.check("cancel keeps USB mode", c.mock.state()["owner"] == "usb" and c.ui()["title"] == T_USB)
    c.tap("#modeBtn")
    c.wait_dlg(T_TAKEOVER)
    c.page.keyboard.press("Escape")
    c.check("Escape cancels the dialog", c.wait_dlg_closed() and c.mock.state()["owner"] == "usb")
    c.tap("#modeBtn")
    c.wait_dlg(T_TAKEOVER)
    c.dlg_yes()
    ok = c.wait("() => document.querySelector('#modeTitle').textContent === 'Dateiverwaltung aktiv' && document.querySelectorAll('#list .row').length > 0")
    c.check("confirm: file mode, list appears", ok and c.mock.state()["owner"] == "app", c.ui()["title"])
    c.shot("after_activate")
    # back to the USB device
    c.clear_toasts()
    c.tap("#modeBtn")
    ok = c.wait_toast("Das USB-Laufwerk ist bereitgestellt.")
    c.check("hand over to the USB device: toast, locked state, owner usb", ok and c.wait("() => document.querySelector('#modeTitle').textContent === 'USB-Laufwerk aktiv'")
            and c.mock.state()["owner"] == "usb" and not c.ui()["rows"], c.toast_texts())
    # busy while switching (slow device): button disabled with a spinner
    c.mock.set(delay_ms=1200)
    c.tap("#modeBtn")
    ok = c.wait("() => document.querySelector('#modeBtn').disabled && !!document.querySelector('#modeBtn .spin')", None, 4000)
    c.shot("switching_busy")
    c.check("while switching: button disabled with spinner", ok, c.ui()["btn"])
    c.wait_dlg(T_TAKEOVER, timeout=8000)
    c.mock.set(delay_ms=0)
    c.dlg_no()
    # no host connected / ejected: no dialog
    for label, scen, sets in (("host ejected", "usb_active", dict(host_ejected=1, owner="usb")), ("no USB device", "usb_no_host", {})):
        c.start(scen, **sets)
        c.tap("#modeBtn")
        ok = c.wait("() => document.querySelector('#modeTitle').textContent === 'Dateiverwaltung aktiv'")
        c.check(f"activate with {label}: no dialog, file mode", ok and not c.ev("() => window.__dialogs.length") and c.mock.state()["owner"] == "app")
    # host writing
    c.start("usb_active", host_writing="hold")
    c.wait("() => document.querySelector('#modeSub').textContent.includes('schreibt')")
    c.check("host writing: status line says so", c.text("#modeSub") == "Das USB-Gerät schreibt gerade auf das Laufwerk.", c.text("#modeSub"))
    c.tap("#modeBtn")
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
    for scen, title in (("no_fs", "Speicher nicht lesbar"), ("empty", T_APP), ("flash_medium", T_APP)):
        c.start(scen)
        ui = c.ui()
        extra = ui["stateH"] == "Der Speicher muss eingerichtet werden" if scen == "no_fs" else ui["stateH"] == "Noch keine Dateien" if scen == "empty" else ui["stoTitle"] == "Interner Speicher"
        c.check(f"scenario {scen}: status and list state", ui["title"] == title and extra, f"{ui['title']} / {ui['stateH']} / {ui['stoTitle']}")
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
    c.check("admin sees all section chips", tabs == ["Allgemein", "Vertraute Geräte", "Netzwerk", "Speicher & USB", "Benutzer", "System"], tabs)
    c.check("account line", c.text("#acctX") == "Angemeldet als admin (Administrator).", c.text("#acctX"))
    c.shot("settings_top")
    c.shot("settings_full", full=True)
    c.overflow_check("settings")
    nm = c.ev(JS_NAMES)
    c.check("settings: every visible control has an accessible name", not nm["bad"], f"{nm['n']} controls {nm['bad'][:3]}")
    c.ev("() => scrollTo(0, 0)")
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
    c.check("storage section filled", c.text("#suMedium").startswith("microSD-Karte") and "von" in c.text("#suUsed") and c.text("#suFree") != "–", c.text("#suMedium"))
    c.tap("label.switch:has(#swWrite)")
    ok = c.wait_toast("darf nur noch lesen")
    c.check("'USB-Gerät darf Dateien ändern' off: API called, toast, mock follows", ok and c.mock.state()["host_writable"] is False, c.toast_texts())
    c.tap("label.switch:has(#swAuto)")
    c.check("'Nach dem Hochladen wieder bereitstellen' is stored per browser", c.ev("() => localStorage.getItem('dd-auto')") == "0" and not c.ev("() => document.querySelector('#swAuto').checked"))
    # system section
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
    c.tap("#fmtBtn")
    c.wait_dlg("Speicher formatieren?")
    c.fill_dlg("falsch")
    c.dlg_yes()
    ok = c.wait_derr()
    c.check("format: needs the exact word LÖSCHEN", ok and c.dlg()["err"] == "Bitte genau das Wort LÖSCHEN eingeben.", c.dlg()["err"])
    c.shot("format_dialog")
    c.dlg_no()
    c.wait_dlg_closed()
    c.check("danger zone has the maintenance mode button", c.text("#flashBtn") == "Starten …")
    # Wi-Fi section
    c.ev("() => document.querySelector('#s-net').scrollIntoView()")
    n0 = c.ev("() => document.querySelectorAll('#wifiList .li').length")
    c.check("saved networks: 3 listed, the active one marked, count text", n0 == 3 and c.ev("() => document.querySelectorAll('#wifiList .pill').length") == 1
            and c.text("#wifiCount") == "3 von 5 gespeichert", c.text("#wifiCount"))
    active = c.ev("() => document.querySelector('#wifiList .pill').closest('.li').querySelector('[data-wdel]').dataset.wdel")
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
    # change own password via the settings button
    c.ev("() => scrollTo(0, 0)")
    c.tap("#pwBtn")
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
    c.ev("() => document.querySelector('#s-sys').scrollIntoView()")
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
    c.check("role user: file management works (list, folders, new folder, upload)", ui["app"] and ui["title"] == T_APP and len(ui["rows"]) >= 3 and ui["mkBtn"] and (ui["fab"] or ui["upBtn"]), f"{ui['title']} {len(ui['rows'])} rows")
    c.check("avatar shows the user", c.text("#userAv") == "g")
    c.shot("files_user_role")
    c.tap("#userBtn")
    c.wait("() => !document.querySelector('#menu').hidden")
    c.check("account menu shows the role 'Benutzer'", "Benutzer" in c.menu()["head"] and "gast" in c.menu()["head"], c.menu()["head"])
    c.page.keyboard.press("Escape")
    c.go("#einstellungen")
    c.wait("() => !document.querySelector('#vSettings').hidden")
    c.wait("() => document.querySelector('#devList .li') !== null")
    vis = c.ev("() => ['#s-gen','#s-dev','#s-net','#s-usb','#s-users','#s-sys','#s-danger'].map(s => document.querySelector(s).getClientRects().length > 0)")
    c.check("settings for a user: only General, Trusted devices, Storage & USB", vis == [True, True, False, True, False, False, False], vis)
    tabs = c.ev(JS_TABS)
    c.check("section chips for a user", tabs == ["Allgemein", "Vertraute Geräte", "Speicher & USB"], tabs)
    shown = c.ev("() => ['#swWrite', '#wifiAdd', '#userAdd', '#rebootBtn', '#logBtn', '#otaBtn', '#fmtBtn', '#flashBtn', '#wifiList', '#userList'].map(s => document.querySelector(s).getClientRects().length > 0)")
    c.check("no admin control visible (USB write switch, Wi-Fi, users, restart, log, update, format, maintenance)", not any(shown), shown)
    c.check("account line says 'Benutzer'", c.text("#acctX") == "Angemeldet als gast (Benutzer).", c.text("#acctX"))
    c.check("the user's own settings work: auto switch and theme", c.ev("() => document.querySelector('#swAuto').getClientRects().length > 0") and c.ev("() => document.querySelector('#theme').getClientRects().length > 0"))
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
    c.check("German sign-in view: no foreign or raw key texts", not r["bad"] and r["n"] > 6, r)
    c.tap("#loginForm [data-lang]")
    c.wait("() => lang === 'en'")
    r = c.ev(JS_UNTRANSLATED)
    texts = c.ev("() => ({u: document.querySelector('label[for=lUser]').textContent, btn: document.querySelector('#loginBtn').textContent, t: document.querySelector('#lTrust').nextElementSibling.firstElementChild.textContent, "
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
    c.check("language kept after sign-in: English file view", ui["title"] == "File management active" and ui["h1"] == "My files" and ui["btn"] == "Hand drive to USB device", f"{ui['title']} / {ui['h1']}")
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

    dialog("mkdir", lambda: c.tap("#mkBtn"), "New folder")
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
    r = c.ev(JS_UNTRANSLATED)
    c.check("English settings: no German leftovers or raw keys", not r["bad"] and r["n"] > 40, r)
    tabs = c.ev(JS_TABS)
    c.check("English section chips", tabs == ["General", "Trusted devices", "Network", "Storage & USB", "Users", "System"], tabs)
    c.shot("settings_en", full=True)
    c.overflow_check("settings in English")
    pressed = c.ev("() => [...document.querySelectorAll('#langSeg button')].map(b => b.getAttribute('aria-pressed'))")
    c.check("language buttons show English selected", pressed == ["false", "true"], pressed)
    c.check("English devices texts", "slots used" in c.text("#devCount") and "Remove" in c.ev("() => document.querySelector('#devList [data-ddel]').getAttribute('aria-label')"), c.text("#devCount"))
    for label, opener, part in (("wifi", lambda: c.tap("#wifiAdd"), "Add network"), ("user", lambda: c.tap("#userAdd"), "Add user"), ("pw", lambda: c.tap("#pwBtn"), "Change password"),
                                ("format", lambda: c.tap("#fmtBtn"), "Format the storage?"), ("restart", lambda: c.tap("#rebootBtn"), "Restart the drive?")):
        opener()
        ok = c.wait_dlg(part)
        r = c.ev(JS_UNTRANSLATED)
        c.check(f"English dialog {label}: '{c.dlg()['title']}', no foreign or raw texts", ok and not r["bad"], r["bad"])
        c.shot("dlg_en_" + label)
        c.dlg_no()
        c.wait_dlg_closed()
    c.tap('[data-umore="gast"]')
    c.wait("() => !document.querySelector('#menu').hidden")
    m = c.menu()
    c.check("English user menu", m["items"] == ["Make administrator", "Set new password", "Delete"], m["items"])
    c.page.keyboard.press("Escape")
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
    c.tap('#langSeg button[data-v="de"]')
    c.wait("() => lang === 'de'")
    r = c.ev(JS_UNTRANSLATED)
    c.check("back to German via the settings: German texts, no English leftovers", c.ui()["lang"] == "de" and c.text("#acctX").startswith("Angemeldet als") and not r["bad"], r["bad"])
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
        res = c.ev(JS_TARGETS, ["#userBtn", "#navSettings", "#modeBtn", "#fab", "#mkBtn", "#list .row [data-more]", "#sortM"])
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
                c.check(f"touch target {r['sel']} at least 40x40 px", r["w"] >= 40 and r["h"] >= 40, f"{r['w']}x{r['h']}")
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
        c.shot(f"w{vw}_settings")
    if not c.phone:
        c.reset("files")
        c.open("", viewport={"width": 1920, "height": 1080})
        c.login(ADMIN)
        w = c.ev("() => document.querySelector('#app').getBoundingClientRect().width")
        c.check("wide window: content column limited to 960 px", w <= 960 + 1, w)
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
    lockBtn: vis(q('#lockBtn')), encTag: vis(q('#encTag')), encTagTxt: q('#encTag').textContent,
    modeTitle: q('#modeTitle').textContent, modeSub: q('#modeSub').textContent, modeBtn: vis(q('#modeBtn')),
    stoSub: q('#stoSub').textContent, stateH: q('#list .state h2') ? q('#list .state h2').textContent : null,
    hint: q('#unlock p.small:not([hidden])').textContent, forgot: vis(q('#ulForgot')), forgotTxt: q('#ulForgot').textContent, lost: vis(q('#ulLost')), lostTxt: q('#ulLost').textContent, focus: document.activeElement ? document.activeElement.id : ''};
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
    c.go("#einstellungen")
    c.wait("() => !document.querySelector('#vSettings').hidden")
    c.wait("() => !!document.querySelector('#cryptItem') && !document.querySelector('#cryptItem').hidden")
    c.ev("() => document.querySelector('#cryptItem').scrollIntoView()")


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
    c.check("locked drive: status strip says so, no switch button, no 'format' offer", u["modeTitle"] == "Laufwerk gesperrt"
            and u["modeSub"] == "Das USB-Gerät sieht kein Laufwerk, bis es entsperrt ist." and not u["modeBtn"] and u["stateH"] is None, f"{u['modeTitle']} / {u['modeSub']} / {u['stateH']}")
    c.check("locked drive: storage line without size, 'verschlüsselt' tag, no lock button", u["stoSub"] == "Größe erst nach dem Entsperren sichtbar"
            and u["encTag"] and u["encTagTxt"] == "verschlüsselt" and not u["lockBtn"], f"{u['stoSub']} {u['encTagTxt']}")
    c.check("unlock form: drive password field (password type), 'Entsperren', recovery key link, hint", u["lbl"] == "Laufwerks-Passwort" and u["ftype"] == "password"
            and u["btn"] == "Entsperren" and u["mode"] == "Stattdessen den Wiederherstellungsschlüssel verwenden" and u["eye"]
            and "auch der Drucker oder PC kein Laufwerk" in u["hint"] and not u["msg"] and not u["forgot"]
            and u["lost"] and u["lostTxt"] == "Passwort und Wiederherstellungsschlüssel verloren?", u)
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
            c.check(f"touch target {t['sel']} at least 40x40 px" if t["sel"] != "#ulMode" else "touch target #ulMode (link) at least 40 px high",
                    not t.get("missing") and (t["h"] >= 40 and (t["w"] >= 40 or t["sel"] == "#ulMode")), t)
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
    if u["btnDisabled"]:
        c.check("3rd wrong input: the text says how long to wait (5 s), button disabled", u["msg"].startswith(WRONG_PW_DE + " Nächster Versuch in ") and 3 <= secs(u["msg"]) <= 5, u["msg"])
    else:     # the UI would show the wait if the 403 carried Retry-After; the mock (and docs/12_api_v4.md) only send it with the 429
        c.run.info("3rd wrong input: the 403 carries no Retry-After, so the page shows no wait yet (the mock sends it only with the 429)")
        c.check("3rd wrong input: plain wrong-password text (the wait shows with the next answer, the 429)", u["msg"] == WRONG_PW_DE, u["msg"])
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
    c.check("recovery key mode: label, text field, switch-back link, no eye, 'Passwort vergessen' help; waiting text stays", u["lbl"] == "Wiederherstellungsschlüssel" and u["ftype"] == "text"
            and u["mode"] == "Stattdessen das Laufwerks-Passwort verwenden" and not u["eye"] and u["forgot"] and "Passwort vergessen?" in u["forgotTxt"]
            and "als bisheriges Passwort dient dabei der Wiederherstellungsschlüssel" in u["forgotTxt"] and u["msg"].startswith("Zu viele falsche Eingaben.") and u["btnDisabled"], u)
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
            ok and st["owner"] == "usb" and st["crypt"]["unlocked"] and u["modeTitle"] == T_USB and u["stateH"] == "Die Dateien gehören gerade dem USB-Gerät"
            and not u["unlock"], f"{st['owner']} {u['modeTitle']} {u['stateH']}")
    c.check("after unlocking: lock button and 'verschlüsselt' tag are there, form reset to the password", u["lockBtn"] and u["encTag"] and u["lbl"] == "Laufwerks-Passwort"
            and u["fval"] == "" and u["stoSub"] != "Größe erst nach dem Entsperren sichtbar", u)
    c.shot("unlocked_usb_view")
    c.overflow_check("unlocked, USB view")
    c.no_console_errors()
    # password unlock, no USB host: file management takes the drive and the list appears
    c.start("crypt_locked", usb_connected=0, crypt_kdf_ms=50)
    u = ul(c)
    c.check("locked without a USB host: status line without the USB sentence", u["modeSub"] == "Die Dateien sind erst nach dem Entsperren zugänglich.", u["modeSub"])
    c.page.fill("#ulPass", DEF_PW)
    if c.phone:
        c.tap("#ulBtn")
    else:
        c.page.press("#ulPass", "Enter")
    ok = wait_unlock_view(c, False) and c.wait("() => S.st.drive.owner === 'app' && S.entries !== null && document.querySelectorAll('#list .row').length > 0")
    u, ui = ul(c), c.ui()
    c.check("unlock with the drive password (Enter / tap): toast, file list is back, toolbar there, owner app",
            ok and c.wait_toast("Das Laufwerk ist entsperrt.") and ui["title"] == T_APP and ui["mkBtn"] and len(ui["rows"]) > 0 and c.mock.state()["owner"] == "app", f"{ui['title']} {len(ui['rows'])}")
    c.check("unlocked in file mode: lock button visible next to the storage line", u["lockBtn"] and u["encTag"], u)
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
    c.check("423 DRIVE_LOCKED on a file request: unlock view, list gone, toast", ok and not u["list"] and c.wait_toast(T_LOCKED_DONE) and u["modeTitle"] == "Laufwerk gesperrt", c.toast_texts())
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
    c.tap("#mkBtn")
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
            c.text("#cState") == "Ein, das Laufwerk ist gesperrt." and "cUnlock" in crypt_btns(c) and "cLock" not in crypt_btns(c) and c.ev("() => document.querySelector('#fmtBtn').disabled")
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
    ok = c.wait_dlg_closed() and c.wait("() => document.querySelector('#cState').textContent === 'Ein, das Laufwerk ist gesperrt.'")
    c.check("lock from the settings: section shows 'gesperrt' and offers 'Entsperren'", ok and crypt_btns(c) == ["cUnlock", "cOff"], crypt_btns(c))
    c.no_console_errors()


@test
def t09d_crypt_setup(c: Ctx):
    c.start("usb_active", crypt_kdf_ms=600)
    to_settings(c)
    c.check("unencrypted: section says 'Aus' and offers only 'Einrichten'", c.text("#cState").startswith("Aus. Wer die Speicherkarte entnimmt") and crypt_btns(c) == ["cSetup"], f"{c.text('#cState')[:30]} {crypt_btns(c)}")
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
            and "Alle Dateien auf" in d["body"] and "endgültig verloren" in d["body"] and d["focus"] == "dF0" and not r["bad"], f"{d['fields']} {r['bad']}")
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
    c.wait("() => document.querySelector('#cState').textContent.startsWith('Ein, das Laufwerk ist entsperrt.')")
    c.check("key confirmed: dialog closes, toast, section shows 'Ein, entsperrt' with lock / change password / turn off", ok and crypt_btns(c) == ["cLock", "cPw", "cRec", "cOff"]
            and c.text("#cState") == "Ein, das Laufwerk ist entsperrt. Ohne Laufwerks-Passwort ist der Speicher nicht lesbar.", f"{crypt_btns(c)} {c.text('#cState')}")
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
            and "Wiederherstellungsschlüssel bleibt gültig" in d["body"] and d["yes"] == "Speichern" and not r["bad"], f"{d['fields']} {r['bad']}")
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
    c.check("change dialog explains the forgotten-password route (recovery key in the first field)", "Passwort vergessen? Dann ins erste Feld den Wiederherstellungsschlüssel eintragen." in c.dlg()["body"], c.dlg()["body"][-120:])
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
    c.wait("() => document.querySelector('#cState').textContent.startsWith('Aus.')")
    st = c.mock.state()
    c.check("turned off: unencrypted, drive empty, section offers 'Einrichten', tag and lock button gone", ok and not st["crypt"]["enabled"] and st["files"] == [] and crypt_btns(c) == ["cSetup"]
            and not ul(c)["encTag"] and not ul(c)["lockBtn"], f"{st['crypt']} {crypt_btns(c)}")
    c.no_console_errors()


@test
def t09f_crypt_role_user(c: Ctx):
    c.start("two_users", who=GUEST)
    u = ul(c)
    c.check("role user, unencrypted drive: no lock button, no tag", not u["lockBtn"] and not u["encTag"])
    to_settings(c)
    c.check("role user, unencrypted: the section is read-only (no setup, no buttons)", crypt_btns(c) == [] and c.text("#cState").startswith("Aus."), crypt_btns(c))
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
    c.ev("() => document.querySelector('#s-net').scrollIntoView()")
    c.tap("#wifiAdd")
    ok = c.wait_dlg("WLAN hinzufügen")
    s = c.ev(JS_SCAN)
    d = c.dlg()
    c.check("scan dialog while searching: spinner text, 'Erneut suchen' disabled, polling paused",
            ok and s["spin"] and s["msg"] == "Suche nach WLANs …" and s["againDisabled"] and s["scanning"], s)
    c.check("dialog still has name and password fields and the hint for hidden networks", [f["type"] for f in d["fields"]] == ["text", "password"]
            and "WLANs in Reichweite" in d["body"] and "Verstecktes WLAN oder nicht in der Liste?" in d["body"] and "Erneut suchen" in d["body"], d["body"][:120])
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
            and u["modeTitle"] == "Drive locked" and u["modeSub"] == "The USB device sees no drive until it is unlocked." and u["stoSub"] == "Size visible after unlocking" and u["encTagTxt"] == "encrypted", u)
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
    c.check("English encryption section: texts and buttons", c.text("#cState") == "On, the drive is unlocked. Without the drive password the storage can’t be read." and crypt_btns(c) == ["cLock", "cPw", "cRec", "cOff"]
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
            and "Ohne Laufwerks-Passwort und ohne Wiederherstellungsschlüssel" in d["body"] and "endgültig verloren" in d["body"] and not r["bad"], f"{d['fields']} {r['bad']}")
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
      return {dmg: vis(q('#damaged')), unlock: vis(q('#unlock')), h2: q('#damaged h2').textContent, who: q('#dmgWho').textContent, btn: vis(q('#dmgErase')) ? q('#dmgErase').textContent : null,
        title: q('#modeTitle').textContent, sub: q('#modeSub').textContent, sto: q('#stoSub').textContent, lock: vis(q('#lockBtn'))}; }""")
    r = c.ev(JS_UNTRANSLATED)
    c.check("damaged key area: its own view (no unlock form), what happened, what is left (admin), status strip and storage line",
            dm["dmg"] and not dm["unlock"] and dm["h2"] == "Schlüsselbereich des Laufwerks beschädigt" and dm["who"].startswith("Das Laufwerk lässt sich nur noch löschen und neu formatieren")
            and dm["btn"] == "Laufwerk löschen und neu formatieren" and dm["title"] == "Laufwerk beschädigt" and dm["sub"] == "Der Schlüsselbereich ist nicht lesbar."
            and dm["sto"] == "Dateien nicht lesbar" and not dm["lock"] and not r["bad"], f"{dm} {r['bad']}")
    c.shot("crypt_damaged")
    c.overflow_check("damaged view")
    to_settings(c)
    c.check("damaged, settings: state text, only 'Löschen und neu formatieren', format disabled with the pointer",
            c.text("#cState") == "Ein, aber der Schlüsselbereich ist beschädigt – die Dateien sind nicht lesbar." and crypt_btns(c) == ["cErase"]
            and c.ev("() => document.querySelector('#fmtBtn').disabled") and "Bei beschädigtem Schlüsselbereich" in c.text("#fmtSub"), f"{c.text('#cState')} {crypt_btns(c)}")
    c.shot("settings_crypt_damaged")
    c.overflow_check("settings, damaged")
    c.tap("#cErase")
    ok = c.wait_dlg("Laufwerk löschen und neu formatieren?")
    c.check("erase from the settings (damaged): wording for a damaged key area", ok and "Der Schlüsselbereich ist beschädigt, die Dateien sind nicht mehr lesbar." in c.dlg()["body"], c.dlg()["body"][:80])
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
