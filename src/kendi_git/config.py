"""Git configuration files (``.git/config``, ``~/.gitconfig``) and the identity of the person committing.

The file format: ``[section]`` or ``[section "Sub Section"]`` headers, ``name = value`` lines (a bare ``name`` means true), ``#`` and ``;`` comments, double-quoted
parts in values, the escapes ``\\n \\t \\b \\\\ \\"``, and a trailing backslash continuing a line. Section and variable names are case-insensitive, subsection names are
not. ``include`` directives are not followed (a documented limit).
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone, timedelta

from .errors import GitError
from .objects import Ident, local_tz

_SECTION = re.compile(r'\[\s*([A-Za-z0-9.-]+)(?:\s+"((?:[^"\\]|\\.)*)")?\s*\]')
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9-]*")


class ConfigFile:
    """One configuration file, kept as its original lines so that edits leave everything else untouched."""

    def __init__(self, path=None):
        self.path = path
        self.lines = []
        if path and os.path.exists(path):
            with open(path, encoding="utf-8", errors="surrogateescape") as f:
                self.lines = f.read().split("\n")
            if self.lines and self.lines[-1] == "":
                self.lines.pop()
        self.items = self._parse()

    # --- parsing ---------------------------------------------------------------------------------------------------------------------------
    def _parse(self):
        items = []                    # (section, subsection-or-None, name, value-or-None, first line index, last line index)
        section = sub = None
        i = 0
        while i < len(self.lines):
            line = self.lines[i]
            pos = 0
            while pos < len(line):
                rest = line[pos:].lstrip()
                if not rest or rest[0] in "#;":
                    break
                if rest[0] == "[":
                    m = _SECTION.match(rest)
                    if not m:
                        raise GitError(f"bad config line {i + 1} in {self.path or 'configuration'}")
                    section = m.group(1).lower()
                    sub = None
                    if m.group(2) is not None:
                        sub = re.sub(r"\\(.)", r"\1", m.group(2))
                    elif "." in section:                          # legacy [section.sub]: the subsection is lower-cased
                        section, sub = section.split(".", 1)
                    pos = len(line) - len(rest) + m.end()
                    continue
                m = _NAME.match(rest)
                if not m or section is None:
                    raise GitError(f"bad config line {i + 1} in {self.path or 'configuration'}")
                name = m.group(0).lower()
                after = rest[m.end():].lstrip()
                start = i
                if after[:1] == "=":
                    value, i = self._value(after[1:], i)
                else:
                    value = None
                    if after and after[0] not in "#;":
                        raise GitError(f"bad config line {i + 1} in {self.path or 'configuration'}")
                items.append((section, sub, name, value, start, i))
                line = ""
                break
            i += 1
        return items

    def _value(self, text, i):
        """Read a value the way git does: unquoted whitespace runs between words become spaces and the edges are trimmed, quotes toggle literal mode, a trailing
        backslash continues the line, and an unquoted ``#`` or ``;`` starts a comment."""
        out, has, quote, comment, space = [], False, False, False, 0
        buf, k = text, 0
        while True:
            if k >= len(buf):
                if quote:
                    raise GitError("missing closing quote in a configuration value")
                return "".join(out), i
            c = buf[k]
            k += 1
            if comment:
                continue
            if c in " \t\r\f\v" and not quote:
                if has:
                    space += 1
                continue
            if not quote and c in "#;":
                comment = True
                continue
            if space:
                out.append(" " * space)
                space = 0
            if c == "\\":
                if k >= len(buf):                                  # line continuation
                    if i + 1 >= len(self.lines):                   # the end of the file counts as the end of the line, as in git
                        if quote:
                            raise GitError("missing closing quote in a configuration value")
                        return "".join(out), i
                    i += 1
                    buf, k = self.lines[i], 0
                    continue
                esc = buf[k]
                k += 1
                if esc not in 'ntb\\"':
                    raise GitError(f"bad escape sequence in the configuration value near {buf!r}")
                out.append({"n": "\n", "t": "\t", "b": "\b", "\\": "\\", '"': '"'}[esc])
                has = True
                continue
            if c == '"':
                quote = not quote
                continue
            out.append(c)
            has = True

    # --- queries ---------------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def split_key(key):
        parts = key.split(".")
        if len(parts) < 2 or not parts[0] or not parts[-1]:
            raise GitError(f"invalid configuration key '{key}'")
        return parts[0].lower(), (".".join(parts[1:-1]) if len(parts) > 2 else None), parts[-1].lower()

    def get_all(self, key):
        section, sub, name = self.split_key(key)
        return [v for s, u, n, v, _a, _b in self.items if (s, u, n) == (section, sub, name)]

    def entries(self):
        for s, u, n, v, _a, _b in self.items:
            yield (f"{s}.{u}.{n}" if u is not None else f"{s}.{n}"), v

    # --- editing ---------------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def quote(value):
        needs = value != value.strip(" \t") or any(c in value for c in '#;"\\\n\t\b')
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t").replace("\b", "\\b")
        return f'"{escaped}"' if needs else escaped

    def set(self, key, value):
        """Set a single-valued variable, replacing the last occurrence if there is one, as ``git config key value`` does."""
        section, sub, name = self.split_key(key)
        if not _NAME.fullmatch(name):
            raise GitError(f"invalid variable name '{name}'")
        new_line = f"\t{name} = {self.quote(value)}"
        same = [it for it in self.items if (it[0], it[1], it[2]) == (section, sub, name)]
        if same:
            _s, _u, _n, _v, first, last = same[-1]
            self.lines[first:last + 1] = [new_line]
        else:
            in_section = [it for it in self.items if (it[0], it[1]) == (section, sub)]
            if in_section:
                at = in_section[-1][5] + 1
                self.lines.insert(at, new_line)
            else:
                header = f"[{section}]" if sub is None else '[%s "%s"]' % (section, sub.replace("\\", "\\\\").replace('"', '\\"'))
                self.lines += [header, new_line]
        self.items = self._parse()

    def save(self):
        if not self.path:
            raise GitError("this configuration has no file to save to")
        tmp = self.path + ".lock"
        with open(tmp, "w", encoding="utf-8", errors="surrogateescape") as f:
            f.write("\n".join(self.lines) + "\n")
        os.replace(tmp, self.path)


