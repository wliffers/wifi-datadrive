#!/usr/bin/env python3
"""Browser test of the WiFi DataDrive web UI (index.html) against tools/mock_server.py.

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

    --only TEXT   run only the tests whose name contains TEXT (e.g. --only upload)
    --shots DIR   screenshot folder; every significant state is saved in the light and in
                  the dark colour scheme as <profile>_<scheme>_<state>.png

What it does
------------
Every test resets the mock (`/mock/scenario?name=...`) and opens a fresh browser context,
so the tests are independent. Each flow runs in two profiles:
    desktop  1280x800
    phone    390x844, device_scale_factor 3, is_mobile, has_touch, iPhone Safari user agent
The contexts are created with color_scheme="light"; for screenshots the scheme is switched
with page.emulate_media() so both themes show exactly the same state.

Covered: initial load of all mock scenarios, switching between USB drive and file
management, uploads (picker, queue, cancel, errors, drag and drop), row menu / download /
rename / delete, search and sorting, settings (Wi-Fi, switches, theme, log, restart,
format, maintenance mode), connection loss and slow answers, accessibility basics
(names, focus, touch targets, contrast) and a few measurements (size, load and render
times, JS heap).

The page is instrumented only by an init script that records toasts, dialog openings and
the time the file list appears; index.html itself is not modified.
"""
from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from decimal import ROUND_HALF_UP, Decimal
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

LONG_NAME = ("Überlanger Dateiname für die Größenprüfung der Oberfläche, "
             "Lüfterhaube mit Düsenkühlung, Maße geändert v7.3mf")
NAME_ORDER = ["Anleitung Drucker.pdf", "Benchy.3mf", "firmware_backup.bin", "Foto Druckbett.jpg",
              "Getriebe_Gehäuse.gcode", "Halterung Kabelkanal v2.3mf", "KURZNAME.GCO", "LEER.TXT",
              "notizen.txt", "Projektarchiv 2026.zip", "README", LONG_NAME, "Video Zeitraffer.mp4",
              "Würfel.3mf"]

T_USB = "USB-Laufwerk aktiv"
T_APP = "Dateiverwaltung aktiv"
T_TAKEOVER = "Laufwerk vom USB-Gerät trennen?"

# ------------------------------------------------------------------ page instrumentation

INIT_JS = r"""
window.__toasts = []; window.__dialogs = []; window.__rowsAt = 0; window.__rowsPainted = 0;
(() => {
  const sm = HTMLDialogElement.prototype.showModal;
  HTMLDialogElement.prototype.showModal = function(){
    const t = this.querySelector('#dTitle');
    window.__dialogs.push({t: performance.now(), title: t ? t.textContent : '', wasOpen: this.open});
    return sm.apply(this, arguments);
  };
  document.addEventListener('DOMContentLoaded', () => {
    const box = document.getElementById('toasts');
    if (box) new MutationObserver(ms => { for (const m of ms) for (const n of m.addedNodes)
      window.__toasts.push({t: performance.now(), text: n.textContent, err: n.classList.contains('err')}); })
      .observe(box, {childList: true});
    const list = document.getElementById('list');
    if (list) { const mo = new MutationObserver(() => {
      if (!window.__rowsAt && list.querySelector('.row')) {
        window.__rowsAt = performance.now(); mo.disconnect();
        requestAnimationFrame(() => requestAnimationFrame(() => { window.__rowsPainted = performance.now(); }));
      } }); mo.observe(list, {childList: true}); }
  });
})();
"""

JS_UI = r"""() => {
  const q = s => document.querySelector(s);
  const vis = el => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
  const b = q('#modeBtn'), st = q('#list .state');
  return {
    title: q('#modeTitle').textContent, sub: q('#modeSub').textContent, modeCls: q('#modeCell').className,
    btn: vis(b) ? b.textContent.trim() : null, btnPrimary: b.classList.contains('primary'),
    btnDisabled: b.disabled, btnSpin: !!b.querySelector('.spin'),
    stoTitle: q('#stoTitle').textContent, stoSub: q('#stoSub').textContent,
    barCls: q('#stoBar').className, barW: q('#stoBar i').style.width,
    count: q('#count').textContent, rows: document.querySelectorAll('#list .row').length,
    skel: document.querySelectorAll('#list .skel').length,
    stateH: st && st.querySelector('h2') ? st.querySelector('h2').textContent : null,
    stateP: st && st.querySelector('p') ? st.querySelector('p').textContent : null,
    hints: [...document.querySelectorAll('#list .hint')].map(h => h.textContent),
    upBtn: vis(q('#upBtn')), upBtnPrimary: q('#upBtn').classList.contains('primary'), fab: vis(q('#fab')),
    search: vis(q('#search')), sortM: vis(q('#sortM')), cols: vis(q('#list .cols')),
    lockedUp: vis(q('#lockedUp')), emptyUp: vis(q('#emptyUp')),
    settingsLink: vis(q('#list .state a[href="#einstellungen"]')),
    notice: q('#notice').hidden ? '' : q('#notice').textContent,
    files: !q('#vFiles').hidden, settings: !q('#vSettings').hidden, hash: location.hash,
    dlgOpen: q('#dlg').open
  };
}"""

JS_PANEL = r"""() => {
  const q = s => document.querySelector(s), box = q('#uploads');
  return {
    hidden: box.hidden, head: q('#upHead').textContent, closeVisible: !q('#upClose').hidden, working: S.working,
    items: [...document.querySelectorAll('#upList li')].map(li => ({
      name: li.querySelector('.n').textContent, s: li.querySelector('.l1 .s').textContent.trim(),
      err: li.querySelector('.s.err') ? li.querySelector('.s.err').textContent : '',
      bar: li.querySelector('.bar>i') ? parseFloat(li.querySelector('.bar>i').style.width) : null,
      live: !!li.querySelector('[data-cancel]') })),
    toasts: window.__toasts.map(t => t.text), dlg: q('#dlg').open ? q('#dTitle').textContent : ''
  };
}"""

JS_DLG = r"""() => {
  const q = s => document.querySelector(s), d = q('#dlg'), i = q('#dInput');
  return {open: d.open, title: q('#dTitle').textContent, body: q('#dBody').textContent, err: q('#dErr').textContent,
    yes: q('#dYes').textContent, no: q('#dNo').textContent, yesCls: q('#dYes').className,
    yesDisabled: q('#dYes').disabled, noDisabled: q('#dNo').disabled,
    input: q('#dInputWrap').hidden ? null : i.value, sel: [i.selectionStart, i.selectionEnd],
    label: q('#dLabel').textContent, focus: document.activeElement ? document.activeElement.id : ''};
}"""

JS_ROWS = r"""() => [...document.querySelectorAll('#list .row')].map(r => ({name: r.dataset.name,
  size: r.querySelector('.num').textContent, date: r.querySelector('.date').textContent,
  meta: r.querySelector('.meta').textContent}))"""

JS_MENU = r"""() => {
  const m = document.querySelector('#menu'), s = document.querySelector('#scrim'), r = m.getBoundingClientRect();
  const h = m.querySelector('.mhead'), a = document.activeElement;
  return {hidden: m.hidden, rect: {l: r.left, t: r.top, r: r.right, b: r.bottom, w: r.width, h: r.height},
    vw: innerWidth, vh: innerHeight, items: [...m.querySelectorAll('[data-act]')].map(b => b.textContent.trim()),
    head: h && h.getClientRects().length ? h.textContent : null,
    focusInMenu: !!a && m.contains(a), focusText: a ? a.textContent.trim() : '',
    scrimBg: getComputedStyle(s).backgroundColor, scrimHidden: s.hidden,
    openRows: document.querySelectorAll('#list .row.open').length};
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
    const lb = el.getAttribute('aria-labelledby');
    if (lb) { const t = lb.split(/\s+/).map(i => (document.getElementById(i) || {}).textContent || '').join(' ').trim(); if (t) return t; }
    if (el.matches('input,select,textarea')) {
      if (el.id) { const l = document.querySelector('label[for="' + el.id + '"]'); if (l && l.textContent.trim()) return l.textContent.trim(); }
      const wl = el.closest('label'); if (wl && wl.textContent.trim()) return wl.textContent.trim();
      return (el.getAttribute('title') || '').trim();
    }
    return el.textContent.trim() || (el.getAttribute('title') || '').trim();
  };
  const bad = []; let n = 0;
  for (const el of document.querySelectorAll('button,a[href],input:not([type=hidden]),select,[role=button],[role=menuitem]')) {
    if (!vis(el)) continue; n++;
    if (!name(el)) bad.push(el.outerHTML.slice(0, 100));
  }
  return {n, bad};
}"""

JS_FOCUS = r"""() => {
  const el = document.activeElement;
  if (!el || el === document.body) return null;
  const on = s => s.outlineStyle !== 'none' && parseFloat(s.outlineWidth) > 0;
  const cs = getComputedStyle(el);
  let how = on(cs) ? 'outline' : '';
  if (!how && el.matches('.switch input') && on(getComputedStyle(el.nextElementSibling))) how = 'outline on switch';
  if (!how && el.matches('.field')) {
    const probe = document.createElement('span'); probe.style.color = 'var(--accent)'; document.body.appendChild(probe);
    const acc = getComputedStyle(probe).color; probe.remove();
    if (cs.borderTopColor === acc && parseFloat(cs.borderTopWidth) > 0) how = 'border colour';
  }
  const d = el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + (el.className && typeof el.className === 'string' ? '.' + el.className.trim().replace(/\s+/g, '.') : '');
  return {el: d, text: (el.getAttribute('aria-label') || el.textContent || '').trim().slice(0, 30), how};
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

# WCAG contrast of the text colour against the composited background, from computed styles.
JS_CONTRAST = r"""(sels) => {
  const cv = document.createElement('canvas'); cv.width = cv.height = 1;
  const g = cv.getContext('2d', {willReadFrequently: true});
  const lum = c => { const f = v => { v /= 255; return v <= .03928 ? v / 12.92 : Math.pow((v + .055) / 1.055, 2.4); };
    return .2126 * f(c[0]) + .7152 * f(c[1]) + .0722 * f(c[2]); };
  const px = () => [...g.getImageData(0, 0, 1, 1).data].slice(0, 3);
  const res = [];
  for (const sel of sels) {
    const el = [...document.querySelectorAll(sel)].find(e => e.getClientRects().length);
    if (!el) { res.push({sel, missing: true}); continue; }
    const chain = []; for (let p = el; p; p = p.parentElement) chain.unshift(p);
    g.globalAlpha = 1; g.fillStyle = '#fff'; g.fillRect(0, 0, 1, 1);
    for (const p of chain) { g.fillStyle = getComputedStyle(p).backgroundColor; g.fillRect(0, 0, 1, 1); }
    const bg = px();
    g.fillStyle = getComputedStyle(el).color; g.fillRect(0, 0, 1, 1);
    const fg = px(), a = lum(bg), b = lum(fg);
    res.push({sel, ratio: Math.round((Math.max(a, b) + .05) / (Math.min(a, b) + .05) * 100) / 100,
      fg: 'rgb(' + fg + ')', bg: 'rgb(' + bg + ')', px: getComputedStyle(el).fontSize});
  }
  return res;
}"""


# ------------------------------------------------------------------ helpers

def fsize(b) -> str:
    """Same formatting as fsize() of the UI (de-DE, at most one decimal)."""
    units = ["B", "kB", "MB", "GB", "TB"]
    i, b = 0, float(b)
    while b >= 1024 and i < 4:
        b /= 1024
        i += 1
    q = Decimal(b).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    s = f"{q:,.1f}".replace(",", "\x00").replace(".", ",").replace("\x00", ".")
    if s.endswith(",0"):
        s = s[:-2]
    return f"{s} {units[i]}"


def make_3mf(size_kb: int) -> bytes:
    """A minimal valid ZIP (3MF container) with incompressible payload."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("3D/3dmodel.model", os.urandom(size_kb * 1024))
    return buf.getvalue()


