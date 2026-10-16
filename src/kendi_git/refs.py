"""References: ``HEAD``, branches, tags, ``packed-refs``.

A reference is a name pointing at an object id (a *direct* ref) or at another reference (a *symbolic* ref, ``HEAD`` normally pointing at a branch). Direct refs live
as small files below ``.git/refs/`` and, once packed, as lines in ``.git/packed-refs``; a loose file always wins over a packed line. Updates go through a ``.lock``
file created exclusively and renamed over the target, so two processes never write the same ref at once. Reflogs are not written (a documented limit).
"""
from __future__ import annotations

import os
import re

from .errors import GitError
from .objects import is_oid

_FORBIDDEN = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")
SYMREF = "ref: "
_UNSET = object()


def check_ref_format(name, onelevel=False):
    """The rules of ``git check-ref-format``; returns True when ``name`` is a valid reference name."""
    if not name or name == "@" or name.startswith("/") or name.endswith("/") or name.endswith(".") or "//" in name or ".." in name or "@{" in name:
        return False
    if _FORBIDDEN.search(name):
        return False
    parts = name.split("/")
    if len(parts) < 2 and not onelevel:
        return False
    return all(p and not p.startswith(".") and not p.endswith(".lock") for p in parts)


def check_branch_name(name):
    return check_ref_format("refs/heads/" + name) and not name.startswith("-") and name != "HEAD"


