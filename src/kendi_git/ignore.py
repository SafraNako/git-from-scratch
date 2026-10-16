"""``.gitignore`` semantics.

Sources, strongest first: the ``.gitignore`` files from the file's own directory up to the repository root (deeper wins), then ``.git/info/exclude``, then the user's
global excludes file. Within one source the *last* matching pattern wins and a leading ``!`` re-includes. A pattern with a slash in it (other than a trailing one) is
anchored to the directory of its ``.gitignore``; one without matches the base name at any depth; a trailing slash restricts it to directories. ``*`` and ``?`` do not
cross a slash, ``**`` as a whole path segment spans directories, ``[...]`` is a character class. Git never descends into an excluded directory, so nothing inside it
can be re-included: a path is ignored as soon as any of its parent directories is.
"""
from __future__ import annotations

import os
import re
import stat

_POSIX = {"alnum": "a-zA-Z0-9", "alpha": "a-zA-Z", "blank": " \\t", "cntrl": "\\x00-\\x1f\\x7f", "digit": "0-9", "graph": "!-~", "lower": "a-z", "print": " -~",
          "punct": "!-/:-@\\[-`{-~", "space": " \\t\\n\\r\\f\\v", "upper": "A-Z", "xdigit": "0-9A-Fa-f"}


def _bracket(pat, i, pathname=True):
    """Translate the bracket expression starting at ``pat[i] == '['``; returns ``(regex, next index)`` or ``None`` when it never closes (then nothing matches)."""
    j = i + 1
    negate = j < len(pat) and pat[j] in "!^"
    if negate:
        j += 1
    items = []
    first = True
    while True:
        if j >= len(pat):
            return None
        c = pat[j]
        if c == "]" and not first:
            j += 1
            break
        first = False
        if c == "\\":
            if j + 1 >= len(pat):
                return None
            items.append(re.escape(pat[j + 1]))
            j += 2
        elif c == "[" and pat.startswith("[:", j):
            end = pat.find(":]", j + 2)
            if end == -1:
                items.append(re.escape("["))
                j += 1
            else:
                name = pat[j + 2:end]
                if name not in _POSIX:
                    return None
                items.append(_POSIX[name])
                j = end + 2
        elif j + 2 < len(pat) and pat[j + 1] == "-" and pat[j + 2] != "]":
            hi_index = j + 2
            if pat[hi_index] == "\\" and hi_index + 1 < len(pat):
                hi_index += 1
            lo, hi = pat[j], pat[hi_index]
            if lo > hi:                                    # an empty range matches nothing, which is not an error
                items.append("")
            else:
                items.append(re.escape(lo) + "-" + re.escape(hi))
            j = hi_index + 1
        else:
            items.append(re.escape(c))
            j += 1
    body = "".join(items)
    slash = "(?!/)" if pathname else ""
    if negate:
        return slash + "[^" + body + ("/" if pathname else "") + "]", j
    return (slash + "[" + body + "]" if body else "(?!)"), j


def translate(pattern, pathname=True):
    """A glob as a regular expression source, or ``None`` when it can never match. With ``pathname`` (gitignore rules) ``*`` and ``?`` stop at a slash and ``**`` spans
    directories; without it (pathspecs) wildcards match slashes too."""
    out = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*" and not pathname:
            while i < n and pattern[i] == "*":
                i += 1
            out.append(".*")
        elif c == "*":
            j = i
            while j < n and pattern[j] == "*":
                j += 1
            stars = j - i
            at_segment_start = i == 0 or pattern[i - 1] == "/"
            at_segment_end = j == n or pattern[j] == "/"
            if stars >= 2 and at_segment_start and at_segment_end:
                if j == n:
                    out.append(".*")                       # trailing "**" (or the whole pattern): everything below
                else:
                    out.append("(?:.*/)?")                  # "**/": zero or more directories
                    j += 1                                  # the slash is part of the construct
            else:
                out.append("[^/]*")
            i = j
        elif c == "?":
            out.append("[^/]" if pathname else ".")
            i += 1
        elif c == "[":
            res = _bracket(pattern, i, pathname)
            if res is None:
                return None
            out.append(res[0])
            i = res[1]
        elif c == "\\":
            if i + 1 >= n:
                return None
            out.append(re.escape(pattern[i + 1]))
            i += 2
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


def _literal_prefix_length(text):
    """Git's ``simple_length``: how many leading characters contain no glob character (``* ? [`` or a backslash)."""
    for i, c in enumerate(text):
        if c in "*?[\\":
            return i
    return len(text)