class Config:
    """The configuration a command sees: repository file over the global file over the system file."""

    def __init__(self, gitdir=None, env=None):
        env = os.environ if env is None else env
        self.env = env
        self.local = ConfigFile(os.path.join(gitdir, "config")) if gitdir else ConfigFile()
        layers = []
        if not env.get("GIT_CONFIG_NOSYSTEM"):
            layers.append(env.get("GIT_CONFIG_SYSTEM") or "/etc/gitconfig")
        if "GIT_CONFIG_GLOBAL" in env:
            if env["GIT_CONFIG_GLOBAL"]:
                layers.append(env["GIT_CONFIG_GLOBAL"])
        else:
            home = env.get("HOME") or os.path.expanduser("~")
            xdg = env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
            layers += [os.path.join(xdg, "git", "config"), os.path.join(home, ".gitconfig")]
        self.layers = [ConfigFile(p) for p in layers if p and p != os.devnull and os.path.isfile(p)] + [self.local]

    def get_all(self, key):
        out = []
        for layer in self.layers:
            out += layer.get_all(key)
        return out

    def get(self, key, default=None):
        values = self.get_all(key)
        return values[-1] if values else default

    def get_bool(self, key, default=False):
        v = self.get_all(key)
        if not v:
            return default
        v = v[-1]
        if v is None:
            return True
        low = v.strip().lower()
        if low in ("true", "yes", "on", "1"):
            return True
        if low in ("false", "no", "off", "0", ""):
            return False
        raise GitError(f"bad boolean config value '{v}' for '{key}'")

    def set(self, key, value):
        self.local.set(key, value)
        self.local.save()


# --- identity and dates -----------------------------------------------------------------------------------------------------------------------------
_TZ = re.compile(r"^([+-])(\d{2}):?(\d{2})$")


def _tz_minutes(text):
    m = _TZ.match(text)
    if not m:
        raise GitError(f"invalid time zone '{text}'")
    return (1 if m.group(1) == "+" else -1) * (int(m.group(2)) * 60 + int(m.group(3)))


