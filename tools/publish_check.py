#!/usr/bin/env python3
"""Safety check before publishing this repository (docs/19_sicherer_veroeffentlichungsprozess.md).

Checks the current tree and, by default, every commit and every file version (blob) reachable
from any ref. Standard library only, Python 3.10+, Windows and Linux.

Modes
    (default)      HEAD tree + full history (commits, all blobs ever reachable from any ref)
    --tree-only    staged content (the index) + current identity; fast, used by the pre-commit
                   hook. With --msg-file FILE the commit message is checked as well; add
                   --msg-only to check only the message and the identity (commit-msg hook).
    --ci           full check without the private terms file; private terms are read from the
                   environment variable PUBLISH_PRIVATE_TERMS (one term per line) if it is set

Severity
    ERROR fails the run (exit 1). WARNING is printed but only fails with --strict.
    Exit 0: no failing findings. Exit 2: usage error or git failure.

What is checked
    identity      author and committer of every commit (or of the next commit in --tree-only)
                  are exactly --name/--email; no co-author trailers in commit messages
    tool-name     names of code assistants, their vendors and model families (ERROR);
                  a word class that is often a leftover of such tools (WARNING); "User-Agent"
                  and similar are ignored
    email         e-mail addresses except noreply addresses, example domains and --allow-email
    local-path    absolute user paths of Windows, Linux and macOS
    private-term  terms from a file OUTSIDE the repository (default
                  %USERPROFILE%/.datadrive/publish_secrets.txt); a term is never printed,
                  only its number ("private term #3")
    secret        private keys, token patterns, pd_secrets.h, key files; Wi-Fi passwords in
                  configuration files (WARNING)
    file          build output, sdkconfig, tool folders, backup/, blobs over --max-blob-size,
                  binaries other than images and fonts
    legal         LICENSE (MIT with a real name), LICENSE-LIO.md, THIRD_PARTY_NOTICES.md,
                  HAFTUNGSAUSSCHLUSS.md, licence headers of the third-party code
    placeholder   <NAME>, <OWNER>, <REPO>, TODO and similar in README files (WARNING)
    link          relative links and anchors in all *.md files resolve

Found values are masked in the output (e-mail addresses, user names, secrets), so the output can
be pasted into an issue or a CI log without repeating the problem.
"""

from __future__ import annotations

import argparse
import base64
import bisect
import fnmatch
import os
import re
import subprocess
import sys
import threading
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

DEFAULT_NAME = "Willi Liffers"
DEFAULT_EMAIL = "wliffers@users.noreply.github.com"
DEFAULT_MAX_BLOB = 1024 * 1024
TERMS_ENV = "PUBLISH_PRIVATE_TERMS"
# Identity GitHub uses for merge commits it creates (pull request test merges, web merges).
GITHUB_WEB_IDENTITY = ("GitHub", "noreply@github.com")

ERROR = "ERROR"
WARNING = "WARNING"

# ---------------------------------------------------------------------------------------------
# Tool names. The terms are obfuscated so this file does not match itself: the table below is
# base64 of lines "kind|severity|label|regex" (regexes are case-insensitive and get word
# boundaries), plus one line of tool folder names and one line of tool file names. To change a
# term, decode the table, edit it, encode it again.
# ---------------------------------------------------------------------------------------------
_TERMS_B64 = (
    "bmFtZXxFfENsYXVkZXxjbGF1ZGUKbmFtZXxFfEFudGhyb3BpY3xhbnRocm9waWMKbmFtZXxFfE9wZW5BSXxvcGVuWy0g"
    "XT9haQpuYW1lfEV8Q2hhdEdQVHxjaGF0Wy0gXT9ncHQKbmFtZXxFfEdQVCBtb2RlbHxncHRbLSBdP1xkKD86XC5cZCsp"
    "P28/KD86Wy0gXT8oPzptaW5pfHR1cmJvKSk/Cm5hbWV8V3xHUFQgKG1heSBiZSB0aGUgcGFydGl0aW9uIHRhYmxlKXxn"
    "cHRzPwpuYW1lfEV8Q29waWxvdHxjb3BpbG90Cm5hbWV8RXxHZW1pbml8Z2VtaW5pCm5hbWV8RXxCYXJkfGJhcmQKbmFt"
    "ZXxFfENvZGV4fGNvZGV4Cm5hbWV8RXxMbGFtYXxsbGFtYVxkKgpuYW1lfEV8TWlzdHJhbHxtaXN0cmFsCm5hbWV8RXxN"
    "aXh0cmFsfG1peHRyYWwKbmFtZXxFfERlZXBTZWVrfGRlZXBbLSBdP3NlZWsKbmFtZXxFfFF3ZW58cXdlblxkKgpuYW1l"
    "fFd8R3Jva3xncm9rCm5hbWV8RXxQZXJwbGV4aXR5fHBlcnBsZXhpdHkKbmFtZXxFfFdpbmRzdXJmfHdpbmRzdXJmCm5h"
    "bWV8RXxDb2RlaXVtfGNvZGVpdW0KbmFtZXxFfFRhYm5pbmV8dGFibmluZQpuYW1lfEV8QWlkZXJ8YWlkZXIKbmFtZXxF"
    "fEN1cnNvciBlZGl0b3J8Y3Vyc29yWy0gXSg/OmFpfGlkZXxlZGl0b3IpfGN1cnNvclwuKD86c2h8Y29tfHNvKQpuYW1l"
    "fEV8Q2xpbmV8Y2xpbmUKbmFtZXxFfFJvbyBDb2RlfHJvb1stIF0/Y29kZQpuYW1lfEV8Q29kZVdoaXNwZXJlcnxjb2Rl"
    "Wy0gXT93aGlzcGVyZXIKbmFtZXxFfFJlcGxpdHxyZXBsaXQKbmFtZXxFfEh1Z2dpbmcgRmFjZXxodWdnaW5nWy0gXT9m"
    "YWNlCm5hbWV8RXxNaWRqb3VybmV5fG1pZGpvdXJuZXkKbmFtZXxFfERBTEwtRXxkYWxsWy0gXT9lCm5hbWV8RXxtb2Rl"
    "bCBmYW1pbHkgU29ubmV0fHNvbm5ldApuYW1lfEV8bW9kZWwgZmFtaWx5IE9wdXN8b3B1cwpuYW1lfEV8bW9kZWwgZmFt"
    "aWx5IEhhaWt1fGhhaWt1Cm5hbWV8RXxMTE18bGxtcz8KbmFtZXxFfGxhcmdlIGxhbmd1YWdlIG1vZGVsfGxhcmdlWy0g"
    "XWxhbmd1YWdlWy0gXW1vZGVscz8KbmFtZXxFfFNwcmFjaG1vZGVsbHwoPzpncm8oPzpzc3zDnyllc1stIF0pP3NwcmFj"
    "aG1vZGVsbFx3KgpuYW1lfEV8S0ktQXNzaXN0ZW50fGtpWy0gXT9hc3Npc3RlbnRcdyoKbmFtZXxFfEFJIGFzc2lzdGFu"
    "dHxhaVstIF0/YXNzaXN0YW50cz8KbmFtZXxFfEFJLWdlbmVyYXRlZHxhaVstIF1nZW5lcmF0ZWQKbmFtZXxFfEtJLWdl"
    "bmVyaWVydHxraVstIF1nZW5lcmllcnRcdyoKbmFtZXxFfGdlbmVyYXRlZCB3aXRoIEFJfCg/OmdlbmVyYXRlZHxjcmVh"
    "dGVkfHdyaXR0ZW4pXHMrKD86d2l0aHxieSlccysoPzphbj9ccyspP2FpCm5hbWV8RXxjby1hdXRob3IgdHJhaWxlcnxj"
    "by1hdXRob3JlZC1ieQpsZWZ0b3ZlcnxXfEFnZW50fCg/OnN1YnxoYXVwdHxuZWJlbnx0ZWlsKT8tP2FnZW50KD8hdXIp"
    "XHcqCmxlZnRvdmVyfFd8SGF1cHRzaXR6dW5nfGhhdXB0c2l0enVuZ1x3Kgpmb2xkZXJ8RXwuY2xhdWRlIC5jdXJzb3Ig"
    "LmNvbnRpbnVlIC5jb2RlaXVtIC53aW5kc3VyZiAuY29waWxvdCAucm9vIC5jbGluZSAua2lybyAuYWlkZXIgLmdlbWlu"
    "aSAuY29kZXh8LQpmaWxlfEV8Y2xhdWRlLm1kIGFnZW50cy5tZCBnZW1pbmkubWQgLmN1cnNvcnJ1bGVzIC53aW5kc3Vy"
    "ZnJ1bGVzIC5jbGluZXJ1bGVzIC5yb29tb2RlcyBjb3BpbG90LWluc3RydWN0aW9ucy5tZCAuYWlkZXIuY29uZi55bWwg"
    "LmFpZGVyLmNoYXQuaGlzdG9yeS5tZCAuYWlkZXIuaW5wdXQuaGlzdG9yeSBjbGF1ZGVfZGVza3RvcF9jb25maWcuanNv"
    "bnwt"
)