class Pattern:
    """One line of an ignore file.

    Anchored patterns are matched the way git does it: the literal text before the first glob character is compared on its own, and only the *rest* of the pattern goes
    through the glob matcher, against the rest of the path. That is not just an optimisation: it changes what a ``**`` right after the literal text means (``a**/b``
    matches ``ab`` because the remaining ``**/b`` then starts a pattern), a quirk the comparison with real git exposed.
    """
    __slots__ = ("negated", "dir_only", "anchored", "regex", "literal", "rest", "text", "ignorecase")

    def __init__(self, line, ignorecase=False):
        self.text = line
        self.ignorecase = ignorecase
        self.negated = False
        if line.startswith("!"):
            self.negated, line = True, line[1:]
        self.dir_only = line.endswith("/")
        if self.dir_only:
            line = line[:-1]
        self.anchored = "/" in line
        if line.startswith("/"):
            line = line[1:]
        flags = re.DOTALL | (re.IGNORECASE if ignorecase else 0)
        if self.anchored:
            n = _literal_prefix_length(line)
            self.literal, self.rest = line[:n], line[n:]
        else:
            self.literal, self.rest = "", line
        source = translate(self.rest) if self.rest else ""
        self.regex = re.compile(source, flags) if source is not None and line else None

    def matches(self, rel, is_dir):
        """``rel`` is the path relative to the directory the pattern's file lives in."""
        if self.regex is None or (self.dir_only and not is_dir):
            return False
        if not self.anchored:
            return self.regex.fullmatch(rel.rsplit("/", 1)[-1]) is not None
        head, tail = rel[:len(self.literal)], rel[len(self.literal):]
        if len(head) != len(self.literal) or (head.lower() != self.literal.lower() if self.ignorecase else head != self.literal):
            return False
        if not self.rest:
            return tail == ""
        return self.regex.fullmatch(tail) is not None


def _trim_trailing_spaces(line):
    """Trailing spaces are dropped unless escaped with a backslash (tabs are not)."""
    last_space = None
    i = 0
    while i < len(line):
        c = line[i]
        if c == "\\":
            i += 1
            last_space = None
            if i >= len(line):
                return line
        elif c == " ":
            if last_space is None:
                last_space = i
        else:
            last_space = None
        i += 1
    return line if last_space is None else line[:last_space]


def parse_patterns(text, ignorecase=False):
    if text.startswith("﻿"):
        text = text[1:]
    patterns = []
    for raw in text.split("\n"):
        line = _trim_trailing_spaces(raw[:-1] if raw.endswith("\r") else raw)     # a CR before the newline is dropped, as in git (found by the oracle)
        if not line or line.startswith("#"):
            continue
        patterns.append(Pattern(line, ignorecase))           # "\\#x" and "\\!x" need no special care: the glob translation reads the backslash as an escape
    return patterns


def _read(path):
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode):               # git does not follow a symlinked .gitignore
            return ""
        with open(path, "rb") as f:
            return f.read().decode("utf-8", "surrogateescape")
    except OSError:
        return ""


class IgnoreRules:
    def __init__(self, worktree, gitdir, config=None, env=None):
        self.worktree = worktree
        env = os.environ if env is None else env
        self.ignorecase = bool(config and config.get_bool("core.ignorecase", False))
        self._cache = {}
        self.fixed = []                                  # (patterns relative to the root) from info/exclude and the global file, in this order
        exclude = os.path.join(gitdir, "info", "exclude") if gitdir else None
        sources = [exclude]
        glob = config.get("core.excludesfile") if config else None
        if glob:
            sources.append(os.path.expanduser(glob))
        else:
            home = env.get("HOME") or os.path.expanduser("~")
            xdg = env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
            sources.append(os.path.join(xdg, "git", "ignore"))
        for path in sources:
            if path and os.path.isfile(path):
                self.fixed.append(parse_patterns(_read(path), self.ignorecase))

    def _dir_patterns(self, rel_dir):
        if rel_dir not in self._cache:
            path = os.path.join(self.worktree, rel_dir, ".gitignore") if rel_dir else os.path.join(self.worktree, ".gitignore")
            self._cache[rel_dir] = parse_patterns(_read(path), self.ignorecase)
        return self._cache[rel_dir]

    def _decide(self, patterns, rel, is_dir):
        for pat in reversed(patterns):
            if pat.matches(rel, is_dir):
                return not pat.negated
        return None

    def _match(self, path, is_dir):
        """The verdict of the ignore files for exactly this path (ignoring its parents): True, False, or None for no opinion."""
        parts = path.split("/")
        for depth in range(len(parts) - 1, -1, -1):                # deepest directory first
            rel_dir = "/".join(parts[:depth])
            verdict = self._decide(self._dir_patterns(rel_dir), "/".join(parts[depth:]), is_dir)
            if verdict is not None:
                return verdict
        for patterns in self.fixed:
            verdict = self._decide(patterns, path, is_dir)
            if verdict is not None:
                return verdict
        return None

    def is_ignored(self, path, is_dir=False):
        """Is the (untracked) path ignored? ``path`` is relative to the work tree, with ``/`` separators."""
        parts = path.split("/")
        for depth in range(1, len(parts)):
            if self._match("/".join(parts[:depth]), True):
                return True
        return bool(self._match(path, is_dir))

    def forget(self):
        self._cache.clear()