class Mock:
    def __init__(self, base: str):
        self.base = base

    def get(self, path: str, timeout: float = 90):
        with urllib.request.urlopen(self.base + path, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def scenario(self, name: str):
        return self.get("/mock/scenario?name=" + urllib.parse.quote(name))

    def set(self, **kw):
        return self.get("/mock/set?" + urllib.parse.urlencode(kw))

    def state(self) -> dict:
        return self.get("/mock/state")

    def file(self, name: str):
        return next((f for f in self.state()["files"] if f["name"] == name), None)

    def names(self) -> list:
        return [f["name"] for f in self.state()["files"]]

    def error_text(self, code: str) -> str:
        """German text the running mock sends for an error code (asked via fail_next, so the
        test does not depend on the wording of a particular mock version)."""
        self.set(fail_next=code)
        req = urllib.request.Request(self.base + "/api/settings/host_writable?value=1", data=b"", method="POST",
                                     headers={"X-PrintDrive": "1"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read()
        except urllib.error.HTTPError as e:
            body = e.read()
        return json.loads(body.decode("utf-8")).get("error", "")


class Run:
    def __init__(self, base: str, shots: Path | None, headed: bool):
        self.base = base.rstrip("/")
        self.shots = shots
        self.headed = headed
        self.mock = Mock(self.base)
        self.results = []
        self.measure = []
        self.tmp = Path(tempfile.mkdtemp(prefix="ddui_"))
        self.f = {}
        self.err = {}                 # error code -> text of the running mock, filled in main()
        self._make_files()

    def _make_files(self):
        def put(key, name, data, sub=""):
            d = self.tmp / sub if sub else self.tmp
            d.mkdir(parents=True, exist_ok=True)
            p = d / name
            p.write_bytes(data)
            self.f[key] = p
        put("3mf", "Prüfstück Größe äöü 2.3mf", make_3mf(600))
        put("txt", "ui-test notiz.txt", ("Zeile mit Umlauten äöüß\n" * 80).encode("utf-8"))
        put("odd", "Tom's Teil & Entwurf #1 100%.txt", b"Sonderzeichen im Namen\n" * 40)
        put("bad", "kaputt.3mf", os.urandom(20000))
        put("big", "gross.bin", os.urandom(400 * 1024))
        put("small", "klein.txt", b"klein\n" * 20)
        put("exists", "notizen.txt", b"neuer Inhalt der Notizen\n" * 40, "exists")
        put("empty", "leer.txt", b"")
        put("n10", "Teil 10.txt", b"zehn\n")
        put("n2", "Teil 2.txt", b"zwei\n")
        put("n1", "teil 1.txt", b"eins\n")

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

    def open(self, hash_: str = "", wait: bool = True, before=None, **ctx_kw):
        self.close()
        opts = dict(PROFILES[self.prof])
        opts.update(color_scheme="light", locale="de-DE", accept_downloads=True)
        opts.update(ctx_kw)
        self.context = self.browser.new_context(**opts)
        self.context.add_init_script(INIT_JS)
        if before:
            before(self.context)
        p = self.page = self.context.new_page()
        p.set_default_timeout(8000)
        self.console, self.requests = [], []
        p.on("console", lambda m: self.console.append(f"{m.type}: {m.text}") if m.type == "error" else None)
        p.on("pageerror", lambda e: self.console.append(f"pageerror: {e}"))
        p.on("request", lambda r: self.requests.append((r.method, r.url)))
        p.goto(self.run.base + "/" + hash_)
        if wait:
            self.wait("() => typeof S !== 'undefined' && !!S.st", timeout=15000)
        return p

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

    def panel(self):
        return self.ev(JS_PANEL)

    def dlg(self):
        return self.ev(JS_DLG)

    def rows(self):
        return self.ev(JS_ROWS)

    def menu(self):
        return self.ev(JS_MENU)

    def text(self, sel):
        return self.ev("s => document.querySelector(s).textContent", sel)

    def toasts(self):
        return self.ev("() => window.__toasts")

    def dialogs(self):
        return self.ev("() => window.__dialogs.map(d => d.title)")

    def clear_toasts(self):
        self.ev("() => { window.__toasts.length = 0; }")

    def wait_toast(self, sub: str, timeout=8000) -> bool:
        return self.wait("s => window.__toasts.some(t => t.text.includes(s))", sub, timeout)

    def wait_title(self, title: str, timeout=8000) -> bool:
        return self.wait("t => document.querySelector('#modeTitle').textContent === t", title, timeout)

    def wait_dlg(self, title: str, timeout=7000) -> bool:
        return self.wait("t => document.querySelector('#dlg').open && document.querySelector('#dTitle').textContent.includes(t)",
                         title, timeout)

    def wait_dlg_closed(self, timeout=7000) -> bool:
        return self.wait("() => !document.querySelector('#dlg').open", None, timeout)

    def wait_derr(self, timeout=7000) -> bool:
        return self.wait("() => document.querySelector('#dErr').textContent.trim() !== '' && !document.querySelector('#dYes').disabled",
                         None, timeout)

    def wait_notice(self, sub: str, timeout=8000) -> bool:
        return self.wait("s => { const n = document.querySelector('#notice'); return !n.hidden && n.textContent.includes(s); }",
                         sub, timeout)

    def wait_no_notice(self, timeout=8000) -> bool:
        return self.wait("() => document.querySelector('#notice').hidden", None, timeout)

    # ---- input
    def tap(self, target, **kw):
        loc = self.page.locator(target) if isinstance(target, str) else target
        if self.phone:
            loc.tap(**kw)
        else:
            loc.click(**kw)

    def point(self, x, y):
        if self.phone:
            self.page.touchscreen.tap(x, y)
        else:
            self.page.mouse.click(x, y)

    def pick(self, *keys):
        self.page.set_input_files("#picker", [str(self.run.f[k]) for k in keys])

    def row(self, name: str):
        names = [r["name"] for r in self.rows()]
        return self.page.locator("#list .row").nth(names.index(name))

    def open_menu(self, name: str) -> bool:
        self.tap(self.row(name).locator("[data-more]"))
        return self.wait("() => !document.querySelector('#menu').hidden")

    def act(self, name: str, action: str):
        self.open_menu(name)
        self.tap(f'#menu [data-act="{action}"]')

    def watch_uploads(self, timeout=45, on_sample=None):
        """Samples the upload panel until no upload is live any more."""
        samples, t0 = [], time.time()
        while time.time() - t0 < timeout:
            s = self.panel()
            samples.append(s)
            if on_sample:
                on_sample(s)
            if s["items"] and not s["working"] and not any(i["live"] for i in s["items"]):
                break
            time.sleep(0.1)
        return samples

    def overflow_check(self, label: str):
        o = self.ev(JS_OVERFLOW)
        self.check(f"{label}: no horizontal scrolling, nothing outside the viewport",
                   o["scrollW"] <= o["innerW"] and not o["out"], f"scrollWidth {o['scrollW']} / {o['innerW']} {o['out'] or ''}")


TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


# ================================================================== 1. initial load

EXPECT = {
    "usb_active": dict(title=T_USB, sub="Das angeschlossene USB-Gerät hat Zugriff auf die Dateien.",
                       btn="Dateiverwaltung aktivieren", sto="microSD-Karte", kind="locked"),
    "usb_no_host": dict(title=T_USB, sub="Kein USB-Gerät angeschlossen.", btn="Dateiverwaltung aktivieren",
                        sto="microSD-Karte", kind="locked"),
    "files": dict(title=T_APP, sub="Das USB-Gerät sieht das Laufwerk im Moment nicht.",
                  btn="USB-Laufwerk bereitstellen", sto="microSD-Karte", kind="rows", rows=14),
    "empty": dict(title=T_APP, sub="Das USB-Gerät sieht das Laufwerk im Moment nicht.",
                  btn="USB-Laufwerk bereitstellen", sto="microSD-Karte", kind="empty"),
    "full": dict(title=T_APP, sub="Das USB-Gerät sieht das Laufwerk im Moment nicht.",
                 btn="USB-Laufwerk bereitstellen", sto="microSD-Karte", kind="rows", rows=14, bar="full"),
    "no_fs": dict(title="Speicher nicht lesbar", sub="Auf dem Speicher wurde kein lesbares Dateisystem gefunden.",
                  btn=None, sto="microSD-Karte", kind="broken"),
    "ap_setup": dict(title=T_USB, sub="Das angeschlossene USB-Gerät hat Zugriff auf die Dateien.",
                     btn="Dateiverwaltung aktivieren", sto="microSD-Karte", kind="locked"),
    "flash_medium": dict(title=T_APP, sub="Das USB-Gerät sieht das Laufwerk im Moment nicht.",
                         btn="USB-Laufwerk bereitstellen", sto="Interner Speicher", kind="rows", rows=2),
    "many": dict(title=T_APP, sub="Das USB-Gerät sieht das Laufwerk im Moment nicht.",
                 btn="USB-Laufwerk bereitstellen", sto="microSD-Karte", kind="rows", rows=300),
    "truncated": dict(title=T_APP, sub="Das USB-Gerät sieht das Laufwerk im Moment nicht.",
                      btn="USB-Laufwerk bereitstellen", sto="microSD-Karte", kind="rows", rows=None),
}


@test
def t01_initial_load(c: Ctx):
    for name, exp in EXPECT.items():
        c.reset(name)
        c.open()
        c.page.wait_for_timeout(250)
        st = c.mock.state()["status"]
        d = st["drive"]
        ui = c.ui()
        tag = f"load {name}"
        c.check(f"{tag}: status strip", ui["title"] == exp["title"] and ui["sub"] == exp["sub"],
                f"'{ui['title']}' / '{ui['sub']}'")
        sto = f"{fsize(d['total'] - d['free'])} von {fsize(d['total'])} belegt" if d["total"] else "Größe unbekannt"
        ok = ui["stoTitle"] == exp["sto"] and ui["stoSub"] == sto
        if exp.get("bar"):
            ok = ok and exp["bar"] in ui["barCls"].split()
        c.check(f"{tag}: storage text", ok, f"'{ui['stoTitle']}' / '{ui['stoSub']}' bar='{ui['barCls']}' {ui['barW']}")

        kind = exp["kind"]
        if exp["btn"] is None:
            ok = ui["btn"] is None
        else:
            ok = ui["btn"] == exp["btn"] and ui["btnPrimary"] == (exp["title"] == T_USB) and not ui["btnDisabled"]
        if kind in ("rows", "empty"):       # file mode: upload is the primary action
            ok = ok and (ui["fab"] and not ui["upBtn"] if c.phone else ui["upBtn"] and ui["upBtnPrimary"] and not ui["fab"])
        else:
            ok = ok and not ui["fab"] and not ui["upBtn"]
        c.check(f"{tag}: mode button and primary action", ok,
                f"modeBtn={ui['btn']!r} primary={ui['btnPrimary']} upBtn={ui['upBtn']} fab={ui['fab']}")

        if kind == "locked":
            n = d["files"]
            ok = ui["stateH"] == "Die Dateien gehören gerade dem USB-Gerät" and ui["rows"] == 0 and ui["lockedUp"] \
                and f"Zuletzt waren {n} Dateien gespeichert." in (ui["stateP"] or "") and not ui["search"]
            info = ui["stateH"]
        elif kind == "empty":
            ok = ui["stateH"] == "Noch keine Dateien" and ui["emptyUp"] and ui["rows"] == 0 and ui["count"] == ""
            info = ui["stateH"]
        elif kind == "broken":
            ok = ui["stateH"] == "Der Speicher muss eingerichtet werden" and ui["settingsLink"] and ui["rows"] == 0
            info = ui["stateH"]
        else:
            want = exp["rows"]
            if want is None:
                listing = c.mock.get("/api/files")
                want = len(listing["files"])
                ok = listing["truncated"] and any("sehr viele Dateien" in h for h in ui["hints"])
            else:
                ok = True
            ok = ok and ui["rows"] == want and ui["count"] == str(want) and ui["search"] \
                and (ui["sortM"] and not ui["cols"] if c.phone else ui["cols"] and not ui["sortM"])
            if name == "files":
                ok = ok and any("2 Ordner werden hier nicht angezeigt" in h for h in ui["hints"])
            info = f"{ui['rows']} rows, count '{ui['count']}', hints {len(ui['hints'])}"
        c.check(f"{tag}: list state ({kind})", ok, info)
        c.check(f"{tag}: no JS console errors", not c.console, c.console[:3])
        c.overflow_check(tag)
        c.shot(name)
        if name == "files":
            c.ev("() => scrollTo(0, document.documentElement.scrollHeight)")
            c.page.wait_for_timeout(150)
            c.shot("files_bottom")


@test
def t01b_small_phone_and_no_api(c: Ctx):
    if c.phone:
        for name, hash_ in (("files", ""), ("usb_active", ""), ("files", "#einstellungen")):
            c.reset(name)
            c.open(hash_, viewport={"width": 320, "height": 568})
            c.page.wait_for_timeout(250)
            c.overflow_check(f"320 px wide, {name}{hash_}")
            c.shot(f"w320_{name}{'_settings' if hash_ else ''}", full=bool(hash_))
    # API unreachable from the first request on (page itself is delivered)
    c.reset("files")
    c.open(wait=False, before=lambda ctx: ctx.route("**/api/**", lambda r: r.abort()))
    ok = c.wait_notice("Keine Antwort vom Laufwerk", 15000)
    ui = c.ui()
    c.check("API unreachable at load: notice appears, no stale content", ok and ui["rows"] == 0 and ui["btn"] is None,
            f"title '{ui['title']}', skeletons {ui['skel']}")
    c.shot("no_api_at_load")


# ================================================================== 2./3. switching

@test
def t02_activate(c: Ctx):
    c.reset("usb_active")
    p = c.open()
    c.tap("#modeBtn")
    ok = c.wait_dlg(T_TAKEOVER)
    d = c.dlg()
    c.check("activate: take-over dialog appears (HOST_ACTIVE)", ok and d["yes"] == "Trennen und fortfahren"
            and d["no"] == "Abbrechen" and "danger" in d["yesCls"], f"'{d['title']}' [{d['no']}] [{d['yes']}] focus=#{d['focus']}")
    c.shot("takeover_dialog")
    c.overflow_check("take-over dialog")
    c.tap("#dNo")
    c.wait_dlg_closed()
    p.wait_for_timeout(300)
    ui = c.ui()
    c.check("activate: cancel keeps USB mode", c.mock.state()["owner"] == "usb" and ui["title"] == T_USB
            and ui["btn"] == "Dateiverwaltung aktivieren" and not ui["btnDisabled"], ui["title"])
    c.tap("#modeBtn")
    c.wait_dlg(T_TAKEOVER)
    p.keyboard.press("Escape")
    c.wait_dlg_closed()
    c.check("activate: Escape cancels the dialog", not c.dlg()["open"] and c.mock.state()["owner"] == "usb")
    c.tap("#modeBtn")
    c.wait_dlg(T_TAKEOVER)
    c.tap("#dYes")
    ok = c.wait("() => document.querySelector('#modeTitle').textContent === 'Dateiverwaltung aktiv' && document.querySelectorAll('#list .row').length === 3")
    c.check("activate: confirm switches to file mode and the list appears", ok and c.mock.state()["owner"] == "app",
            f"{c.ui()['title']}, rows {c.ui()['rows']}")
    c.shot("after_activate")

    for label, scen, sets in (("host_ejected=1", "usb_active", dict(host_ejected=1, owner="usb")),
                              ("usb_connected=0", "usb_no_host", {})):
        c.reset(scen, **sets)
        c.open()
        c.tap("#modeBtn")
        ok = c.wait_title(T_APP)
        c.check(f"activate with {label}: no dialog, file mode", ok and not c.dialogs() and c.mock.state()["owner"] == "app",
                f"dialogs {c.dialogs()}")

    c.reset("usb_active", host_writing="hold")
    c.open()
    sub = c.text("#modeSub")
    c.check("host writing: status strip says the device is writing", sub == "Das USB-Gerät schreibt gerade auf das Laufwerk.", sub)
    c.tap("#modeBtn")
    ok = c.wait_toast("schreibt gerade")
    t = c.toasts()
    c.check("host writing: error toast, no dialog, mode stays USB", ok and t and t[-1]["err"] and not c.dialogs()
            and c.mock.state()["owner"] == "usb" and c.ui()["title"] == T_USB, t[-1]["text"] if t else "no toast")
    c.shot("host_writing_toast")


@test
def t03_provide(c: Ctx):
    c.reset("files")
    c.open()
    c.tap("#modeBtn")
    ok = c.wait_toast("Das USB-Laufwerk ist bereitgestellt.")
    c.shot("provided_toast")
    ui = c.ui()
    c.check("provide: toast and locked state", ok and ui["title"] == T_USB and ui["stateH"] == "Die Dateien gehören gerade dem USB-Gerät"
            and "Zuletzt waren 14 Dateien gespeichert." in (ui["stateP"] or "") and c.mock.state()["owner"] == "usb"
            and not ui["fab"] and not ui["upBtn"] and not ui["search"], f"'{ui['title']}' / '{ui['stateH']}'")
    t = c.toasts()
    c.check("provide: exactly one toast, not an error", len(t) == 1 and not t[0]["err"], [x["text"] for x in t])


# ================================================================== 4. uploads

def _row_ok(c: Ctx, name: str, size: int):
    r = next((x for x in c.rows() if x["name"] == name), None)
    if not r:
        return False, "row missing"
    if c.phone:
        return r["meta"].startswith(f"{fsize(size)} · Heute, "), r["meta"]
    return r["size"] == fsize(size) and r["date"].startswith("Heute, "), f"{r['size']} / {r['date']}"


@test
def t04a_upload_picker(c: Ctx):
    c.reset("files", upload_kbps=200)
    p = c.open()
    keys = ["3mf", "txt", "odd"]
    names = [c.run.f[k].name for k in keys]
    sizes = [c.run.f[k].stat().st_size for k in keys]
    seen = {"bars": [], "early": [], "shot": False, "states": set()}

    def on(s):
        it = s["items"]
        if it and it[0]["bar"] is not None:
            seen["bars"].append(it[0]["bar"])
        for i in it:
            seen["states"].add(i["s"].split(" ")[0] if "%" not in i["s"] else "%")
        if any("gespeichert" in t for t in s["toasts"]) and (s["working"] or any(i["live"] for i in it)):
            seen["early"].append(s["toasts"])
        if not seen["shot"] and it and it[0]["bar"] is not None and 15 < it[0]["bar"] < 70:
            seen["shot"] = True
            c.shot("upload_progress")

    c.pick(*keys)
    first = c.panel()
    c.check("upload: progress panel appears with all files", not first["hidden"] and [i["name"] for i in first["items"]] == names,
            f"head '{first['head']}'")
    samples = c.watch_uploads(on_sample=on)
    last = samples[-1]
    bars = seen["bars"]
    mid = sorted(set(b for b in bars if 0 < b < 100))
    c.check("upload: progress advances", len(mid) >= 3 and bars == sorted(bars), f"{len(mid)} distinct values, e.g. {mid[:6]}")
    c.check("upload: final state 'Gespeichert' for every file", all(i["s"] == "Gespeichert" and not i["err"] for i in last["items"])
            and last["head"] == "3 Dateien gespeichert" and last["closeVisible"], f"head '{last['head']}' {[i['s'] for i in last['items']]}")
    ok = c.wait_toast("3 Dateien gespeichert.")
    c.shot("upload_done")
    st = {f["name"]: f for f in c.mock.state()["files"]}
    c.check("upload: success toast only after the server confirmed all files", ok and not seen["early"]
            and all(n in st and st[n]["size"] == s for n, s in zip(names, sizes)), f"early toasts: {seen['early'][:1]}")
    for n, s in zip(names, sizes):
        ok, info = _row_ok(c, n, s)
        c.check(f"upload: '{n}' listed with size and 'Heute, …'", ok, info)
    c.check("upload: count updated", c.ui()["count"] == "17", c.ui()["count"])

    # panel / FAB / list overlap once the panel is open
    c.ev("() => scrollTo(0, document.documentElement.scrollHeight)")
    p.wait_for_timeout(200)
    cov = c.ev(r"""() => {
      const rows = document.querySelectorAll('#list .row'), b = rows[rows.length - 1].querySelector('[data-more]');
      const r = b.getBoundingClientRect(), el = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      const up = document.querySelector('#uploads').getBoundingClientRect(), fab = document.querySelector('#fab').getBoundingClientRect();
      return {covered: !(el && (el === b || b.contains(el))), by: el ? (el.id || el.className || el.tagName) : null,
              fabOverPanel: fab.width > 0 && fab.bottom > up.top + 1 && fab.top < up.bottom, upH: Math.round(up.height)};
    }""")
    c.shot("upload_panel_list_end")
    c.check("upload panel: last row's menu button stays reachable at the end of the list", not cov["covered"],
            f"covered by '{cov['by']}', panel height {cov['upH']} px")
    c.check("upload panel: FAB does not overlap the panel", not cov["fabOverPanel"])
    c.tap("#upClose")
    c.check("upload panel: close button hides it", c.panel()["hidden"])
    c.check("upload: no JS console errors", not c.console, c.console[:3])


@test
def t04b_upload_from_usb_mode(c: Ctx):
    name, size = c.run.f["txt"].name, c.run.f["txt"].stat().st_size

    def start():
        with c.page.expect_file_chooser() as fc:
            c.tap("#lockedUp")
        fc.value.set_files(str(c.run.f["txt"]))

    # automatic re-provide (default)
    c.reset("usb_active")
    c.open()
    start()
    ok = c.wait_dlg(T_TAKEOVER)
    c.check("upload in USB mode: take-over dialog", ok, c.dlg()["title"])
    c.tap("#dYes")
    ok = c.wait_toast("gespeichert", 20000)
    c.page.wait_for_timeout(400)
    t = [x["text"] for x in c.toasts()]
    st = c.mock.state()
    c.shot("upload_usb_reprovided")
    c.check("upload in USB mode: drive is provided again automatically, toast says so",
            ok and st["owner"] == "usb" and t == ["1 Datei gespeichert. Das USB-Laufwerk ist wieder bereitgestellt."]
            and any(f["name"] == name and f["size"] == size for f in st["files"]) and c.ui()["title"] == T_USB, t)

    # switch "Nach dem Hochladen wieder bereitstellen" off
    c.reset("usb_active")
    p = c.open("#einstellungen")
    c.tap("label.switch:has(#swAuto)")
    c.check("auto switch: stored for this browser", c.ev("() => localStorage.getItem('dd-auto')") == "0"
            and not c.ev("() => document.querySelector('#swAuto').checked"))
    c.tap("a.back")
    c.wait("() => !document.querySelector('#vFiles').hidden")
    start()
    c.wait_dlg(T_TAKEOVER)
    c.tap("#dYes")
    ok = c.wait_toast("gespeichert", 20000)
    p.wait_for_timeout(600)
    t = [x["text"] for x in c.toasts()]
    c.check("upload in USB mode, auto switch off: stays in file mode", ok and c.mock.state()["owner"] == "app"
            and t == ["1 Datei gespeichert."] and c.ui()["title"] == T_APP, t)

    # take-over refused by the user
    c.reset("usb_active")
    c.open()
    start()
    c.wait_dlg(T_TAKEOVER)
    c.tap("#dNo")
    c.wait("() => !S.working")
    pn = c.panel()
    c.shot("upload_takeover_cancelled")
    c.check("upload in USB mode, take-over cancelled: nothing uploaded, clear message",
            pn["items"] and "Nicht hochgeladen" in pn["items"][0]["err"] and c.mock.state()["owner"] == "usb"
            and name not in c.mock.names() and not c.toasts(), f"'{pn['head']}' / '{pn['items'][0]['err'] if pn['items'] else ''}'")


@test
def t04c_upload_existing(c: Ctx):
    c.reset("files")
    c.open()
    old = c.mock.file("notizen.txt")["size"]
    new = c.run.f["exists"].stat().st_size
    c.pick("exists")
    ok = c.wait_dlg("Vorhandene Datei ersetzen?")
    d = c.dlg()
    c.check("existing file: replace dialog", ok and "notizen.txt" in d["body"] and d["yes"] == "Ersetzen" and d["no"] == "Überspringen",
            f"'{d['title']}' [{d['no']}] [{d['yes']}]")
    c.shot("replace_dialog")
    c.tap("#dNo")
    c.wait("() => !S.working")
    pn = c.panel()
    c.check("existing file: 'Überspringen' keeps the old file", pn["items"][0]["s"] == "Übersprungen"
            and c.mock.file("notizen.txt")["size"] == old and not any("gespeichert" in t for t in pn["toasts"]),
            f"'{pn['items'][0]['s']}', head '{pn['head']}'")
    c.tap("#upClose")
    c.pick("exists")
    c.wait_dlg("Vorhandene Datei ersetzen?")
    c.tap("#dYes")
    ok = c.wait_toast("1 Datei gespeichert.", 15000)
    pn = c.panel()
    c.check("existing file: 'Ersetzen' overwrites", ok and pn["items"][0]["s"] == "Gespeichert"
            and c.mock.file("notizen.txt")["size"] == new and c.ui()["rows"] == 14, f"size {old} -> {c.mock.file('notizen.txt')['size']}")


@test
def t04d_upload_errors(c: Ctx):
    def one(label, scenario, sets, key, want_err, shot=None, head="1 von 1 nicht gespeichert"):
        c.reset(scenario, **sets)
        c.open()
        before = len(c.mock.state()["files"])
        rows = c.ui()["rows"]
        c.pick(key)
        c.watch_uploads(timeout=30)
        c.page.wait_for_timeout(500)
        pn = c.panel()
        it = pn["items"][0] if pn["items"] else {"err": "", "s": ""}
        if shot:
            c.shot(shot)
        st = c.mock.state()
        c.check(f"upload error, {label}", want_err in it["err"] and len(st["files"]) == before and c.ui()["rows"] == rows
                and not any("gespeichert" in t for t in pn["toasts"]) and pn["head"] == head and it["s"] != "Gespeichert",
                f"panel: '{pn['head']}' / '{it['err']}' toasts {pn['toasts']}")

    one("invalid .3mf (random bytes)", "files", {}, "bad", c.run.err["INVALID_FILE"], "upload_invalid_3mf")
    one("not enough space (scenario full)", "full", {}, "3mf", c.run.err["NO_SPACE"], "upload_no_space")
    one("connection reset at 50 % (upload_fail_at)", "files", dict(upload_fail_at=50, upload_kbps=300), "3mf",
        "Verbindung unterbrochen", "upload_connection_lost")
    one("server reports IO error after the whole body arrived", "files", {"fail_next": "IO@/api/upload", "upload_kbps": 0},
        "txt", c.run.err["IO"])
    one("type not allowed (types=.3mf)", "files", dict(types=".3mf"), "txt", "Dieser Dateityp ist nicht erlaubt (möglich: .3mf)")
    one("empty file", "files", {}, "empty", "Die Datei ist leer.")


@test
def t04e_upload_cancel_and_queue(c: Ctx):
    # cancel during a slow upload
    c.reset("files", upload_kbps=40)
    p = c.open()
    c.pick("big")
    ok = c.wait("() => { const b = document.querySelector('#upList .bar>i'); return b && parseFloat(b.style.width) > 4; }", None, 15000)
    ui = c.ui()
    c.check("slow upload: mode button disabled while uploading", ok and ui["btnDisabled"], f"disabled={ui['btnDisabled']}")
    c.shot("upload_slow")
    c.tap("[data-cancel]")
    ok = c.wait("() => document.querySelector('#upList .l1 .s').textContent.trim() === 'Abgebrochen' && !S.working", None, 10000)
    p.wait_for_timeout(500)
    pn = c.panel()
    c.shot("upload_cancelled")
    c.check("cancel: 'Abgebrochen', no file, no toast", ok and "gross.bin" not in c.mock.names() and not pn["toasts"]
            and not pn["items"][0]["live"] and pn["closeVisible"], f"head '{pn['head']}' / '{pn['items'][0]['s']}'")
    c.mock.set(upload_kbps=0)
    c.pick("small")
    ok = c.wait_toast("1 Datei gespeichert.", 15000)
    c.check("cancel: UI usable afterwards (next upload works)", ok and "klein.txt" in c.mock.names()
            and not c.ui()["btnDisabled"] and any(r["name"] == "klein.txt" for r in c.rows()))

    # queue: add files while one is uploading
    c.reset("files", upload_kbps=120)
    c.open()
    c.pick("big")
    c.wait("() => { const b = document.querySelector('#upList .bar>i'); return b && parseFloat(b.style.width) > 4; }", None, 15000)
    c.pick("txt", "small")
    pn = c.panel()
    c.shot("upload_queue")
    c.check("queue: files added during an upload wait their turn", pn["head"] == "3 Dateien werden hochgeladen"
            and [i["s"] for i in pn["items"][1:]] == ["Wartet", "Wartet"] and not pn["closeVisible"],
            f"head '{pn['head']}' {[i['s'] for i in pn['items']]}")
    c.watch_uploads(timeout=40)
    ok = c.wait_toast("3 Dateien gespeichert.", 10000)
    names = c.mock.names()
    c.check("queue: all files saved, one summary toast", ok and all(c.run.f[k].name in names for k in ("big", "txt", "small"))
            and len(c.toasts()) == 1 and c.ui()["rows"] == 17, [t["text"] for t in c.toasts()])


@test
def t04f_drag_and_drop(c: Ctx):
    if c.phone:
        return
    mk = "() => { const dt = new DataTransfer(); dt.items.add(new File(['Hallo DataDrive\\n'], 'gezogen äö.txt', {type: 'text/plain'})); return dt; }"
    c.reset("files")
    p = c.open()
    dt = p.evaluate_handle(mk)
    p.dispatch_event("body", "dragenter", {"dataTransfer": dt})
    vis = c.ev("() => !document.querySelector('#drop').hidden")
    c.check("drag and drop: overlay appears", vis and c.text("#drop") == "Dateien zum Hochladen hier ablegen", c.text("#drop"))
    c.shot("drop_overlay")
    p.dispatch_event("body", "dragleave", {"dataTransfer": dt})
    c.check("drag and drop: overlay disappears on dragleave", c.ev("() => document.querySelector('#drop').hidden"))
    p.dispatch_event("body", "dragenter", {"dataTransfer": dt})
    p.dispatch_event("body", "dragover", {"dataTransfer": dt})
    p.dispatch_event("body", "drop", {"dataTransfer": dt})
    ok = c.wait_toast("1 Datei gespeichert.", 15000)
    c.check("drag and drop: dropped file is uploaded", ok and "gezogen äö.txt" in c.mock.names()
            and c.ev("() => document.querySelector('#drop').hidden") and any(r["name"] == "gezogen äö.txt" for r in c.rows()))
    # settings view: no overlay
    c.tap("#navSettings")
    p.dispatch_event("body", "dragenter", {"dataTransfer": p.evaluate_handle(mk)})
    c.check("drag and drop: no overlay in the settings", c.ev("() => document.querySelector('#drop').hidden"))
    # USB mode: drop leads to the take-over dialog
    c.reset("usb_active")
    p = c.open()
    dt = p.evaluate_handle(mk)
    p.dispatch_event("body", "dragenter", {"dataTransfer": dt})
    p.dispatch_event("body", "drop", {"dataTransfer": dt})
    c.check("drag and drop in USB mode: take-over dialog", c.wait_dlg(T_TAKEOVER))


@test
def t04g_upload_concurrency(c: Ctx):
    # file actions while an upload is running (the device answers one request at a time)
    c.reset("files", upload_kbps=50)
    p = c.open()
    c.pick("big")                                           # ~8 s
    c.wait("() => { const b = document.querySelector('#upList .bar>i'); return b && parseFloat(b.style.width) > 3; }", None, 15000)
    c.act("LEER.TXT", "del")
    c.wait_dlg("Datei löschen?")
    t0 = time.time()
    c.tap("#dYes")
    p.wait_for_timeout(2500)
    d = c.dlg()
    c.shot("delete_during_upload")
    c.check("during an upload: file actions are blocked or explained instead of hanging silently",
            not (d["open"] and d["yesDisabled"] and not d["err"]),
            f"after 2.5 s: dialog open={d['open']}, 'Löschen' disabled={d['yesDisabled']}, message='{d['err']}'")
    c.wait_dlg_closed(30000)
    c.run.info(f"{c.prof}: delete confirmed during an upload returned after {time.time() - t0:.1f} s")
    c.watch_uploads(timeout=40)

    # a dialog the user opened is replaced by the "replace file?" question of the upload queue
    c.reset("files", upload_kbps=60)
    p = c.open()
    c.pick("big", "exists")                                 # big ~7 s, then notizen.txt (exists)
    c.wait("() => { const b = document.querySelector('#upList .bar>i'); return b && parseFloat(b.style.width) > 3; }", None, 15000)
    c.act("README", "ren")
    c.wait_dlg("Datei umbenennen")
    c.wait("() => window.__dialogs.length > 1 || !S.working", None, 40000)
    p.wait_for_timeout(300)
    d = c.dlg()
    c.shot("dialog_replaced_by_upload")
    c.check("dialog collision: an open rename dialog is not replaced by another question",
            d["title"] == "Datei umbenennen", f"dialog now: '{d['title']}' [{d['no']}] [{d['yes']}], input shown: {d['input'] is not None}")
    if d["open"]:
        c.tap("#dNo")
    c.wait("() => !S.working", None, 30000)


# ================================================================== 5. file actions

@test
def t05a_row_menu(c: Ctx):
    c.reset("files")
    p = c.open()
    ok = c.open_menu("Benchy.3mf")
    m = c.menu()
    r = m["rect"]
    c.check("menu via ⋯: opens with three actions", ok and m["items"] == ["Herunterladen", "Umbenennen", "Löschen"]
            and m["openRows"] == 1, m["items"])
    c.check("menu via ⋯: inside the viewport", r["l"] >= 0 and r["t"] >= 0 and r["r"] <= m["vw"] + 0.5 and r["b"] <= m["vh"] + 0.5, r)
    if c.phone:
        c.check("menu on phone is a bottom sheet with file name and dimmed background", abs(r["l"]) < 1 and abs(r["r"] - m["vw"]) < 1
                and abs(r["b"] - m["vh"]) < 1 and m["head"] == "Benchy.3mf" and "0)" not in m["scrimBg"].replace(" ", "")[-3:],
                f"rect {r}, head {m['head']!r}, scrim {m['scrimBg']}")
    c.shot("menu")
    c.check("menu: focus moves into the menu", m["focusInMenu"] and m["focusText"] == "Herunterladen", m["focusText"])
    nm = c.ev(JS_NAMES)
    c.check("menu open: every control has an accessible name", not nm["bad"], nm["bad"][:3])
    p.keyboard.press("ArrowDown")
    c.check("menu: arrow keys move between the items (role=menu)", c.menu()["focusText"] == "Umbenennen", c.menu()["focusText"])
    p.keyboard.press("Escape")
    m = c.menu()
    foc = c.ev("() => { const a = document.activeElement; return a && a.matches('[data-more]') ? a.closest('.row').dataset.name : (a ? a.tagName : ''); }")
    c.check("menu: Escape closes it", m["hidden"] and m["scrimHidden"] and m["openRows"] == 0)
    c.check("menu: focus returns to the ⋯ button after closing", foc == "Benchy.3mf", f"focus is on {foc}")

    # click on the row itself
    row = c.row("firmware_backup.bin")
    row.scroll_into_view_if_needed()
    bb = row.bounding_box()
    pos = {"x": bb["width"] * 0.4, "y": bb["height"] / 2}
    c.tap(row, position=pos)
    c.wait("() => !document.querySelector('#menu').hidden")
    m = c.menu()
    r = m["rect"]
    ok = not m["hidden"] and r["r"] <= m["vw"] + 0.5 and r["b"] <= m["vh"] + 0.5
    if not c.phone:
        ok = ok and abs(r["l"] - (bb["x"] + pos["x"])) < 3 and abs(r["t"] - (bb["y"] + pos["y"] + 4)) < 3
    c.check("menu via click on the row: opens at the pointer, inside the viewport", ok, r)
    c.point(20, 110)
    c.check("menu: click on the backdrop closes it", c.menu()["hidden"])

    if not c.phone:
        c.row("README").click(button="right", position={"x": 300, "y": 30})
        m = c.menu()
        c.check("menu via right-click (desktop)", not m["hidden"] and m["openRows"] == 1, m["rect"])
        p.keyboard.press("Escape")
        c.row("README").focus()
        p.keyboard.press("Enter")
        c.check("menu via keyboard (Enter on the row)", not c.menu()["hidden"] and c.menu()["focusInMenu"])
        p.keyboard.press("Escape")

    # last row at the very bottom of the page
    c.ev("() => scrollTo(0, document.documentElement.scrollHeight)")
    p.wait_for_timeout(150)
    c.tap(p.locator("#list .row").last.locator("[data-more]"))
    m = c.menu()
    r = m["rect"]
    c.check("menu for the last row: clamped into the viewport", not m["hidden"] and r["b"] <= m["vh"] + 0.5 and r["t"] >= 0, r)
    c.shot("menu_last_row")
    # the list is re-rendered by every status poll
    p.wait_for_timeout(3600)
    m = c.menu()
    c.check("menu: row stays highlighted while its menu is open (survives the 3 s status poll)",
            not m["hidden"] and m["openRows"] == 1, f"highlighted rows: {m['openRows']}")
    p.keyboard.press("Escape")

    if not c.phone:
        p.set_viewport_size({"width": 760, "height": 480})
        p.wait_for_timeout(200)
        c.ev("() => scrollTo(0, 0)")
        c.open_menu(c.rows()[0]["name"])
        m = c.menu()
        r = m["rect"]
        c.check("menu in a narrow desktop window (760x480): inside the viewport",
                r["l"] >= 0 and r["r"] <= m["vw"] + 0.5 and r["b"] <= m["vh"] + 0.5 and r["t"] >= 0, r)
        c.overflow_check("narrow desktop window 760 px")
        c.shot("menu_narrow_window")


@test
def t05b_download(c: Ctx):
    c.reset("files")
    p = c.open()
    for name in ("Würfel.3mf", LONG_NAME):
        url = p.url
        try:
            c.open_menu(name)
            with p.expect_download(timeout=10000) as di:
                c.tap('#menu [data-act="dl"]')
            dl = di.value
            size = Path(dl.path()).stat().st_size
            want = c.mock.file(name)["stored"]
            c.check(f"download '{name[:28]}': download event with the file name", dl.suggested_filename == name
                    and dl.failure() is None and size == want and p.url == url, f"{dl.suggested_filename!r}, {size} B")
        except PWTimeout:
            c.check(f"download '{name[:28]}': download event with the file name", False, "no download event")


@test
def t05c_rename(c: Ctx):
    c.reset("files")
    p = c.open()
    c.act("Würfel.3mf", "ren")
    ok = c.wait_dlg("Datei umbenennen")
    d = c.dlg()
    c.check("rename: dialog prefilled, base name selected, input focused", ok and d["input"] == "Würfel.3mf"
            and d["sel"] == [0, 6] and d["focus"] == "dInput" and d["yes"] == "Umbenennen" and d["label"] == "Neuer Name",
            f"value {d['input']!r} selection {d['sel']} focus #{d['focus']}")
    c.shot("rename_dialog")

    def attempt(value, enter=False):
        p.fill("#dInput", value)
        if enter:
            p.keyboard.press("Enter")
        else:
            c.tap("#dYes")
        c.wait_derr(6000)
        return c.dlg()

    d = attempt("a/b:c.3mf", enter=True)
    c.check("rename: forbidden characters rejected in the dialog", d["open"] and "darf nicht" in d["err"]
            and "Würfel.3mf" in c.mock.names(), d["err"])
    c.shot("rename_error")
    c.overflow_check("rename dialog with error")
    d = attempt(".versteckt")
    c.check("rename: leading dot rejected", d["open"] and "darf nicht" in d["err"], d["err"])
    d = attempt("Benchy.3mf")
    c.check("rename: EXISTS error stays in the dialog", d["open"] and d["err"] == "Eine Datei mit diesem Namen gibt es bereits."
            and "Würfel.3mf" in c.mock.names(), d["err"])
    d = attempt("   ")
    c.check("rename: empty name rejected", d["open"] and d["err"] == "Bitte einen Namen eingeben.", d["err"])
    d = attempt("x" * 130 + ".3mf")
    c.check("rename: over-long name (server BAD_NAME) shows a readable message", d["open"] and bool(d["err"]), d["err"])
    p.fill("#dInput", "Würfel neu äß.3mf")
    c.tap("#dYes")
    ok = c.wait_dlg_closed() and c.wait_toast("Umbenannt in „Würfel neu äß.3mf“.")
    names = c.mock.names()
    rows = [r["name"] for r in c.rows()]
    c.shot("rename_done")
    c.check("rename: success updates list and shows a toast", ok and "Würfel neu äß.3mf" in names and "Würfel.3mf" not in names
            and "Würfel neu äß.3mf" in rows and "Würfel.3mf" not in rows and len(rows) == 14, [t["text"] for t in c.toasts()])


@test
def t05d_delete_and_dismiss(c: Ctx):
    c.reset("files")
    p = c.open()
    c.act(LONG_NAME, "del")
    ok = c.wait_dlg("Datei löschen?")
    d = c.dlg()
    c.check("delete: confirm dialog names the file, safe button focused", ok and LONG_NAME in d["body"] and d["yes"] == "Löschen"
            and "danger" in d["yesCls"] and d["focus"] == "dNo", f"focus #{d['focus']}")
    c.shot("delete_dialog")
    c.overflow_check("delete dialog (long name)")
    nm = c.ev(JS_NAMES)
    c.check("dialog open: every control has an accessible name", not nm["bad"], nm["bad"][:3])
    seen = set()
    for _ in range(6):
        p.keyboard.press("Tab")
        seen.add(c.ev("() => { const a = document.activeElement; return !a || a === document.body ? 'body' : (document.querySelector('#dlg').contains(a) ? 'dlg:' + a.id : 'OUTSIDE:' + a.tagName + '#' + a.id); }"))
    c.check("dialog: Tab never reaches the page behind it", not any(s.startswith("OUTSIDE") for s in seen), sorted(seen))
    p.keyboard.press("Escape")
    c.wait_dlg_closed()
    foc = c.ev("() => { const a = document.activeElement; return !a || a === document.body ? 'body' : a.tagName.toLowerCase() + (a.id ? '#' + a.id : '') + '.' + a.className; }")
    c.check("delete: Escape closes the dialog, file kept", not c.dlg()["open"] and LONG_NAME in c.mock.names())
    c.check("dialog opened from the row menu: focus returns to the list (not <body>)", foc != "body", f"focus is on {foc}")

    c.act("LEER.TXT", "del")
    c.wait_dlg("Datei löschen?")
    c.point(6, 6)
    c.wait_dlg_closed(3000)
    c.check("delete: click on the backdrop closes the dialog, file kept", not c.dlg()["open"] and "LEER.TXT" in c.mock.names())

    c.act("LEER.TXT", "del")
    c.wait_dlg("Datei löschen?")
    c.tap("#dYes")
    ok = c.wait_dlg_closed() and c.wait_toast("Gelöscht.")
    ui = c.ui()
    c.check("delete: confirm removes the file, list and count update", ok and "LEER.TXT" not in c.mock.names()
            and ui["rows"] == 13 and ui["count"] == "13" and not any(r["name"] == "LEER.TXT" for r in c.rows()), ui["count"])

    # dialog must not close by a click inside its own box / by a text selection that ends outside
    c.act("README", "del")
    c.wait_dlg("Datei löschen?")
    bb = p.locator("#dlg").bounding_box()
    c.point(bb["x"] + 8, bb["y"] + 8)
    p.wait_for_timeout(300)
    d = c.dlg()
    c.check("dialog: a click on its own padding does not dismiss it", d["open"], f"open={d['open']}")
    if d["open"]:
        c.tap("#dNo")
    if not c.phone:
        c.act("README", "ren")
        c.wait_dlg("Datei umbenennen")
        ib = p.locator("#dInput").bounding_box()
        p.mouse.move(ib["x"] + ib["width"] - 10, ib["y"] + ib["height"] / 2)
        p.mouse.down()
        p.mouse.move(ib["x"] - 150, ib["y"] + ib["height"] / 2, steps=6)
        p.mouse.move(12, ib["y"], steps=4)
        p.mouse.up()
        p.wait_for_timeout(300)
        d = c.dlg()
        c.check("dialog: selecting the text with the mouse and releasing outside does not dismiss it", d["open"], f"open={d['open']}")

    # focus restore for a dialog opened from a normal button
    c.reset("files")
    p = c.open("#einstellungen")
    c.tap("#rebootBtn")
    c.wait_dlg("Laufwerk neu starten?")
    c.tap("#dNo")
    c.wait_dlg_closed()
    foc = c.ev("() => document.activeElement ? document.activeElement.id : ''")
    c.check("dialog opened from a button: focus returns to that button", foc == "rebootBtn", f"focus is on #{foc}")


# ================================================================== 6. search and sorting

@test
def t06_search_sort(c: Ctx):
    c.reset("files")
    p = c.open()
    files = {f["name"]: f for f in c.mock.state()["files"]}
    order = [r["name"] for r in c.rows()]
    c.check("sort: default is by name, German collation (Ü next to U, case-insensitive)", order == NAME_ORDER,
            [n[:12] for n in order])

    p.fill("#search", "3mf")
    c.wait("() => document.querySelectorAll('#list .row').length === 4")
    ui = c.ui()
    c.check("search: filters the list and shows 'x von y'", ui["rows"] == 4 and ui["count"] == "4 von 14"
            and all("3mf" in r["name"].lower() for r in c.rows()), f"{ui['rows']} rows, '{ui['count']}'")
    c.shot("search")
    p.fill("#search", "WÜRFEL")
    p.wait_for_timeout(150)
    c.check("search: case-insensitive incl. umlauts", [r["name"] for r in c.rows()] == ["Würfel.3mf"], c.ui()["count"])
    p.wait_for_timeout(3600)
    c.check("search: filter and input survive the status poll", [r["name"] for r in c.rows()] == ["Würfel.3mf"]
            and p.input_value("#search") == "WÜRFEL")
    p.fill("#search", "<b>xyz")
    p.wait_for_timeout(150)
    ui = c.ui()
    c.check("search: 'Keine Treffer' state", ui["stateH"] == "Keine Treffer" and ui["stateP"] == "Keine Datei enthält „<b>xyz“."
            and ui["count"] == "0 von 14" and ui["rows"] == 0 and not c.ev("() => !!document.querySelector('#list .state b')"),
            f"'{ui['stateH']}' / '{ui['stateP']}' / '{ui['count']}'")
    c.shot("search_no_hits")
    p.fill("#search", "")
    p.wait_for_timeout(150)
    c.check("search: clearing restores the list", c.ui()["rows"] == 14 and c.ui()["count"] == "14")

    def names():
        return [r["name"] for r in c.rows()]

    def mono(vals, desc):
        return all((a >= b) if desc else (a <= b) for a, b in zip(vals, vals[1:]))

    if c.phone:
        ui = c.ui()
        c.check("sort on phone: select instead of column headers", ui["sortM"] and not ui["cols"])
        for value, key, desc in (("size,-1", "size", True), ("size,1", "size", False), ("mtime,-1", "mtime", True),
                                 ("mtime,1", "mtime", False), ("name,-1", "name", True), ("name,1", "name", False)):
            p.select_option("#sortM", value)
            p.wait_for_timeout(120)
            got = names()
            ok = got == (NAME_ORDER[::-1] if desc else NAME_ORDER) if key == "name" else \
                mono([files[n][key] for n in got], desc) and sorted(got) == sorted(files)
            c.check(f"sort on phone: {value}", ok, [n[:10] for n in got[:4]])
            if value == "size,-1":
                c.shot("sorted_by_size")
    else:
        def head(key):
            return c.ev("k => { const b = document.querySelector('#list .cols [data-sort=\"' + k + '\"]'); const u = b.querySelector('use'); return {sort: b.getAttribute('aria-sort'), icon: u ? u.getAttribute('href') : null, others: [...document.querySelectorAll('#list .cols [aria-sort]')].length}; }", key)
        for key, label in (("size", "Größe"), ("mtime", "Geändert"), ("name", "Name")):
            for click in (1, 2):
                c.tap(f'#list .cols [data-sort="{key}"]')
                p.wait_for_timeout(120)
                h = head(key)
                got = names()
                desc = (click == 1) if key != "name" else (click == 2)
                if key == "name":       # name was the active column (descending after mtime? no: fresh click = ascending)
                    ok = got == (NAME_ORDER[::-1] if desc else NAME_ORDER)
                else:
                    ok = mono([files[n][key] for n in got], desc) and sorted(got) == sorted(files)
                ok = ok and h["sort"] == ("descending" if desc else "ascending") and h["others"] == 1 \
                    and h["icon"] == ("#i-down" if desc else "#i-up")
                c.check(f"sort by column '{label}', click {click}: order, direction toggle, aria-sort", ok,
                        f"aria-sort={h['sort']} icon={h['icon']} first: {[n[:10] for n in got[:3]]}")
                if key == "size" and click == 1:
                    c.shot("sorted_by_size")
        role = c.ev("() => { const b = document.querySelector('#list .cols [aria-sort]'); return (b.getAttribute('role') || b.tagName.toLowerCase()) + ' in ' + (b.parentElement.getAttribute('role') || b.parentElement.tagName.toLowerCase()); }")
        c.check("sort: aria-sort sits on a columnheader (the only role that supports it)", "columnheader" in role, role)

    # numeric collation
    c.reset("empty", upload_kbps=0)
    c.open()
    c.pick("n10", "n2", "n1")
    c.wait_toast("3 Dateien gespeichert.", 15000)
    got = [r["name"] for r in c.rows()]
    c.check("sort: numbers inside names sort numerically (1, 2, 10)", got == ["teil 1.txt", "Teil 2.txt", "Teil 10.txt"], got)


# ================================================================== 7. settings

@test
def t07a_settings_view(c: Ctx):
    c.reset("files")
    p = c.open("#einstellungen")
    st = c.mock.state()["status"]
    d = st["drive"]
    heads = c.ev("() => [...document.querySelectorAll('#vSettings section.sec > h2')].map(h => h.textContent)")
    chips = c.ev("() => [...document.querySelectorAll('#vSettings .tabs a')].map(a => a.textContent)")
    dz = c.ev("() => { const s = document.querySelector('#s-danger'); return s.classList.contains('dangerzone') && !!s.querySelector('#fmtBtn') && !!s.querySelector('#flashBtn') && !document.querySelector('#s-sys #fmtBtn, #s-sys #flashBtn'); }")
    c.check("settings: four sections plus a separate danger zone", heads == ["Netzwerk", "Speicher & USB", "Sicherheit", "System", "Gefahrenbereich"]
            and chips == heads[:4] and dz, heads)
    ui = c.ui()
    c.check("settings: files view, FAB and settings link hidden", ui["settings"] and not ui["files"] and not ui["fab"]
            and not c.ev("() => document.querySelector('#navSettings').getClientRects().length > 0"))
    v = c.ev("() => Object.fromEntries(['netTitle','netSub','netAddr','netHost','suMedium','suUsed','suFree','sysVer','sysUp','sysHeap','secAp','secTypes','fmtSub','foot'].map(i => [i, document.getElementById(i).textContent]).concat([['swWrite', document.getElementById('swWrite').checked], ['swAuto', document.getElementById('swAuto').checked]]))")
    used = d["total"] - d["free"]
    ok = (v["netTitle"] == "Verbunden mit „MeinWLAN“" and v["netAddr"] == "http://192.0.2.152"
          and v["netHost"] == "http://datadrive-mock.local" and "Empfang" in v["netSub"]
          and v["suMedium"] == "microSD-Karte (SDABC)" and v["suUsed"] == f"{fsize(used)} von {fsize(d['total'])}"
          and v["suFree"] == fsize(d["free"]) and v["sysVer"] == "0.2.0" and v["sysHeap"].endswith(" kB")
          and v["swWrite"] is True and v["swAuto"] is True and "aller Typen" in v["secTypes"]
          and "microSD-Karte" in v["fmtSub"] and v["foot"] == "WiFi DataDrive 0.2.0")
    c.check("settings: values match the mock status", ok, {k: v[k] for k in ("netTitle", "netSub", "netAddr", "suMedium", "suUsed", "suFree", "sysUp", "sysHeap")})
    c.shot("settings", full=True)
    c.shot("settings_top")
    c.overflow_check("settings")
    nm = c.ev(JS_NAMES)
    c.check("settings: every control has an accessible name", not nm["bad"], nm["bad"][:3])
    c.check("settings: no JS console errors", not c.console, c.console[:3])

    hist0 = c.ev("() => history.length")
    for href in ("#s-usb", "#s-sec", "#s-sys", "#s-net"):
        c.ev("() => scrollTo(0, 0)")
        p.wait_for_timeout(100)
        c.tap(f'.tabs a[href="{href}"]')
        p.wait_for_timeout(1100)
        g = c.ev("id => { const h = document.querySelector(id + ' > h2').getBoundingClientRect(), t = document.querySelector('header.top').getBoundingClientRect(); const max = document.documentElement.scrollHeight - innerHeight; return {top: Math.round(h.top), header: Math.round(t.bottom), y: Math.round(scrollY), max: Math.round(max), vh: innerHeight, hash: location.hash, settings: !document.querySelector('#vSettings').hidden}; }", href)
        ok = g["settings"] and g["hash"] == "#einstellungen" and \
            ((g["header"] - 1 <= g["top"] <= g["vh"] * 0.6) or g["y"] >= g["max"] - 2 or (href == "#s-net" and g["top"] >= g["header"]))
        c.check(f"settings: chip {href} scrolls to its section (heading below the sticky header)", ok, g)
        if href == "#s-sys":
            c.shot("settings_system")
    hist = c.ev("() => history.length")
    p.go_back()
    p.wait_for_timeout(400)
    ui = c.ui()
    c.check("settings: one browser 'Back' leaves the settings after using the chips", ui["files"],
            f"history grew by {hist - hist0} entries; after Back: hash '{ui['hash']}', settings visible={ui['settings']}")
    c.ev("() => scrollTo(0, document.documentElement.scrollHeight)")
    p.wait_for_timeout(200)
    c.shot("settings_danger")

    c.reset("ap_setup")
    c.open("#einstellungen")
    v = c.ev("() => ['netTitle','netSub','netAddr','secAp'].map(i => document.getElementById(i).textContent)")
    c.check("settings in ap_setup: no Wi-Fi configured, setup network shown", v[0] == "Kein WLAN eingerichtet"
            and v[1] == "Das Einrichtungs-WLAN „DataDrive-54D5“ ist aktiv." and v[2] == "http://192.168.4.1"
            and v[3].startswith("Das Einrichtungs-WLAN „DataDrive-54D5“ ist gerade aktiv."), v[:3])
    c.shot("settings_ap_setup")
    c.reset("flash_medium", types=".3mf,.gcode")
    c.open("#einstellungen")
    v = c.ev("() => ['suMedium','secTypes','fmtSub'].map(i => document.getElementById(i).textContent)")
    c.check("settings with internal flash and restricted types", v[0] == "Interner Speicher" and v[1].endswith(".3mf, .gcode.")
            and "dem internen Speicher" in v[2], v)


@test
def t07b_wifi_form(c: Ctx):
    c.reset("files")
    p = c.open("#einstellungen")
    msg = lambda: c.ev("() => { const m = document.querySelector('#wifiMsg'); return {t: m.textContent, c: m.className}; }")
    ssid = lambda: c.mock.state()["status"]["wifi"]["ssid"]
    c.tap("#wifiBtn")
    m = msg()
    c.check("wifi: empty SSID rejected", m["t"] == "Bitte den WLAN-Namen eingeben." and "err" in m["c"] and ssid() == "MeinWLAN", m["t"])
    p.fill("#ssid", "NeuesNetz")
    p.fill("#pass", "12345")
    c.tap("#wifiBtn")
    m = msg()
    c.check("wifi: 5-character password rejected", m["t"].startswith("Das Passwort muss mindestens 8 Zeichen") and "err" in m["c"]
            and ssid() == "MeinWLAN" and not any("/api/wifi" in u for _, u in c.requests), m["t"])
    c.shot("wifi_validation")
    c.mock.set(fail_next="IO")
    p.fill("#pass", "geheim123")
    c.tap("#wifiBtn")
    c.wait("() => document.querySelector('#wifiMsg').className.includes('err') && !document.querySelector('#wifiBtn').disabled")
    m = msg()
    c.check("wifi: server error (fail_next) is shown, no success", m["t"] == c.run.err["IO"] and "err" in m["c"]
            and ssid() == "MeinWLAN" and p.input_value("#pass") == "geheim123", m["t"])
    c.mock.set(delay_ms=1500)
    c.tap("#wifiBtn")
    p.wait_for_timeout(400)
    m = msg()
    dis = c.ev("() => document.querySelector('#wifiBtn').disabled")
    c.check("wifi: no success message before the server answered", m["t"] == "Wird gespeichert …" and dis, f"'{m['t']}' disabled={dis}")
    ok = c.wait("() => document.querySelector('#wifiMsg').className.includes('ok')", None, 15000)
    c.mock.set(delay_ms=0)
    m = msg()
    c.check("wifi: success message after server OK", ok and m["t"].startswith("Gespeichert. Das Laufwerk verbindet sich jetzt mit „NeuesNetz“")
            and ssid() == "NeuesNetz" and p.input_value("#pass") == "", m["t"])
    c.shot("wifi_saved")
    ok = c.wait("() => document.querySelector('#netTitle').textContent === 'Verbunden mit „NeuesNetz“'", None, 12000)
    c.check("wifi: network section follows the new connection", ok, c.text("#netTitle"))


@test
def t07c_switches_theme_log(c: Ctx):
    c.reset("files")
    p = c.open("#einstellungen")
    hw = lambda: c.mock.state()["host_writable"]
    chk = lambda: c.ev("() => document.querySelector('#swWrite').checked")
    c.tap("label.switch:has(#swWrite)")
    ok = c.wait_toast("Das USB-Gerät darf nur noch lesen.")
    c.check("host-writable switch: posts and reflects the mock state", ok and hw() is False and chk() is False,
            [t["text"] for t in c.toasts()])
    c.mock.set(fail_next="IO")
    c.clear_toasts()
    c.tap("label.switch:has(#swWrite)")
    ok = c.wait_toast(c.run.err["IO"])
    c.check("host-writable switch: reverts on a server error", ok and hw() is False and chk() is False and c.toasts()[-1]["err"],
            f"checked={chk()} mock={hw()}")
    c.tap("label.switch:has(#swWrite)")
    ok = c.wait_toast("Das USB-Gerät darf Dateien ändern.")
    c.check("host-writable switch: on again", ok and hw() is True and chk() is True)
    p.reload()
    c.wait("() => typeof S !== 'undefined' && !!S.st")
    c.check("host-writable switch: state after reload comes from the device", chk() is True)

    theme = lambda: c.ev("() => ({t: document.documentElement.dataset.theme || '', bg: getComputedStyle(document.body).backgroundColor, pressed: [...document.querySelectorAll('#theme button')].filter(b => b.getAttribute('aria-pressed') === 'true').map(b => b.textContent), ls: localStorage.getItem('dd-theme')})")
    c.tap('#theme button[data-t="dark"]')
    t = theme()
    c.check("theme: 'Dunkel' sets data-theme=dark", t["t"] == "dark" and t["bg"] == "rgb(18, 20, 22)" and t["pressed"] == ["Dunkel"], t)
    p.reload()
    c.wait("() => typeof S !== 'undefined' && !!S.st")
    t = theme()
    c.check("theme: persists after reload", t["t"] == "dark" and t["bg"] == "rgb(18, 20, 22)" and t["pressed"] == ["Dunkel"], t)
    c.tap('#theme button[data-t="light"]')
    p.emulate_media(color_scheme="dark")
    t = theme()
    c.check("theme: 'Hell' wins over a dark system setting", t["t"] == "light" and t["bg"] == "rgb(247, 247, 245)" and t["pressed"] == ["Hell"], t)
    c.tap('#theme button[data-t=""]')
    t = theme()
    c.check("theme: 'Automatisch' follows the system again", t["t"] == "" and t["bg"] == "rgb(18, 20, 22)" and t["ls"] is None
            and t["pressed"] == ["Automatisch"], t)
    p.emulate_media(color_scheme="light")

    c.tap("#logBtn")
    ok = c.wait("() => !document.querySelector('#log').hidden")
    log = c.text("#log")
    c.check("log viewer: loads the device log", ok and "WiFi DataDrive 0.2.0" in log and "MOCK SERVER" in log
            and c.text("#logBtn") == "Aktualisieren", f"{len(log)} chars")
    c.ev("() => document.querySelector('#log').scrollIntoView({block: 'center'})")
    c.shot("log")
    c.overflow_check("settings with log")


@test
def t07d_restart(c: Ctx):
    c.reset("files")
    p = c.open("#einstellungen")
    for code, text in (("BUSY", "Es läuft gerade eine Dateiübertragung. Bitte warte, bis sie abgeschlossen ist."),
                       ("HOST_WRITING", "Das USB-Gerät schreibt gerade auf das Laufwerk. Bitte warte, bis es fertig ist.")):
        c.mock.set(fail_next=code)
        c.tap("#rebootBtn")
        c.wait_dlg("Laufwerk neu starten?")
        c.tap("#dYes")
        c.wait_derr()
        d = c.dlg()
        c.check(f"restart refused with {code}: dialog stays open with a readable message", d["open"] and d["err"] == text
                and not c.mock.state()["restarting"] and c.ui()["notice"] == "", d["err"])
        if code == "BUSY":
            c.shot("restart_refused")
        c.tap("#dNo")
        c.wait_dlg_closed()
    c.tap("#rebootBtn")
    c.wait_dlg("Laufwerk neu starten?")
    c.shot("restart_dialog")
    c.tap("#dYes")
    t0 = time.time()
    ok = c.wait_notice("Das Laufwerk startet neu …", 5000)
    c.check("restart: confirm shows the notice 'Das Laufwerk startet neu …'", ok and not c.dlg()["open"], c.ui()["notice"])
    c.shot("restart_notice")
    ok = c.wait_toast("Das Laufwerk ist wieder bereit.", 30000)
    dt = time.time() - t0
    gone = c.wait_no_notice(5000)
    t = [x["text"] for x in c.toasts()]
    c.check("restart: toast 'Das Laufwerk ist wieder bereit.' after the mock is back, notice disappears",
            ok and gone and t.count("Das Laufwerk ist wieder bereit.") == 1 and not any(x["err"] for x in c.toasts()),
            f"{dt:.1f} s, toasts {t}")
    c.tap("a.back")
    c.wait_title(T_USB)
    c.check("restart: files view shows the state after the restart", c.ui()["title"] == T_USB and c.ui()["notice"] == "", c.ui()["title"])


@test
def t07e_format(c: Ctx):
    c.reset("files")
    p = c.open("#einstellungen")
    c.tap("#fmtBtn")
    ok = c.wait_dlg("Speicher formatieren?")
    d = c.dlg()
    c.check("format: confirmation dialog with input", ok and d["input"] == "" and d["yes"] == "Alles löschen" and "danger" in d["yesCls"]
            and "LÖSCHEN" in d["body"] and not c.dialogs()[:-1], f"'{d['title']}' focus #{d['focus']}")
    p.fill("#dInput", "FALSCH")
    c.tap("#dYes")
    c.wait_derr()
    d = c.dlg()
    c.check("format: wrong confirmation word rejected", d["open"] and d["err"] == "Bitte genau das Wort LÖSCHEN eingeben."
            and c.mock.state()["file_count"] == 14 and not any("/api/format" in u for _, u in c.requests), d["err"])
    c.shot("format_dialog_error")
    p.fill("#dInput", "LÖSCHEN")
    c.tap("#dYes")
    ok = c.wait_notice("Das Laufwerk startet neu …", 6000)
    st = c.mock.state()
    c.check("format: correct word formats (mock empty) and shows the restart notice", ok and st["file_count"] == 0 and not c.dlg()["open"],
            f"files in mock: {st['file_count']}")
    ok = c.wait_toast("Das Laufwerk ist wieder bereit.", 30000) and c.wait_no_notice(5000)
    c.check("format: UI recovers after the restart", ok)

    c.reset("usb_active")
    p = c.open("#einstellungen")
    c.tap("#fmtBtn")
    ok = c.wait_dlg(T_TAKEOVER)
    c.check("format in USB mode: take-over dialog first", ok, c.dlg()["title"])
    c.tap("#dYes")
    ok = c.wait_dlg("Speicher formatieren?")
    c.check("format in USB mode: then the format confirmation", ok and c.mock.state()["owner"] == "app", c.dlg()["title"])
    c.tap("#dNo")
    c.wait_dlg_closed()
    p.wait_for_timeout(3500)
    st = c.mock.state()
    c.check("format in USB mode, format cancelled: the USB device gets the drive back",
            st["owner"] == "usb", f"owner after cancelling the format: {st['owner']} (files {st['file_count']})")

    c.reset("no_fs")
    p = c.open()
    c.tap('#list .state a[href="#einstellungen"]')
    c.wait("() => !document.querySelector('#vSettings').hidden")
    c.tap("#fmtBtn")
    c.wait_dlg("Speicher formatieren?")
    p.fill("#dInput", "löschen")
    c.tap("#dYes")
    ok = c.wait_toast("Das Laufwerk ist wieder bereit.", 30000)
    c.tap("a.back")
    ok = ok and c.wait_title(T_USB)
    c.check("format from the 'Speicher nicht lesbar' state: works, storage usable afterwards", ok and c.mock.state()["fs_ok"],
            c.ui()["title"])


@test
def t07f_maintenance(c: Ctx):
    c.reset("files")
    p = c.open("#einstellungen")
    c.tap("#flashBtn")
    ok = c.wait_dlg("Wartungsmodus starten?")
    d = c.dlg()
    c.check("maintenance: confirm dialog", ok and d["yes"] == "Wartungsmodus starten" and "danger" in d["yesCls"], d["title"])
    c.shot("maintenance_dialog")
    c.overflow_check("maintenance dialog")
    c.tap("#dYes")
    ok = c.wait_notice("Der Wartungsmodus ist aktiv. Zum Beenden das Laufwerk aus- und wieder einstecken.", 6000)
    p.wait_for_timeout(7000)
    n = c.ui()["notice"]
    c.shot("maintenance_notice")
    c.check("maintenance: persistent notice about unplugging (still there after 7 s, no other notice)",
            ok and n == "Der Wartungsmodus ist aktiv. Zum Beenden das Laufwerk aus- und wieder einstecken."
            and c.mock.state()["restarting"] and not c.toasts(), n)
    c.mock.set(online=1)
    ok = c.wait_toast("Das Laufwerk ist wieder bereit.", 12000) and c.wait_no_notice(5000)
    c.check("maintenance: UI recovers when the device is back", ok)


# ================================================================== 8. connection loss, slow device

@test
def t08a_offline(c: Ctx):
    c.reset("files")
    p = c.open()
    c.mock.set(online=0)
    t0 = time.time()
    ok = c.wait_notice("Keine Antwort vom Laufwerk", 20000)
    c.check("offline: notice 'Keine Antwort vom Laufwerk …' appears", ok, f"after {time.time() - t0:.1f} s")
    c.shot("offline_notice")
    c.overflow_check("offline notice")
    c.check("offline: file list stays visible", c.ui()["rows"] == 14)

    c.clear_toasts()
    c.tap("#modeBtn")
    ok = c.wait("() => window.__toasts.length > 0", None, 25000)
    t = c.toasts()
    c.shot("offline_error_toast")
    c.check("offline: 'USB-Laufwerk bereitstellen' gives an error toast, no false success", ok and all(x["err"] for x in t)
            and not any("bereitgestellt" in x["text"] for x in t) and c.ui()["title"] == T_APP and c.mock.state()["owner"] == "app",
            [x["text"] for x in t])
    c.wait("() => !document.querySelector('#modeBtn').disabled", None, 25000)

    c.clear_toasts()
    c.act("LEER.TXT", "del")
    c.wait_dlg("Datei löschen?")
    c.tap("#dYes")
    c.wait_derr(25000)
    d = c.dlg()
    c.check("offline: delete shows the error in the dialog, nothing deleted", d["open"] and d["err"] == "Keine Antwort vom Laufwerk."
            and "LEER.TXT" in c.mock.names() and not c.toasts(), d["err"])
    c.shot("offline_dialog_error")
    c.tap("#dNo")
    c.wait_dlg_closed()
    c.act("README", "ren")
    c.wait_dlg("Datei umbenennen")
    p.fill("#dInput", "README2")
    c.tap("#dYes")
    c.wait_derr(25000)
    d = c.dlg()
    c.check("offline: rename shows the error in the dialog, nothing renamed", d["open"] and d["err"] == "Keine Antwort vom Laufwerk."
            and "README" in c.mock.names() and not c.toasts(), d["err"])
    c.tap("#dNo")
    c.wait_dlg_closed()
    c.pick("small")
    c.wait("() => !S.working", None, 25000)
    pn = c.panel()
    c.check("offline: upload is refused with a clear message", pn["items"] and "Keine Verbindung" in pn["items"][0]["err"]
            and not any("gespeichert" in x for x in pn["toasts"]), pn["items"][0]["err"] if pn["items"] else "")
    c.tap("#upClose")
    c.check("offline: rows unchanged after the failed actions", c.ui()["rows"] == 14 and len(c.mock.names()) == 14)

    c.mock.set(online=1)
    t0 = time.time()
    ok = c.wait_no_notice(15000)
    c.check("back online: notice disappears without reload", ok, f"after {time.time() - t0:.1f} s")
    c.clear_toasts()
    c.wait("() => !document.querySelector('#modeBtn').disabled", None, 10000)
    c.tap("#modeBtn")
    ok = c.wait_toast("Das USB-Laufwerk ist bereitgestellt.", 10000)
    c.check("back online: actions work again", ok and c.mock.state()["owner"] == "usb" and c.ui()["title"] == T_USB)

    # activate while offline
    c.mock.set(online=0)
    c.wait_notice("Keine Antwort vom Laufwerk", 20000)
    c.clear_toasts()
    c.tap("#modeBtn")
    ok = c.wait("() => window.__toasts.length > 0", None, 25000)
    t = c.toasts()
    c.check("offline: 'Dateiverwaltung aktivieren' gives an error toast, mode unchanged", ok and all(x["err"] for x in t)
            and c.ui()["title"] == T_USB and c.mock.state()["owner"] == "usb" and not c.dlg()["open"], [x["text"] for x in t])
    c.tap("#navSettings")
    c.wait("() => !document.querySelector('#vSettings').hidden")
    c.clear_toasts()
    c.tap("label.switch:has(#swWrite)")
    ok = c.wait("() => window.__toasts.length > 0", None, 25000)
    c.check("offline: host-writable switch reverts with an error toast", ok and c.toasts()[-1]["err"]
            and c.ev("() => document.querySelector('#swWrite').checked") is True and c.mock.state()["host_writable"] is True,
            [x["text"] for x in c.toasts()])
    c.mock.set(online=1)
    c.check("back online (settings): notice disappears", c.wait_no_notice(15000))


@test
def t08b_slow_device(c: Ctx):
    c.reset("files", delay_ms=3000)
    p = c.open(wait=False)
    p.wait_for_timeout(900)
    ui = c.ui()
    c.check("slow device: loading state while waiting for the first status", ui["title"] == "Verbindung wird hergestellt …"
            and ui["skel"] == 3 and ui["btn"] is None, f"'{ui['title']}', skeletons {ui['skel']}")
    c.shot("loading")
    ok = c.wait("() => !!S.st && document.querySelectorAll('#list .row').length === 14", None, 25000)
    c.check("slow device: list appears", ok)
    bb = p.locator("#modeBtn").bounding_box()
    x, y = bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2
    c.point(x, y)
    p.wait_for_timeout(120)
    c.point(x, y)
    p.wait_for_timeout(400)
    ui = c.ui()
    c.check("slow device: mode button shows spinner and is disabled", ui["btnDisabled"] and ui["btnSpin"] and ui["btn"] == "Wird bereitgestellt …",
            f"'{ui['btn']}' disabled={ui['btnDisabled']} spinner={ui['btnSpin']}")
    c.shot("mode_spinner")
    t0 = time.time()
    c.tap("#navSettings")
    ok = c.wait("() => !document.querySelector('#vSettings').hidden", None, 1500)
    c.tap("a.back")
    ok = ok and c.wait("() => !document.querySelector('#vFiles').hidden", None, 1500)
    c.check("slow device: UI stays responsive while the request is pending", ok and time.time() - t0 < 3, f"{time.time() - t0:.2f} s for settings and back")
    spurious = []
    t0 = time.time()
    while time.time() - t0 < 40 and not any("bereitgestellt" in t["text"] for t in c.toasts()):
        n = c.ui()["notice"]
        if n and n not in spurious:
            spurious.append(n)
        p.wait_for_timeout(250)
    posts = [u for m, u in c.requests if m == "POST" and "/api/mode" in u]
    c.check("slow device: two clicks send one request", len(posts) == 1 and c.mock.state()["owner"] == "usb", posts)
    c.check("slow device: switch finishes with the normal toast", any("bereitgestellt" in t["text"] for t in c.toasts())
            and c.ui()["title"] == T_USB, f"{time.time() - t0:.1f} s")
    c.check("slow device (3 s per answer): no false 'Keine Antwort' notice", not spurious, spurious[:1])

    # activation: spinner before and after the dialog
    c.clear_toasts()
    c.wait("() => !document.querySelector('#modeBtn').disabled", None, 15000)
    c.tap("#modeBtn")
    p.wait_for_timeout(400)
    ui = c.ui()
    c.check("slow device: activation shows 'Wird aktiviert …' until the device answers", ui["btn"] == "Wird aktiviert …" and ui["btnDisabled"] and ui["btnSpin"], ui["btn"])
    c.wait_dlg(T_TAKEOVER, 20000)
    c.tap("#dNo")

    # dialog: double click on the confirm button, cancel button while the request runs
    c.reset("files", delay_ms=1500)
    p = c.open()
    c.wait("() => document.querySelectorAll('#list .row').length === 14", None, 15000)
    c.act("LEER.TXT", "del")
    c.wait_dlg("Datei löschen?")
    bb = p.locator("#dYes").bounding_box()
    x, y = bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2
    c.point(x, y)
    p.wait_for_timeout(100)
    c.point(x, y)
    p.wait_for_timeout(300)
    d = c.dlg()
    c.shot("dialog_pending")
    c.check("slow device: confirm button disabled while the request runs", d["open"] and d["yesDisabled"], f"yes disabled={d['yesDisabled']}")
    c.check("slow device: 'Abbrechen' cannot dismiss the dialog while the request is still running", d["noDisabled"],
            f"'Abbrechen' disabled={d['noDisabled']} (a cancelled dialog does not stop the request)")
    c.wait_dlg_closed(20000)
    c.wait_toast("Gelöscht.", 20000)
    posts = [u for m, u in c.requests if m == "POST" and "/api/delete" in u]
    c.check("slow device: double click on 'Löschen' sends one request", len(posts) == 1, len(posts))

    c.reset("files", other_upload=1)
    c.open()
    c.tap("#modeBtn")
    ok = c.wait("() => window.__toasts.length > 0", None, 8000)
    t = c.toasts()
    c.check("upload by another client (BUSY): error toast, mode unchanged", ok and t[-1]["err"] and c.mock.state()["owner"] == "app",
            t[-1]["text"] if t else "")


# ================================================================== 9. accessibility

@test
def t09a_accessibility(c: Ctx):
    c.reset("files", upload_kbps=30)
    p = c.open()
    nm = c.ev(JS_NAMES)
    c.check("a11y files view: every button/link/input has an accessible name", not nm["bad"], f"{nm['n']} controls {nm['bad'][:3]}")
    nested = c.ev("() => document.querySelectorAll('[role=button] button, [role=button] a, button button').length")
    c.check("a11y: no interactive element nested inside another one (row role=button contains a button)", nested == 0,
            f"{nested} nested controls")
    h1 = c.ev("() => document.querySelector('#vFiles h1').textContent")
    c.check("a11y: page heading text is clean", h1 == "Meine Dateien", repr(h1))

    if not c.phone:
        def walk(n, label):
            none, seen = [], []
            for _ in range(n):
                p.keyboard.press("Tab")
                f = c.ev(JS_FOCUS)
                if not f:
                    continue
                seen.append(f"{f['el'].split('.')[0]}({f['how'] or 'NONE'})")
                if not f["how"]:
                    none.append(f"{f['el']} '{f['text']}'")
            c.check(f"a11y {label}: visible focus indicator on every Tab stop", seen and not none,
                    f"{len(seen)} stops; without indicator: {none[:4]}" if none else f"{len(seen)} stops, e.g. {seen[:6]}")
        walk(12, "files view")
        c.shot("focus_ring")
        # focus inside the list is lost when the status poll re-renders it
        c.row("Benchy.3mf").focus()
        p.wait_for_timeout(3700)
        foc = c.ev("() => { const a = document.activeElement; return a && a.dataset && a.dataset.name ? a.dataset.name : (a ? a.tagName : ''); }")
        c.check("a11y: keyboard focus on a row survives the 3 s status poll", foc == "Benchy.3mf", f"focus after 3.7 s: {foc}")
        c.ev("() => location.hash = '#einstellungen'")
        p.wait_for_timeout(300)
        walk(24, "settings")
    else:
        sels = ["#navSettings", "#modeBtn", "#fab", "#list .row [data-more]", "#sortM", "#search"]
        res = c.ev(JS_TARGETS, sels)
        c.open_menu("Benchy.3mf")
        res += c.ev(JS_TARGETS, ["#menu [data-act]"])
        c.tap('#menu [data-act="del"]')
        c.wait_dlg("Datei löschen?")
        res += c.ev(JS_TARGETS, ["#dYes", "#dNo"])
        c.tap("#dNo")
        c.pick("big")
        c.wait("() => !!document.querySelector('[data-cancel]')")
        res += c.ev(JS_TARGETS, ["[data-cancel]"])
        c.tap("[data-cancel]")
        c.wait("() => !S.working", None, 10000)
        res += c.ev(JS_TARGETS, ["#upClose"])
        c.ev("() => location.hash = '#einstellungen'")
        p.wait_for_timeout(300)
        res += c.ev(JS_TARGETS, ["a.back", ".tabs a", "label.switch", "#swWrite", "#theme button", "#wifiBtn", "#logBtn", "#rebootBtn", "#fmtBtn", "#flashBtn"])
        for r in res:
            if r.get("missing"):
                c.check(f"touch target {r['sel']}: found", False, "element not visible")
            else:
                c.check(f"touch target {r['sel']} at least 40x40 px", r["w"] >= 40 and r["h"] >= 40, f"{r['w']}x{r['h']}")


@test
def t09b_contrast(c: Ctx):
    muted_files = ["#modeSub", "#stoSub", "#count", ".row .date" if not c.phone else ".row .meta", ".hint",
                   "#sortM" if c.phone else ".cols button:not([aria-sort])"]
    other_files = [".ficon:not(.model):not(.image):not(.doc):not(.archive):not(.media)", ".ficon.model", ".ficon.image",
                   ".ficon.doc", ".ficon.archive", ".ficon.media", ".btn.primary", "#toasts .toast:not(.err)", "#toasts .toast.err",
                   "#notice", "#uploads .s.ok", "#uploads .s.err", "#uploads .l1 .s:not(.ok)"]
    muted_set = [".sec > h2", ".item .txt p", ".kv span:last-child", "#theme button[aria-pressed=false]", "footer", "a.back"]
    other_set = [".dangerzone > h2", ".btn.outline-danger", "#notice.info", ".tabs a", "#wifiMsg.err", "#wifiMsg.ok"]
    dlg_sel = ["#dBody p", "#dYes.danger", "#dErr"]

    def run(label, muted, other):
        for scheme in ("light", "dark"):
            c.page.emulate_media(color_scheme=scheme)
            for group, sels, need in (("muted text", muted, True), ("other text", other, False)):
                res = c.ev(JS_CONTRAST, sels)
                bad = [f"{r['sel']} {r['ratio']}:1 ({r['fg']} on {r['bg']}, {r['px']})" for r in res if not r.get("missing") and r["ratio"] < 4.5]
                miss = [r["sel"] for r in res if r.get("missing")]
                lo = min((r["ratio"] for r in res if not r.get("missing")), default=0)
                c.check(f"contrast {label}, {scheme}: {group} at least 4.5:1", not bad and not miss,
                        "; ".join(bad) + (f" missing: {miss}" if miss else "") if bad or miss else f"lowest {lo}:1 of {len(res)}")
        c.page.emulate_media(color_scheme="light")

    c.reset("files", upload_kbps=0)
    p = c.open()
    c.pick("small", "bad", "exists")
    c.wait_dlg("Vorhandene Datei ersetzen?")
    c.tap("#dNo")
    c.wait("() => !S.working", None, 15000)
    c.ev("() => { toast('Beispiel für eine Meldung.'); toast('Beispiel für eine Fehlermeldung.', true); S.fails = 2; renderNotice(); }")
    run("files view", muted_files, other_files)
    c.shot("toasts_notice_panel")
    c.ev("() => { S.fails = 0; renderNotice(); }")
    c.tap("#upClose")
    c.act("LEER.TXT", "del")
    c.wait_dlg("Datei löschen?")
    c.ev("() => { document.querySelector('#dErr').textContent = 'Beispiel für einen Fehler.'; }")
    run("dialog", ["#dBody p"], ["#dYes.danger", "#dErr"])
    c.tap("#dNo")
    c.ev("() => { location.hash = '#einstellungen'; }")
    p.wait_for_timeout(300)
    c.ev("() => { S.restart = 'normal'; renderNotice(); const m = document.querySelector('#wifiMsg'); m.className = 'formmsg full err'; m.textContent = 'Fehler'; }")
    run("settings", muted_set, [s for s in other_set if s != "#wifiMsg.ok"])
    c.ev("() => { S.restart = ''; renderNotice(); const m = document.querySelector('#wifiMsg'); m.className = 'formmsg full ok'; m.textContent = 'Gespeichert'; }")
    run("settings (ok message)", [], ["#wifiMsg.ok"])
    c.reset("usb_active")
    c.open()
    run("locked state", [".state p", "#modeSub"], [".btn.primary", ".state h2"])


# ================================================================== 10. measurements

@test
def t10_measure(c: Ctx):
    run = c.run
    if not c.phone:
        with urllib.request.urlopen(run.base + "/", timeout=30) as r:
            body = r.read()
        run.measure.append(f"index.html: {len(body)} B raw, {len(gzip.compress(body, 9))} B gzip -9")
    c.reset("many")
    p = c.open(wait=False)
    ok = c.wait("() => window.__rowsPainted > 0", None, 20000)
    c.check("measure: 300-file list rendered", ok and c.ui()["rows"] == 300)
    nav = c.ev("() => { const n = performance.getEntriesByType('navigation')[0]; return {resp: n.responseEnd, dcl: n.domContentLoadedEventEnd, load: n.loadEventEnd, rows: window.__rowsAt, painted: window.__rowsPainted}; }")
    med = c.ev("() => { const ts = []; for (let i = 0; i < 9; i++) { const t = performance.now(); renderList(); document.body.offsetHeight; ts.push(performance.now() - t); } ts.sort((a, b) => a - b); return ts[4]; }")
    cdp = c.context.new_cdp_session(p)
    cdp.send("HeapProfiler.enable")
    cdp.send("HeapProfiler.collectGarbage")
    heap = cdp.send("Runtime.getHeapUsage")
    cdp.send("Performance.enable")
    met = {m["name"]: m["value"] for m in cdp.send("Performance.getMetrics")["metrics"]}
    run.measure.append(f"{c.prof} (many, 300 files): response end {nav['resp']:.0f} ms, DOMContentLoaded {nav['dcl']:.0f} ms, "
                       f"list in DOM {nav['rows']:.0f} ms, painted {nav['painted']:.0f} ms after navigation start; "
                       f"renderList()+layout median {med:.1f} ms; JS heap used {heap['usedSize'] / 1024:.0f} kB "
                       f"(total {heap['totalSize'] / 1024:.0f} kB), DOM nodes {met.get('Nodes', 0):.0f}")
    if c.phone:
        cdp.send("Emulation.setCPUThrottlingRate", {"rate": 4})
        p.reload()
        c.wait("() => window.__rowsPainted > 0", None, 30000)
        nav = c.ev("() => { const n = performance.getEntriesByType('navigation')[0]; return {dcl: n.domContentLoadedEventEnd, rows: window.__rowsAt, painted: window.__rowsPainted}; }")
        med = c.ev("() => { const ts = []; for (let i = 0; i < 5; i++) { const t = performance.now(); renderList(); document.body.offsetHeight; ts.push(performance.now() - t); } ts.sort((a, b) => a - b); return ts[2]; }")
        run.measure.append(f"phone with 4x CPU throttling (many): DOMContentLoaded {nav['dcl']:.0f} ms, list in DOM {nav['rows']:.0f} ms, "
                           f"painted {nav['painted']:.0f} ms; renderList()+layout median {med:.1f} ms (runs on every 3 s status poll)")
        cdp.send("Emulation.setCPUThrottlingRate", {"rate": 1})
    c.reset("truncated")
    p = c.open(wait=False)
    c.wait("() => window.__rowsPainted > 0", None, 30000)
    n = c.ui()["rows"]
    med = c.ev("() => { const ts = []; for (let i = 0; i < 5; i++) { const t = performance.now(); renderList(); document.body.offsetHeight; ts.push(performance.now() - t); } ts.sort((a, b) => a - b); return ts[2]; }")
    run.measure.append(f"{c.prof} (truncated, {n} rows): renderList()+layout median {med:.1f} ms")


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
    for code in ("INVALID_FILE", "NO_SPACE", "IO"):
        run.err[code] = run.mock.error_text(code)
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
    if run.measure:
        print("\nMeasurements")
        for m in run.measure:
            print("  " + m)
    print(f"\n{len(run.results) - len(failed)}/{len(run.results)} checks passed in {time.time() - t_start:.0f} s")
    if failed:
        print("Failed:")
        for n in failed:
            print("  - " + n)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