@dataclass(frozen=True)
class Term:
    kind: str          # "name" (tool names) or "leftover" (warning class)
    severity: str
    label: str
    regex: re.Pattern  # case-insensitive, with word boundaries (paths, fallback)
    fast: re.Pattern   # for lowercased text, no lookbehind (the preceding character is checked
                       # by hand; this keeps the regex engine's fast literal-prefix search)
    needles: tuple     # lowercase substrings, one of which must occur; () = always run the regex


def _needles(rx: str) -> tuple:
    """Literal letters every match must contain, per top-level alternative (speed-up only).

    Returns () when an alternative has no literal run of 3+ letters; the regex then always runs.
    """
    branches, runs, run, depth, cls, i = [], [], "", 0, False, 0

    def close():
        nonlocal run
        if run:
            runs.append(run)
        run = ""

    while i < len(rx):
        c = rx[i]
        nxt = rx[i + 1] if i + 1 < len(rx) else ""
        if cls:
            cls = c != "]"
        elif c == "\\":
            close()
            i += 1
        elif c == "[":
            close()
            cls = True
        elif c == "(":
            close()
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "|" and depth == 0:
            close()
            branches.append(runs)
            runs = []
        elif depth == 0 and c.isalpha() and c.islower():
            if nxt in ("?", "*") or (nxt == "{" and rx[i + 2:i + 3] == "0"):
                close()
            else:
                run += c
        elif depth == 0 and c == "-" and run and nxt.isalpha():
            run += c
        else:
            close()
        i += 1
    close()
    branches.append(runs)
    out = []
    for b in branches:
        best = max(b, key=len, default="")
        if len(best) < 3:
            return ()
        out.append(best)
    return tuple(out)


def _load_terms():
    terms, folders, files = [], set(), set()
    for line in base64.b64decode("".join(_TERMS_B64)).decode("utf-8").splitlines():
        kind, sev, label, rx = line.split("|", 3)
        sev = ERROR if sev == "E" else WARNING
        if kind == "folder":
            folders.update(label.split())
        elif kind == "file":
            files.update(label.split())
        else:
            pat = re.compile(r"(?<![A-Za-z0-9])(?:" + rx + r")(?![A-Za-z0-9])", re.IGNORECASE)
            fast = re.compile(r"(?:" + rx + r")(?![a-z0-9])")
            terms.append(Term(kind, sev, label, pat, fast, _needles(rx)))
    return terms, frozenset(folders), frozenset(files)


TERMS, TOOL_FOLDERS, TOOL_FILES = _load_terms()
CO_AUTHOR_LABEL = "co-author trailer"
# Trailer line in a commit message; assembled from pieces so this file does not match itself.
RX_TRAILER = re.compile(r"(?im)^[ \t]*co-" + "author" + r"ed-by[ \t]*:")
_ALNUM = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
# "User-Agent", "user_agent", "user agent" are HTTP vocabulary, not tool leftovers.
_USER_BEFORE = re.compile(r"user[-_ ]?$", re.IGNORECASE)

# ---------------------------------------------------------------------------------------------
# Content patterns
# ---------------------------------------------------------------------------------------------
_DASH5 = "-" * 5
RX_PEM_PRIVATE = re.compile(_DASH5 + r"BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?" + _DASH5)
RX_PEM_OTHER = re.compile(_DASH5 + r"BEGIN [A-Z0-9 ]+" + _DASH5)
# (severity, label, regex, lowercase needles: the regex only runs if one of them occurs)
TOKEN_PATTERNS = [
    (ERROR, "GitHub token", re.compile(r"(?<![A-Za-z0-9_])gh[pousr]_[A-Za-z0-9]{36,}"),
     ("ghp_", "gho_", "ghu_", "ghs_", "ghr_")),
    (ERROR, "GitHub fine-grained token", re.compile(r"github_pat_[A-Za-z0-9_]{22,}"), ("github_pat_",)),
    (ERROR, "AWS access key id", re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])"), ("akia", "asia")),
    (ERROR, "AWS secret key", re.compile(r"(?i)aws_?secret_?access_?key\s*[=:]\s*['\"]?[A-Za-z0-9/+]{40}"), ("secret",)),
    (ERROR, "Slack token", re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"), ("xox",)),
    (ERROR, "Google API key", re.compile(r"AIza[0-9A-Za-z_\-]{35}"), ("aiza",)),
    (ERROR, "Stripe key", re.compile(r"[sr]k_live_[0-9A-Za-z]{20,}"), ("k_live_",)),
    (ERROR, "API secret key", re.compile(r"(?<![A-Za-z0-9_])sk-[A-Za-z0-9_\-]{32,}"), ("sk-",)),
    (WARNING, "JSON web token", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), ("eyj",)),
]
RX_EMAIL = re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![A-Za-z0-9-])")
# Placeholders like %USERPROFILE%, $env:USERNAME, <Name> are not matched: the user name class
# excludes % $ < { and quotes.
RX_WIN_PATH = re.compile(r"(?i)(?<![A-Za-z])[A-Za-z]:[\\/]+(?:Users|Dokumente und Einstellungen|Documents and Settings)[\\/]+([^\\/\s\"'<>%$`{}|*?:;,()\[\]]+)")
RX_UNIX_PATH = re.compile(r"(?<![\w.~:/-])/(home|Users)/([A-Za-z_][\w.@-]*)")
PATH_USER_OK = {"user", "username", "name", "you", "me", "runner", "example", "yourname",
                "your-name", "your_name", "benutzer", "benutzername", "nutzer", "public",
                "default", "all users", "shared", "admin", "ubuntu", "pi", "xxx", "..."}