class RefStore:
    def __init__(self, gitdir):
        self.gitdir = gitdir

    def _path(self, name):
        return os.path.join(self.gitdir, *name.split("/"))

    # --- reading ---------------------------------------------------------------------------------------------------------------------------
    def read_raw(self, name):
        """``("sym", target)``, ``("oid", oid)`` or ``None`` for a name that is not (yet) a reference; loose files shadow packed lines."""
        path = self._path(name)
        try:
            with open(path, "rb") as f:
                text = f.read().decode("utf-8", "surrogateescape").strip()
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
            text = None
        if text is not None:
            if text.startswith(SYMREF):
                return "sym", text[len(SYMREF):].strip()
            if is_oid(text):
                return "oid", text
            raise GitError(f"reference {name} is corrupt (its file holds {text[:40]!r})")
        packed = self._packed()
        if name in packed:
            return "oid", packed[name][0]
        return None

    def read(self, name, depth=0):
        """The object id a reference finally points at, or ``None`` (an unborn branch, a missing ref)."""
        if depth > 5:
            raise GitError(f"reference {name} is a symbolic ref loop (or nested too deeply)")
        raw = self.read_raw(name)
        if raw is None:
            return None
        return self.read(raw[1], depth + 1) if raw[0] == "sym" else raw[1]

    def head(self):
        """``("ref", "refs/heads/main")`` when ``HEAD`` names a branch (born or not), ``("detached", oid)`` otherwise."""
        raw = self.read_raw("HEAD")
        if raw is None:
            raise GitError("HEAD is missing: this is not a git repository")
        return ("ref", raw[1]) if raw[0] == "sym" else ("detached", raw[1])

    def head_oid(self):
        return self.read("HEAD")

    def current_branch(self):
        kind, value = self.head()
        return value[len("refs/heads/"):] if kind == "ref" and value.startswith("refs/heads/") else None

    def _packed(self):
        path = os.path.join(self.gitdir, "packed-refs")
        out = {}
        try:
            with open(path, "rb") as f:
                lines = f.read().decode("utf-8", "surrogateescape").split("\n")
        except FileNotFoundError:
            return out
        last = None
        for line in lines:
            if not line or line.startswith("#"):
                continue
            if line.startswith("^"):
                if last is None or not is_oid(line[1:]):
                    raise GitError("packed-refs is corrupt (a peeled line without a ref)")
                out[last] = (out[last][0], line[1:])
                continue
            oid, _, name = line.partition(" ")
            if not is_oid(oid) or not name:
                raise GitError(f"packed-refs is corrupt near {line[:50]!r}")
            out[name] = (oid, None)
            last = name
        return out

    def packed_peeled(self, name):
        entry = self._packed().get(name)
        return entry[1] if entry else None

    def list(self, prefix="refs/"):
        """``[(name, oid)]`` of every direct ref below ``prefix`` (symbolic refs are resolved), sorted by name."""
        found = {name: entry[0] for name, entry in self._packed().items() if name.startswith(prefix)}
        base = self._path(prefix.rstrip("/"))
        if os.path.isdir(base):
            for root, dirs, files in os.walk(base):
                dirs.sort()
                for fn in files:
                    if fn.endswith(".lock"):
                        continue
                    name = os.path.relpath(os.path.join(root, fn), self.gitdir).replace(os.sep, "/")
                    if name.startswith(prefix):
                        oid = self.read(name)
                        if oid:
                            found[name] = oid
        return sorted(found.items(), key=lambda kv: kv[0].encode())

    def exists(self, name):
        return self.read_raw(name) is not None

    def names(self, prefix):
        return [n for n, _ in self.list(prefix)]

    # --- writing ---------------------------------------------------------------------------------------------------------------------------
    def _check_conflicts(self, name):
        """A ref ``a/b`` cannot exist next to a ref ``a`` (a file and a directory with one name)."""
        parts = name.split("/")
        existing = {n for n, _ in self.list("refs/")} if name.startswith("refs/") else set()
        for i in range(1, len(parts)):
            parent = "/".join(parts[:i])
            if parent in existing:
                raise GitError(f"cannot create reference '{name}': '{parent}' exists, and a ref cannot be both a file and a directory")
        prefix = name + "/"
        clash = [n for n in existing if n.startswith(prefix)]
        if clash:
            raise GitError(f"cannot create reference '{name}': '{clash[0]}' exists, and a ref cannot be both a file and a directory")

    def _write_locked(self, name, text, old=_UNSET):
        path = self._path(name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.isdir(path):                           # a directory left behind by deleted refs ("a/b" gone, "a" wanted) is swept away
            for root, dirs, files in os.walk(path, topdown=False):
                if files:
                    raise GitError(f"cannot create reference '{name}': {root} is a directory holding files")
                os.rmdir(root)
        lock = path + ".lock"
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            raise GitError(f"unable to lock reference '{name}': {lock} exists (another process is updating it; if not, remove the file)")
        try:
            if old is not _UNSET:
                current = self.read(name)
                if current != old:
                    raise GitError(f"reference '{name}' changed under us (expected {old}, found {current}); nothing was written")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text + "\n")
            os.replace(lock, path)
        except BaseException:
            if os.path.exists(lock):
                os.unlink(lock)
            raise

    def update(self, name, oid, old=_UNSET):
        """Point the direct reference ``name`` (or, for a symbolic ``HEAD``, the branch it names) at ``oid``. ``old`` is an optional compare-and-swap guard."""
        if not is_oid(oid):
            raise GitError(f"{oid!r} is not an object id")
        raw = self.read_raw(name)
        if raw and raw[0] == "sym":                       # writing through HEAD updates the branch HEAD points at
            return self.update(raw[1], oid, old)
        if name != "HEAD" and not check_ref_format(name):
            raise GitError(f"'{name}' is not a valid reference name")
        if raw is None:
            self._check_conflicts(name)
        self._write_locked(name, oid, old)

    def set_symbolic(self, name, target):
        if not check_ref_format(target, onelevel=False):
            raise GitError(f"'{target}' is not a valid reference name")
        self._write_locked(name, SYMREF + target)

    def delete(self, name, old=_UNSET):
        """Remove a reference everywhere (loose file and packed line) and prune the directories that become empty."""
        if self.read_raw(name) is None:
            raise GitError(f"reference '{name}' does not exist")
        path = self._path(name)
        lock = path + ".packed-refs-lock"
        if old is not _UNSET and self.read(name) != old:
            raise GitError(f"reference '{name}' changed under us; nothing was deleted")
        packed = self._packed()
        if name in packed:
            self._rewrite_packed({n: e for n, e in packed.items() if n != name})
        if os.path.isfile(path):
            os.unlink(path)
        if os.path.exists(lock):
            os.unlink(lock)
        directory = os.path.dirname(path)
        keep = {os.path.join(self.gitdir, "refs"), os.path.join(self.gitdir, "refs", "heads"), os.path.join(self.gitdir, "refs", "tags")}
        while directory.startswith(os.path.join(self.gitdir, "refs") + os.sep) and directory not in keep and os.path.isdir(directory) and not os.listdir(directory):
            os.rmdir(directory)
            directory = os.path.dirname(directory)

    def _rewrite_packed(self, packed):
        path = os.path.join(self.gitdir, "packed-refs")
        lock = path + ".lock"
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            raise GitError(f"unable to lock packed-refs: {lock} exists")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape") as f:
                f.write("# pack-refs with: peeled fully-peeled sorted \n")
                for name in sorted(packed, key=lambda n: n.encode()):
                    f.write(f"{packed[name][0]} {name}\n")
                    if packed[name][1]:
                        f.write(f"^{packed[name][1]}\n")
            os.replace(lock, path)
        except BaseException:
            if os.path.exists(lock):
                os.unlink(lock)
            raise