def _norm_tz(text):
    minutes = _tz_minutes(text if text[0] in "+-" else "+" + text)
    return "%s%02d%02d" % ("+" if minutes >= 0 else "-", abs(minutes) // 60, abs(minutes) % 60)


def parse_date(text, now=None):
    """Return ``(unix time, tz string like '+0100')`` for the date formats ``GIT_AUTHOR_DATE`` accepts: ``<unix> <+zzzz>``, ``@<unix>``, ISO 8601 and RFC 2822."""
    text = text.strip()
    m = re.fullmatch(r"@(\d+)\s+([+-]?\d{2}:?\d{2})(?:\s.*)?", text)         # the strict form commit objects use, any number of digits
    if m:
        return int(m.group(1)), _norm_tz(m.group(2))
    m = re.fullmatch(r"@?(\d{9,})(?:\s+([+-]?\d{2}:?\d{2}))?(?:\s.*)?", text)  # a bare number only counts as a timestamp from 9 digits on (git's rule)
    if m:
        return int(m.group(1)), (_norm_tz(m.group(2)) if m.group(2) else local_tz(int(m.group(1))))
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?\s*(Z|[+-]\d{2}:?\d{2})?", text)
    if m:
        y, mo, d, h, mi, sec = (int(x) if x else 0 for x in m.groups()[:6])
        zone = m.group(7)
        if zone is None:
            local = time.mktime((y, mo, d, h, mi, sec, 0, 0, -1))
            return int(local), local_tz(int(local))
        minutes = 0 if zone == "Z" else _tz_minutes(zone)
        tz = timezone(timedelta(minutes=minutes))
        stamp = int(datetime(y, mo, d, h, mi, sec, tzinfo=tz).timestamp())
        return stamp, "%s%02d%02d" % ("+" if minutes >= 0 else "-", abs(minutes) // 60, abs(minutes) % 60)
    m = re.fullmatch(r"(?:[A-Za-z]{3},\s+)?(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})\s+(\d{2}):(\d{2})(?::(\d{2}))?\s+([+-]\d{4})", text)
    if m:
        months = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
        mon = months.index(m.group(2).lower()) + 1 if m.group(2).lower() in months else None
        if mon is None:
            raise GitError(f"unrecognised date '{text}'")
        minutes = _tz_minutes(m.group(7))
        tz = timezone(timedelta(minutes=minutes))
        stamp = int(datetime(int(m.group(3)), mon, int(m.group(1)), int(m.group(4)), int(m.group(5)), int(m.group(6) or 0), tzinfo=tz).timestamp())
        return stamp, m.group(7)
    raise GitError(f"unrecognised date '{text}'")


def _clean_ident(text):
    """Git removes ``<``, ``>`` and newlines anywhere and strips control characters, spaces and ``.,:;<>"\\'`` from both ends."""
    text = re.sub(r"[<>\n]", "", text)
    return text.strip("".join(chr(c) for c in range(33)) + ".,:;\"\\'")


def identity(role, config, env=None, now=None):
    """The author or committer: environment (``GIT_AUTHOR_NAME`` ...) over ``user.name`` / ``user.email`` over nothing (an error, as in git)."""
    env = os.environ if env is None else env
    role_upper = role.upper()
    name = env.get(f"GIT_{role_upper}_NAME") or config.get("user.name")
    email = env.get(f"GIT_{role_upper}_EMAIL") if f"GIT_{role_upper}_EMAIL" in env else config.get("user.email")
    if not name:
        raise GitError(f"Author identity unknown: set user.name and user.email (kgit config user.name \"Your Name\") or the GIT_{role_upper}_NAME variable")
    if email is None:
        raise GitError(f"Author identity unknown: set user.email (kgit config user.email you@example.com) or the GIT_{role_upper}_EMAIL variable")
    date = env.get(f"GIT_{role_upper}_DATE")
    if date:
        when, tz = parse_date(date)
    else:
        when = int(now if now is not None else time.time())
        tz = local_tz(when)
    name, email = _clean_ident(name), _clean_ident(email)
    if not name:
        raise GitError("empty ident name (for <%s>) not allowed" % email)
    return Ident(name, email, when, tz)