RX_WIFI_SDK = re.compile(r"(?im)^\s*CONFIG_\w*(?:PASS|PASSWORD|PSK|SSID)\w*\s*=\s*\"[^\"]+\"")
RX_WIFI_DEFINE = re.compile(r"(?im)^\s*#\s*define\s+\w*(?:PASS|PASSWORD|PSK|SSID)\w*\s+\"[^\"]+\"")
RX_WIFI_ASSIGN = re.compile(r"(?im)(?<![A-Za-z0-9_])(?:wifi_?|wlan_?|sta_?|ap_?)?(?:pass|password|passwd|psk)=(?![\s\"'$%{<…]|$)\S")
CONFIG_SUFFIXES = (".h", ".conf", ".cfg", ".ini", ".env", ".txt", ".yml", ".yaml", ".json",
                   ".toml", ".properties", ".defaults", ".signed", ".diag", ".csv")

EMAIL_ALLOW_PATTERNS = [
    "*@users.noreply.github.com", "noreply@*", "no-reply@*", "*@noreply.*", "git@github.com",
    "*@example.com", "*@example.org", "*@example.net", "*@*.example.com", "*.example",
    "*.invalid", "*.test", "*.localhost",
]
FILE_EXT_TLDS = {"png", "jpg", "jpeg", "gif", "svg", "webp", "ico", "js", "mjs", "css", "html",
                 "htm", "json", "md", "txt", "py", "c", "h", "bin", "zip", "gz", "pdf", "ps1"}

BINARY_OK_EXT = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".bmp", ".svg", ".ttf",
                 ".otf", ".woff", ".woff2"}
KEY_FILE_EXT = {".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".der", ".ppk"}

REQUIRED_FILES = ["LICENSE", "LICENSE-LIO.md", "THIRD_PARTY_NOTICES.md", "HAFTUNGSAUSSCHLUSS.md"]
# (file, strings that must appear in its first 25 lines)
LICENCE_HEADERS = [
    ("firmware/components/st7735/esp_lcd_st7735.c", ["LICENSE.LILYGO", "MIT"]),
    ("firmware/components/display/font5x7.c", ["LICENSE.Adafruit_GFX", "BSD"]),
]
# (licence file, strings it must contain)
LICENCE_FILES = [
    ("firmware/components/st7735/LICENSE.LILYGO", ["MIT License", "Copyright", "Xinyuan-LilyGO"]),
    ("firmware/components/display/LICENSE.Adafruit_GFX", ["Copyright", "Adafruit Industries"]),
]
NOTICE_MENTIONS = ["firmware/components/st7735", "firmware/components/display/font5x7.c"]

RX_PLACEHOLDER = re.compile(r"<(?:NAME|OWNER|REPO|USER|YOUR[ _-]?\w*)>|\bOWNER/REPO\b|\byour-username\b|\b(?:TODO|FIXME|TBD|XXX)\b")
RX_LICENSE_COPYRIGHT = re.compile(r"(?im)^\s*Copyright\s+\(c\)\s+(\d{4}(?:\s*[-,]\s*\d{4})*)\s+(.+?)\s*$")


# ---------------------------------------------------------------------------------------------
# Findings and output
# ---------------------------------------------------------------------------------------------
@dataclass
class Hit:
    rule: str
    severity: str
    line: int          # 0 = whole file / no line
    key: str           # identity of the finding across file versions (never a secret)
    message: str


@dataclass
class Finding:
    severity: str
    rule: str
    where: str
    message: str


def mask_email(addr: str) -> str:
    local, _, domain = addr.partition("@")
    parts = domain.split(".")
    head = parts[0][:1] + "***" if len(parts) > 1 else domain
    return f"{local[:1]}***@{head}.{'.'.join(parts[1:])}" if len(parts) > 1 else f"{local[:1]}***@{head}"


def mask_user(name: str) -> str:
    return name[:1] + "***"


def short(sha: str | None) -> str:
    return sha[:8] if sha else "?"


class Report:
    def __init__(self) -> None:
        self.findings: list[Finding] = []

    def add(self, severity: str, rule: str, where: str, message: str) -> None:
        self.findings.append(Finding(severity, rule, where, message))

    def counts(self) -> tuple[int, int]:
        e = sum(1 for f in self.findings if f.severity == ERROR)
        return e, len(self.findings) - e

    def print(self) -> None:
        order = {ERROR: 0, WARNING: 1}
        for f in sorted(self.findings, key=lambda f: (order[f.severity], f.rule, f.where)):
            print(f"{f.severity:<7} [{f.rule}] {f.where}: {f.message}")


# ---------------------------------------------------------------------------------------------
# git helpers
# ---------------------------------------------------------------------------------------------
class GitError(RuntimeError):
    pass


def git(repo: Path, *args: str, binary: bool = False, check: bool = True):
    cmd = ["git", "-C", str(repo), "-c", "core.quotepath=off", *args]
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError as e:
        raise GitError("git not found in PATH") from e
    if check and p.returncode != 0:
        raise GitError(f"{' '.join(args[:3])}: {p.stderr.decode('utf-8', 'replace').strip()}")
    return p.stdout if binary else p.stdout.decode("utf-8", "replace")


def read_objects(repo: Path, shas: list[str]):
    """Yields (sha, type, size, data) for each object via one `git cat-file --batch` process."""
    if not shas:
        return
    p = subprocess.Popen(["git", "-C", str(repo), "cat-file", "--batch"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    def feed():
        try:
            for s in shas:
                p.stdin.write((s + "\n").encode())
            p.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    t = threading.Thread(target=feed, daemon=True)
    t.start()
    for _ in shas:
        header = p.stdout.readline().decode("ascii", "replace").split()
        if len(header) < 3:          # "<sha> missing"
            continue
        size = int(header[2])
        data = p.stdout.read(size)
        p.stdout.read(1)
        yield header[0], header[1], size, data
    p.stdout.close()
    p.wait()
    t.join()


def unquote_git_path(path: str) -> str:
    if len(path) >= 2 and path[0] == '"' and path[-1] == '"':
        raw = path[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape")
        return raw.encode("latin-1", "replace").decode("utf-8", "replace")
    return path


# ---------------------------------------------------------------------------------------------
# Scanning text
# ---------------------------------------------------------------------------------------------
def is_binary(data: bytes) -> bool:
    return b"\0" in data[:8000]


def decode_text(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.decode("utf-8", "replace")


def binary_strings(data: bytes) -> str:
    """Printable ASCII runs of 8+ characters, one per line (like `strings`)."""
    return "\n".join(m.group().decode("ascii") for m in re.finditer(rb"[\x20-\x7e]{8,}", data))


def email_allowed(addr: str, extra: list[str], identity_email: str) -> bool:
    a = addr.lower()
    if a == identity_email.lower():
        return True
    tld = a.rsplit(".", 1)[-1]
    if tld in FILE_EXT_TLDS:             # icon@2x.png, name@1.0.js and similar
        return True
    domain = a.split("@", 1)[1]
    for pat in EMAIL_ALLOW_PATTERNS + extra:
        p = pat.lower()
        if fnmatch.fnmatchcase(a, p) or (p.startswith("*.") and fnmatch.fnmatchcase(domain, p)):
            return True
    return False


class Scanner:
    def __init__(self, private_terms: list[str], allow_email: list[str], identity_email: str):
        self.private = [t.lower() for t in private_terms]
        self.allow_email = allow_email
        self.identity_email = identity_email

    def scan(self, text: str, binary: bool = False) -> list[Hit]:
        """Path-independent findings in one text. Line numbers start at 1."""
        starts = [0] + [m.end() for m in re.finditer("\n", text)]

        def line_of(pos: int) -> int:
            return 0 if binary else bisect.bisect_right(starts, pos)

        def line_text(pos: int) -> str:
            i = bisect.bisect_right(starts, pos) - 1
            end = starts[i + 1] if i + 1 < len(starts) else len(text)
            return text[starts[i]:end]

        hits: list[Hit] = []
        low = text.lower()

        def has(*needles: str) -> bool:      # cheap substring test before a regex pass
            return any(n in low for n in needles)

        soft = WARNING if binary else None    # random bytes in images can look like words
        same_len = len(low) == len(text)      # lower() keeps offsets for almost every text
        for term in TERMS:
            if term.needles and not has(*term.needles):
                continue
            if same_len:
                found = (m for m in term.fast.finditer(low)
                         if m.start() == 0 or low[m.start() - 1] not in _ALNUM)
            else:
                found = term.regex.finditer(text)
            for m in found:
                word = text[m.start():m.end()]
                if term.kind == "leftover":
                    ls = starts[bisect.bisect_right(starts, m.start()) - 1]
                    if _USER_BEFORE.search(text[max(ls, m.start() - 8):m.start()]):
                        continue
                    sev, what = WARNING, f"word that is often a tool leftover: \"{word}\""
                elif term.label == CO_AUTHOR_LABEL:
                    # in file text only a mention; a real trailer in a commit message is an
                    # error (scan_message)
                    sev, what = WARNING, f"co-author trailer text: \"{word}\""
                else:
                    sev, what = term.severity, f"tool or vendor name ({term.label}): \"{word}\""
                hits.append(Hit("tool-name", soft or sev, line_of(m.start()),
                                f"{term.label}:{word.lower()}", what))
        if "@" in text:
            for m in RX_EMAIL.finditer(text):
                addr = m.group()
                if not email_allowed(addr, self.allow_email, self.identity_email):
                    hits.append(Hit("email", ERROR, line_of(m.start()), addr.lower(),
                                    f"e-mail address {mask_email(addr)}"))
        if has("users", "settings", "einstellungen"):
            for m in RX_WIN_PATH.finditer(text):
                user = m.group(1).strip()
                if user.lower() in PATH_USER_OK:
                    continue
                hits.append(Hit("local-path", ERROR, line_of(m.start()), "win:" + user.lower(),
                                f"absolute user path (Windows, user {mask_user(user)})"))
        if has("/home/", "/users/"):
            for m in RX_UNIX_PATH.finditer(text):
                user = m.group(2)
                if user.lower() in PATH_USER_OK:
                    continue
                hits.append(Hit("local-path", ERROR, line_of(m.start()), f"{m.group(1)}:{user.lower()}",
                                f"absolute user path (/{m.group(1)}/{mask_user(user)})"))
        for n, term in enumerate(self.private, 1):
            pos = low.find(term)
            while pos != -1:
                hits.append(Hit("private-term", ERROR, line_of(pos), f"#{n}", f"private term #{n}"))
                pos = low.find(term, pos + 1)
        if has(_DASH5 + "begin"):
            for m in RX_PEM_PRIVATE.finditer(text):
                hits.append(Hit("secret", ERROR, line_of(m.start()), "pem-private", "PEM private key block"))
            for m in RX_PEM_OTHER.finditer(text):
                if not RX_PEM_PRIVATE.match(text, m.start()):
                    hits.append(Hit("secret", WARNING, line_of(m.start()), "pem:" + m.group()[11:-5],
                                    "PEM block (certificate or public key?); check that it is meant to be public"))
        for sev, label, rx, needles in TOKEN_PATTERNS:
            if has(*needles):
                for m in rx.finditer(text):
                    hits.append(Hit("secret", sev, line_of(m.start()), "token:" + label, f"looks like a {label}"))
        # Wi-Fi credentials; filtered by path in applies() (only non-example configuration files)
        for rx, needle in ((RX_WIFI_SDK, "config_"), (RX_WIFI_DEFINE, "define")):
            if has(needle):
                for m in rx.finditer(text):
                    name = re.split(r"[=\s\"]", m.group().strip().replace("#", "").replace("define", "", 1).strip())[0]
                    hits.append(Hit("secret-wifi", WARNING, line_of(m.start()), "wifi:" + name,
                                    f"Wi-Fi name or password with a value in a configuration file ({name})"))
        if has("pass=", "password=", "passwd=", "psk="):
            for m in RX_WIFI_ASSIGN.finditer(text):
                if "…" in line_text(m.start()) or "..." in line_text(m.start()):
                    continue
                hits.append(Hit("secret-wifi-assign", WARNING, line_of(m.start()), "wifi-assign",
                                "password= with a value in a configuration file"))
        # one finding per rule, key and line
        seen, out = set(), []
        for h in hits:
            k = (h.rule, h.key, h.line)
            if k not in seen:
                seen.add(k)
                out.append(h)
        return out

    def scan_message(self, text: str) -> list[Hit]:
        hits = [h for h in self.scan(text) if not h.rule.startswith("secret-wifi")]
        for m in RX_TRAILER.finditer(text):
            ln = text.count("\n", 0, m.start()) + 1
            hits = [h for h in hits if not (h.line == ln and h.key.startswith(CO_AUTHOR_LABEL))]
            hits.append(Hit("identity", ERROR, ln, "co-author", "co-author trailer in the commit message"))
        return hits


def applies(hit: Hit, path: str) -> bool:
    if hit.rule.startswith("secret-wifi"):
        name = path.rsplit("/", 1)[-1].lower()
        if "example" in name or "sample" in name or "template" in name:
            return False
        if hit.rule == "secret-wifi-assign":
            return name.endswith(CONFIG_SUFFIXES) or name.startswith((".env", "sdkconfig"))
        return True
    return True


# ---------------------------------------------------------------------------------------------
# Path rules
# ---------------------------------------------------------------------------------------------
def path_hits(path: str, private: list[str]) -> list[Hit]:
    parts = path.split("/")
    dirs, name = parts[:-1], parts[-1]
    lname = name.lower()
    ext = os.path.splitext(lname)[1]
    hits: list[Hit] = []

    def h(sev, rule, key, msg):
        hits.append(Hit(rule, sev, 0, key, msg))

    if lname == "pd_secrets.h":
        h(ERROR, "secret", "pd_secrets", "pd_secrets.h (Wi-Fi credentials) is tracked")
    if ext in KEY_FILE_EXT:
        h(ERROR, "secret", "keyfile", f"key or certificate file ({ext})")
    if lname == ".env" or lname.startswith(".env."):
        h(ERROR, "secret", "envfile", "environment file")
    if lname == "sdkconfig.signing_key":
        h(ERROR, "secret", "signing-key", "signing key overlay")
    for d in dirs:
        ld = d.lower()
        if ld.startswith("build") or ld in ("managed_components", "__pycache__", "node_modules",
                                            ".venv", "venv", "dist", "stage", "site"):
            if ld in ("dist", "stage", "site") and dirs.index(d) != 0:
                continue
            h(ERROR, "file", "build:" + ld, f"build output or generated folder '{d}/'")
            break
    if lname in ("sdkconfig", "sdkconfig.old"):
        h(ERROR, "file", "sdkconfig", f"generated '{name}' (only sdkconfig.defaults/.signed/.diag belong in git)")
    if ext in (".pyc", ".pyo", ".o", ".obj", ".elf", ".map", ".bin", ".log"):
        h(ERROR, "file", "artifact:" + ext, f"build artefact, log or image file ({ext})")
    for d in dirs:
        if d.lower() in TOOL_FOLDERS:
            h(ERROR, "file", "toolfolder:" + d.lower(), f"tool folder '{d}/'")
            break
        if d.lower() in (".idea",) or (d.lower() == ".vscode" and lname != "extensions.json"):
            h(WARNING, "file", "editor:" + d.lower(), f"editor settings '{d}/'")
            break
    if lname in TOOL_FILES:
        h(ERROR, "file", "toolfile:" + lname, f"tool instruction file '{name}'")
    if dirs and dirs[0].lower() == "backup":
        if lname.endswith(".txt"):
            h(WARNING, "file", "backup-txt", "file in backup/ (checksum list; decide whether it is published)")
        else:
            h(ERROR, "file", "backup", "file in backup/ (flash dumps are not published)")
    for term in TERMS:
        if term.kind == "name" and term.regex.search(path):
            h(term.severity, "tool-name", "path:" + term.label, f"tool or vendor name in the path ({term.label})")
    low = path.lower()
    for n, t in enumerate(private, 1):
        if t in low:
            h(ERROR, "private-term", f"path#{n}", f"private term #{n} in the path")
    return hits


# ---------------------------------------------------------------------------------------------
# Markdown links
# ---------------------------------------------------------------------------------------------
RX_FENCE = re.compile(r"^\s{0,3}(```|~~~)")
RX_MD_LINK = re.compile(r"!?\[(?:[^\[\]]|\[[^\]]*\])*\]\(\s*(<[^>]*>|[^)\s]+)(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")
RX_REF_DEF = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*(<[^>]*>|\S+)")
RX_HTML_REF = re.compile(r"""(?i)\b(?:href|src)\s*=\s*["']([^"']+)["']""")
RX_HTML_ANCHOR = re.compile(r"""(?i)<a\s[^>]*\b(?:id|name)\s*=\s*["']([^"']+)["']""")
RX_ATX = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
GITHUB_PAGES = {"releases", "issues", "pulls", "actions", "security", "wiki", "discussions",
                "tags", "compare", "commits", "blob", "tree", "raw", "projects", "milestones"}


def strip_code(lines: list[str]) -> list[str]:
    """Blank out fenced code blocks and inline code spans, keeping line numbers."""
    out, fence = [], None
    for ln in lines:
        m = RX_FENCE.match(ln)
        if fence:
            if m and m.group(1) == fence:
                fence = None
            out.append("")
            continue
        if m:
            fence = m.group(1)
            out.append("")
            continue
        out.append(re.sub(r"(`+)(?:(?!\1).)+?\1", lambda x: " " * len(x.group()), ln))
    return out


def gh_slug(heading: str) -> str:
    s = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)   # links -> text
    s = re.sub(r"<[^>]+>", "", s)                             # html tags
    s = s.strip().lower()
    s = re.sub(r"[^\w\- ]", "", s)
    return s.replace(" ", "-")


def md_anchors(text: str) -> set[str]:
    lines = text.splitlines()
    code = strip_code(lines)
    anchors, counts = set(), {}
    raw_headings = []
    in_fence = None
    for i, ln in enumerate(lines):
        fm = RX_FENCE.match(ln)
        if in_fence:
            if fm and fm.group(1) == in_fence:
                in_fence = None
            continue
        if fm:
            in_fence = fm.group(1)
            continue
        m = RX_ATX.match(ln)
        if m:
            raw_headings.append(m.group(2))
        elif i + 1 < len(lines) and ln.strip() and re.match(r"^\s{0,3}(=+|-+)\s*$", lines[i + 1]) \
                and not ln.lstrip().startswith(("|", "-", "*", ">")):
            raw_headings.append(ln.strip())
    for h in raw_headings:
        slug = gh_slug(h)
        n = counts.get(slug, 0)
        anchors.add(slug if n == 0 else f"{slug}-{n}")
        counts[slug] = n + 1
    for ln in code:
        for m in RX_HTML_ANCHOR.finditer(ln):
            anchors.add(m.group(1))
    return anchors


def check_links(report: Report, label: str, tree: dict[str, str], texts: dict[str, str]) -> None:
    paths = set(tree)
    dirs = set()
    for p in paths:
        parts = p.split("/")
        for i in range(1, len(parts)):
            dirs.add("/".join(parts[:i]))
    anchor_cache: dict[str, set[str]] = {}

    def anchors_of(p: str) -> set[str]:
        if p not in anchor_cache:
            anchor_cache[p] = md_anchors(texts.get(p, ""))
        return anchor_cache[p]

    for md in sorted(p for p in texts if p.lower().endswith(".md")):
        base = md.rsplit("/", 1)[0] if "/" in md else ""
        for lineno, ln in enumerate(strip_code(texts[md].splitlines()), 1):
            targets = [m.group(1) for m in RX_MD_LINK.finditer(ln)]
            targets += [m.group(1) for m in RX_REF_DEF.finditer(ln)]
            targets += [m.group(1) for m in RX_HTML_REF.finditer(ln)]
            for t in targets:
                t = t.strip("<>").strip()
                if not t or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", t) or t.startswith("//"):
                    continue
                t = t.split("?", 1)[0] if "?" in t.split("#", 1)[0] else t
                file_part, _, anchor = t.partition("#")
                file_part = urllib.parse.unquote(file_part)
                if file_part:
                    if file_part.startswith("/"):
                        target = file_part.lstrip("/")
                    else:
                        target = os.path.normpath(os.path.join(base, file_part)).replace("\\", "/")
                    target = target.rstrip("/")
                    # GitHub idiom: from a file at depth d, d+2 times "../" reaches the repository
                    # pages (README at the root: ../../releases/latest). Not a file, not checked.
                    rest = target
                    ups = 0
                    while rest.startswith("../"):
                        rest, ups = rest[3:], ups + 1
                    if ups == 2 and rest.split("/", 1)[0] in GITHUB_PAGES:
                        continue
                    if target.startswith("../") or target == "..":
                        report.add(ERROR, "link", f"{label} {md}:{lineno}", f"link leaves the repository: {t}")
                        continue
                    if target in ("", "."):
                        target = ""
                    if target and target not in paths and target not in dirs:
                        report.add(ERROR, "link", f"{label} {md}:{lineno}", f"link target does not exist: {t}")
                        continue
                else:
                    target = md
                if anchor and target.lower().endswith(".md") and target in paths:
                    a = urllib.parse.unquote(anchor)
                    if a not in anchors_of(target) and a.lower() not in anchors_of(target):
                        report.add(ERROR, "link", f"{label} {md}:{lineno}",
                                   f"anchor #{anchor} not found in {target}")


# ---------------------------------------------------------------------------------------------
# Tree checks: legal files, placeholders
# ---------------------------------------------------------------------------------------------
def check_legal(report: Report, label: str, tree: dict[str, str], texts: dict[str, str], name: str) -> None:
    for f in REQUIRED_FILES:
        if f not in tree:
            report.add(ERROR, "legal", f"{label} {f}", "required file is missing")
        elif not texts.get(f, "").strip():
            report.add(ERROR, "legal", f"{label} {f}", "required file is empty")
    lic = texts.get("LICENSE", "")
    if lic:
        if "MIT License" not in lic.strip().splitlines()[0]:
            report.add(ERROR, "legal", f"{label} LICENSE:1", "LICENSE does not start with 'MIT License'")
        m = RX_LICENSE_COPYRIGHT.search(lic)
        if not m:
            report.add(ERROR, "legal", f"{label} LICENSE", "no 'Copyright (c) <year> <name>' line")
        else:
            holder = m.group(2)
            ln = lic.count("\n", 0, m.start()) + 1
            if re.search(r"[<>\[\]{}]|\bNAME\b|\bOWNER\b|\bAUTHOR\b|your name|TODO|\[fullname\]", holder, re.I):
                report.add(ERROR, "legal", f"{label} LICENSE:{ln}", "copyright holder is a placeholder")
            elif name and name not in holder:
                report.add(WARNING, "legal", f"{label} LICENSE:{ln}",
                           f"copyright holder differs from the configured author name")
    for f, needles in LICENCE_HEADERS:
        if f not in tree:
            report.add(ERROR, "legal", f"{label} {f}", "third-party file with licence header is missing")
            continue
        head = "\n".join(texts.get(f, "").splitlines()[:25])
        for n in needles:
            if n not in head:
                report.add(ERROR, "legal", f"{label} {f}", f"licence header no longer mentions '{n}'")
    for f, needles in LICENCE_FILES:
        if f not in tree:
            report.add(ERROR, "legal", f"{label} {f}", "third-party licence file is missing")
            continue
        for n in needles:
            if n not in texts.get(f, ""):
                report.add(ERROR, "legal", f"{label} {f}", f"licence text no longer contains '{n}'")
    notices = texts.get("THIRD_PARTY_NOTICES.md", "")
    if notices:
        for n in NOTICE_MENTIONS:
            if n not in notices:
                report.add(ERROR, "legal", f"{label} THIRD_PARTY_NOTICES.md", f"no entry for {n}")


def check_placeholders(report: Report, label: str, texts: dict[str, str]) -> None:
    for p in sorted(texts):
        if not p.rsplit("/", 1)[-1].lower().startswith("readme"):
            continue
        for lineno, ln in enumerate(strip_code(texts[p].splitlines()), 1):
            for m in RX_PLACEHOLDER.finditer(ln):
                report.add(WARNING, "placeholder", f"{label} {p}:{lineno}", f"placeholder or leftover '{m.group()}'")


# ---------------------------------------------------------------------------------------------
# Main checks
# ---------------------------------------------------------------------------------------------
TREE_TEXT_NEEDED = set(REQUIRED_FILES) | {f for f, _ in LICENCE_HEADERS} | {f for f, _ in LICENCE_FILES}


def wants_text(path: str) -> bool:
    return path in TREE_TEXT_NEEDED or path.lower().endswith(".md")


def blob_file_hits(path: str, size: int, binary: bool, max_blob: int) -> list[Hit]:
    hits = []
    if size > max_blob:
        hits.append(Hit("file", ERROR, 0, "big", f"large blob: {size:,} bytes (limit {max_blob:,})"))
    if binary and os.path.splitext(path.lower())[1] not in BINARY_OK_EXT:
        hits.append(Hit("file", ERROR, 0, "binary", "binary file that is not an image or font"))
    return hits


def parse_ident(ident: str) -> tuple[str, str]:
    m = re.match(r"^(.*?)\s*<([^>]*)>", ident)
    return (m.group(1), m.group(2)) if m else (ident, "")


def check_identity(report: Report, where: str, role: str, name: str, email: str, args, allowed) -> None:
    if (name, email) == (args.name, args.email) or (name, email) in allowed:
        return
    shown = mask_email(email) if not email_allowed(email, [], args.email) else email
    report.add(ERROR, "identity", where, f"{role} is \"{name} <{shown}>\", expected \"{args.name} <{args.email}>\"")


def run_tree_only(repo: Path, args, scanner: Scanner, report: Report, private: list[str]) -> dict:
    allowed = set(args.allowed_identities)
    for var, role in (("GIT_AUTHOR_IDENT", "author"), ("GIT_COMMITTER_IDENT", "committer")):
        out = git(repo, "var", var, check=False).strip()
        if not out:
            report.add(ERROR, "identity", "next commit", f"{role} identity is not configured")
            continue
        n, e = parse_ident(out)
        check_identity(report, "next commit", role, n, e, args, allowed)

    if args.msg_file:
        raw = Path(args.msg_file).read_text(encoding="utf-8", errors="replace")
        comment = git(repo, "config", "--get", "core.commentChar", check=False).strip() or "#"
        if comment == "auto":
            comment = "#"
        lines = []
        for ln in raw.splitlines():
            if re.match(r"^" + re.escape(comment) + r" -+ >8 -+$", ln):
                break
            if not ln.startswith(comment):
                lines.append(ln)
        msg = "\n".join(lines)
        for h in scanner.scan_message(msg):
            report.add(h.severity, h.rule, f"commit message:{h.line}", h.message)
    if args.msg_only:
        return {"files": 0}

    tree: dict[str, str] = {}
    for entry in git(repo, "ls-files", "-s", "-z").split("\0"):
        if not entry:
            continue
        meta, path = entry.split("\t", 1)
        mode, sha, _stage = meta.split()
        if mode == "160000":
            continue
        tree[path] = sha
    texts: dict[str, str] = {}
    by_sha: dict[str, list[str]] = {}
    for p, s in tree.items():
        by_sha.setdefault(s, []).append(p)
    label = "index"
    for p in tree:
        for h in path_hits(p, private):
            report.add(h.severity, h.rule, f"{label} {p}", h.message)
    for sha, _typ, size, data in read_objects(repo, list(by_sha)):
        binary = is_binary(data)
        text = binary_strings(data) if binary else decode_text(data)
        hits = scanner.scan(text, binary)
        for p in by_sha[sha]:
            for h in blob_file_hits(p, size, binary, args.max_blob_size):
                report.add(h.severity, h.rule, f"{label} {p}", h.message)
            for h in hits:
                if applies(h, p):
                    report.add(h.severity, h.rule, f"{label} {p}:{h.line}" if h.line else f"{label} {p}", h.message)
            if not binary and wants_text(p):
                texts[p] = text
    check_legal(report, label, tree, texts, args.name)
    check_placeholders(report, label, texts)
    check_links(report, label, tree, texts)
    return {"files": len(tree)}


def run_full(repo: Path, args, scanner: Scanner, report: Report, private: list[str]) -> dict:
    allowed = set(args.allowed_identities)
    if args.ci:
        allowed.add(GITHUB_WEB_IDENTITY)
    if git(repo, "rev-parse", "--is-shallow-repository", check=False).strip() == "true":
        report.add(ERROR, "history", "repository", "shallow clone: history is incomplete (use fetch-depth: 0)")

    # --- commits: identity and messages
    raw = git(repo, "log", "--all", "--format=%H%x1f%an%x1f%ae%x1f%cn%x1f%ce%x1f%B%x1e")
    commits = 0
    for rec in raw.split("\x1e"):
        rec = rec.strip("\n")
        if not rec:
            continue
        sha, an, ae, cn, ce, body = rec.split("\x1f", 5)
        commits += 1
        where = f"commit {short(sha)}"
        check_identity(report, where, "author", an, ae, args, allowed)
        check_identity(report, where, "committer", cn, ce, args, allowed)
        for h in scanner.scan_message(body):
            report.add(h.severity, h.rule, f"{where} message:{h.line}", h.message)
    # annotated tags carry their own identity and message
    for line in git(repo, "for-each-ref", "refs/tags",
                    "--format=%(objecttype)%1f%(refname:short)%1f%(taggername)%1f%(taggeremail)").splitlines():
        typ, tag, tn, te = (line.split("\x1f") + ["", "", ""])[:4]
        if typ != "tag":
            continue
        te = te.strip("<>")
        check_identity(report, f"tag {tag}", "tagger", tn, te, args, allowed)
        body = git(repo, "for-each-ref", f"refs/tags/{tag}", "--format=%(contents)")
        for h in scanner.scan_message(body):
            report.add(h.severity, h.rule, f"tag {tag} message:{h.line}", h.message)

    # --- order of commits and the first (commit, path) of every blob
    order: dict[str, int] = {}
    first: dict[tuple[str, str], int] = {}          # (blob, path) -> commit index
    log = git(repo, "log", "--all", "--reverse", "--date-order", "-m", "--raw", "--no-abbrev",
              "--no-renames", "--format=%x01%H")
    cur = -1
    commit_list: list[str] = []
    for ln in log.splitlines():
        if ln.startswith("\x01"):
            sha = ln[1:].strip()
            if sha not in order:
                order[sha] = len(commit_list)
                commit_list.append(sha)
            cur = order[sha]
        elif ln.startswith(":") and "\t" in ln:
            meta, path = ln.split("\t", 1)
            f = meta.split()
            if len(f) < 5 or set(f[3]) == {"0"} or f[1] == "160000":
                continue
            key = (f[3], unquote_git_path(path))
            if key not in first or cur < first[key]:
                first[key] = cur

    # --- complete set of reachable objects (catches blobs only reachable through tags etc.)
    objs: dict[str, str] = {}
    for ln in git(repo, "rev-list", "--all", "--objects").splitlines():
        sha, _, path = ln.partition(" ")
        objs.setdefault(sha, path)
    occ: dict[str, list[tuple[str, int]]] = {}
    for (blob, path), idx in first.items():
        occ.setdefault(blob, []).append((path, idx))

    # --- HEAD tree
    head = git(repo, "rev-parse", "--verify", "-q", "HEAD", check=False).strip()
    tree: dict[str, str] = {}
    if head:
        for entry in git(repo, "ls-tree", "-r", "-z", "--full-tree", "HEAD").split("\0"):
            if not entry:
                continue
            meta, path = entry.split("\t", 1)
            mode, typ, sha = meta.split()
            if typ == "blob":
                tree[path] = sha
    for p, s in tree.items():
        occ.setdefault(s, [])
        if not any(pp == p for pp, _ in occ[s]):
            occ[s].append((p, order.get(head, len(commit_list))))
    label = f"HEAD {short(head)}"

    agg: dict[tuple, tuple[int, int, Hit]] = {}     # key -> (commit index, line, hit)
    in_head: dict[tuple, int] = {}                  # key -> first line in HEAD
    head_lines: dict[tuple, int] = {}               # key -> number of lines in HEAD
    texts: dict[str, str] = {}
    head_paths = {(p, s) for p, s in tree.items()}

    def record(hit: Hit, path: str, idx: int, blob: str):
        k = (hit.rule, hit.severity, path, hit.key)
        if k not in agg or idx < agg[k][0]:
            agg[k] = (idx, hit.line, hit)
        if (path, blob) in head_paths:
            in_head.setdefault(k, hit.line)
            head_lines[k] = head_lines.get(k, 0) + 1

    # path rules over every path that ever existed
    seen_paths: dict[str, int] = {}
    for blob, lst in occ.items():
        for p, idx in lst:
            if p not in seen_paths or idx < seen_paths[p]:
                seen_paths[p] = idx
    head_set = set(tree)
    for p, idx in seen_paths.items():
        for h in path_hits(p, private):
            k = (h.rule, h.severity, p, h.key)
            agg[k] = (idx, 0, h)
            if p in head_set:
                in_head[k] = 0

    # blobs: query types first, then read the blobs only
    candidates = [s for s in objs if s not in occ] + list(occ)
    types: dict[str, str] = {}
    out = subprocess.run(["git", "-C", str(repo), "cat-file", "--batch-check"],
                         input=("\n".join(candidates) + "\n").encode(), stdout=subprocess.PIPE).stdout
    for ln in out.decode("ascii", "replace").splitlines():
        f = ln.split()
        if len(f) >= 3:
            types[f[0]] = f[1]
    blobs = [s for s in candidates if types.get(s) == "blob"]
    for s in blobs:
        if s not in occ:
            occ[s] = [(objs.get(s) or "(unknown path)", len(commit_list))]
    for sha, _typ, size, data in read_objects(repo, blobs):
        binary = is_binary(data)
        text = binary_strings(data) if binary else decode_text(data)
        hits = scanner.scan(text, binary)
        for p, idx in occ[sha]:
            for h in blob_file_hits(p, size, binary, args.max_blob_size):
                record(h, p, idx, sha)
            for h in hits:
                if applies(h, p):
                    record(h, p, idx, sha)
            if (p, sha) in head_paths and not binary and wants_text(p):
                texts[p] = text

    for k, (idx, line, hit) in agg.items():
        path = k[2]
        sha = commit_list[idx] if 0 <= idx < len(commit_list) else None
        loc = f"{path}:{line}" if line else path
        if k in in_head:
            hl, n = in_head[k], head_lines.get(k, 1)
            more = f", {n - 1} more line(s)" if n > 1 else ""
            state = f"in current tree{f' (line {hl}{more})' if hl else ''}"
        else:
            state = "history only, not in current tree"
        report.add(hit.severity, hit.rule, f"{loc} [first in {short(sha)}; {state}]", hit.message)

    check_legal(report, label, tree, texts, args.name)
    check_placeholders(report, label, texts)
    check_links(report, label, tree, texts)
    return {"commits": commits, "blobs": len(blobs), "files": len(tree)}


def load_private_terms(args, report: Report) -> list[str]:
    terms: list[str] = []
    env = os.environ.get(TERMS_ENV)
    if env:
        terms += [t.strip() for t in env.splitlines() if t.strip() and not t.strip().startswith("#")]
    path = Path(args.private_terms) if args.private_terms else None
    if path is None and not args.ci:
        home = os.environ.get("USERPROFILE") or str(Path.home())
        path = Path(home) / ".datadrive" / "publish_secrets.txt"
    if path is not None:
        if path.is_file():
            for ln in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
                t = ln.strip()
                if t and not t.startswith("#"):
                    terms.append(t)
        elif not env:
            report.add(ERROR, "private-term", str(path.name),
                       "private terms file not found (create it, see docs/19_sicherer_veroeffentlichungsprozess.md, or use --ci)")
            return []
    if not terms and (path is not None or not args.ci):
        report.add(WARNING, "private-term", "private terms", "no private terms configured; that check did nothing")
    short_terms = [i for i, t in enumerate(terms, 1) if len(t) < 3]
    for i in short_terms:
        report.add(WARNING, "private-term", "private terms", f"private term #{i} is shorter than 3 characters (many false hits)")
    return terms


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="Safety check before publishing the repository.",
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="See the header of this file and docs/19_sicherer_veroeffentlichungsprozess.md.")
    ap.add_argument("--repo", help="repository to check (default: the repository of the current directory)")
    ap.add_argument("--tree-only", action="store_true", help="check only the staged content and the identity (fast)")
    ap.add_argument("--msg-file", help="commit message file to check (commit-msg hook)")
    ap.add_argument("--msg-only", action="store_true", help="with --tree-only: check only the message and the identity")
    ap.add_argument("--ci", action="store_true", help="CI mode: no private terms file required; uses $" + TERMS_ENV)
    ap.add_argument("--strict", action="store_true", help="warnings fail the run too")
    ap.add_argument("--name", default=DEFAULT_NAME, help=f"expected author and committer name (default: {DEFAULT_NAME})")
    ap.add_argument("--email", default=DEFAULT_EMAIL, help=f"expected author and committer e-mail (default: {DEFAULT_EMAIL})")
    ap.add_argument("--allow-identity", action="append", default=[], metavar="'NAME <EMAIL>'",
                    help="additional accepted identity (repeatable)")
    ap.add_argument("--allow-email", action="append", default=[], metavar="PATTERN",
                    help="additional accepted e-mail address, fnmatch pattern (repeatable)")
    ap.add_argument("--private-terms", help="file with private terms (default: %%USERPROFILE%%/.datadrive/publish_secrets.txt)")
    ap.add_argument("--max-blob-size", type=int, default=DEFAULT_MAX_BLOB, help="size limit per file version in bytes")
    args = ap.parse_args(argv)
    if args.msg_only and not (args.tree_only and args.msg_file):
        ap.error("--msg-only needs --tree-only and --msg-file")
    args.allowed_identities = [parse_ident(i) for i in args.allow_identity]

    try:
        if args.repo:
            repo = Path(args.repo)
        else:
            top = subprocess.run(["git", "rev-parse", "--show-toplevel"], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL).stdout.decode().strip()
            repo = Path(top) if top else Path(__file__).resolve().parent.parent
        git(repo, "rev-parse", "--git-dir")
        report = Report()
        private = load_private_terms(args, report)
        scanner = Scanner(private, args.allow_email, args.email)
        if args.tree_only:
            stats = run_tree_only(repo, args, scanner, report, [t.lower() for t in private])
            scope = "staged files" if not args.msg_only else "commit message"
        else:
            stats = run_full(repo, args, scanner, report, [t.lower() for t in private])
            scope = "HEAD tree and full history"
    except GitError as e:
        print(f"publish_check: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"publish_check: {e}", file=sys.stderr)
        return 2

    report.print()
    errors, warnings = report.counts()
    detail = ", ".join(f"{v} {k}" for k, v in stats.items())
    print(f"publish_check: {scope} ({detail}): {errors} error(s), {warnings} warning(s)")
    if errors or (args.strict and warnings):
        print("publish_check: FAILED" + (" (warnings count because of --strict)" if not errors else ""))
        return 1
    print("publish_check: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
