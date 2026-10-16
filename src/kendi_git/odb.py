"""The object database of a repository: loose objects (one zlib-compressed file each), packfiles, and alternate object directories."""
from __future__ import annotations

import os
import tempfile
import zlib

from .errors import GitError
from .objects import EMPTY_TREE, KINDS, encode_object, hash_object, is_oid
from .pack import Pack


class ObjectDB:
    def __init__(self, objects_dir):
        self.dir = objects_dir
        self._packs = None
        self._alternates = None

    # --- discovery -------------------------------------------------------------------------------------------------------------------------
    def packs(self):
        if self._packs is None:
            self._packs = []
            pack_dir = os.path.join(self.dir, "pack")
            if os.path.isdir(pack_dir):
                for name in sorted(os.listdir(pack_dir)):
                    if name.endswith(".idx") and os.path.exists(os.path.join(pack_dir, name[:-4] + ".pack")):
                        self._packs.append(Pack(os.path.join(pack_dir, name), os.path.join(pack_dir, name[:-4] + ".pack")))
        return self._packs

    def alternates(self):
        if self._alternates is None:
            self._alternates = []
            path = os.path.join(self.dir, "info", "alternates")
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    for line in f.read().splitlines():
                        line = line.strip()
                        if line and not line.startswith("#"):
                            self._alternates.append(ObjectDB(line if os.path.isabs(line) else os.path.normpath(os.path.join(self.dir, line))))
        return self._alternates

    def refresh(self):
        """Forget cached pack lists (after something else, such as ``git gc``, changed the directory)."""
        for p in self._packs or []:
            p.close()
        self._packs = self._alternates = None

    def _loose_path(self, oid):
        return os.path.join(self.dir, oid[:2], oid[2:])

    # --- reading ---------------------------------------------------------------------------------------------------------------------------
    def read(self, oid):
        """``(type, data)`` of the object ``oid``; raises ``GitError`` if it is missing or damaged."""
        found = self._read(oid)
        if found is None:
            raise GitError(f"object {oid} not found")
        return found

    def _read(self, oid):
        path = self._loose_path(oid)
        if os.path.exists(path):
            with open(path, "rb") as f:
                try:
                    raw = zlib.decompress(f.read())
                except zlib.error as exc:
                    raise GitError(f"loose object {oid} is corrupt: {exc}")
            nul = raw.find(b"\x00")
            kind, _, size = raw[:nul].partition(b" ")
            if nul < 0 or kind.decode("ascii", "replace") not in KINDS or not size.isdigit() or int(size) != len(raw) - nul - 1:
                raise GitError(f"loose object {oid} has a malformed header")
            return kind.decode(), raw[nul + 1:]
        for pack in self.packs():
            found = pack.read(oid, self._read_external)
            if found is not None:
                return found
        for alt in self.alternates():
            found = alt._read(oid)
            if found is not None:
                return found
        return ("tree", b"") if oid == EMPTY_TREE else None            # git knows the empty tree without it being stored

    def _read_external(self, oid):
        return self._read(oid)

    def exists(self, oid):
        if oid == EMPTY_TREE:
            return True
        if os.path.exists(self._loose_path(oid)) or any(p.has(oid) for p in self.packs()):
            return True
        return any(a.exists(oid) for a in self.alternates())

    def kind_of(self, oid):
        return self.read(oid)[0]

    def iter_oids(self):
        """Every object id in the database, loose first, then packed, then alternates (an id can repeat when stored twice)."""
        if os.path.isdir(self.dir):
            for d in sorted(os.listdir(self.dir)):
                if len(d) == 2 and all(c in "0123456789abcdef" for c in d):
                    for name in sorted(os.listdir(os.path.join(self.dir, d))):
                        if len(name) == 38:
                            yield d + name
        for pack in self.packs():
            yield from pack
        for alt in self.alternates():
            yield from alt.iter_oids()

    def candidates(self, prefix):
        """Every object id that starts with ``prefix`` (any length of lower-case hex digits), as a set."""
        found = set()
        d = os.path.join(self.dir, prefix[:2])
        if len(prefix) >= 2 and os.path.isdir(d):
            found.update(prefix[:2] + n for n in os.listdir(d) if (prefix[:2] + n).startswith(prefix) and len(n) == 38)
        elif len(prefix) < 2 and os.path.isdir(self.dir):
            for sub in os.listdir(self.dir):
                if len(sub) == 2 and sub.startswith(prefix):
                    found.update(sub + n for n in os.listdir(os.path.join(self.dir, sub)) if len(n) == 38)
        for pack in self.packs():
            found.update(pack.prefix_matches(prefix))
        for alt in self.alternates():
            found.update(alt.candidates(prefix))
        return found

    def resolve_prefix(self, prefix):
        """The single object id starting with ``prefix`` (4 or more hex digits); ``GitError`` when none or several match."""
        prefix = prefix.lower()
        if is_oid(prefix):
            return prefix
        if len(prefix) < 4 or any(c not in "0123456789abcdef" for c in prefix):
            raise GitError(f"'{prefix}' is not a valid object name")
        found = self.candidates(prefix)
        if not found:
            raise GitError(f"no object starts with '{prefix}'")
        if len(found) > 1:
            raise GitError(f"'{prefix}' is ambiguous: {len(found)} objects start with it")
        return found.pop()

    def approximate_count(self):
        """Packed objects only, which is also all that git counts when it picks the default abbreviation length."""
        return sum(p.count for p in self.packs()) + sum(a.approximate_count() for a in self.alternates())

    def abbrev(self, oid, minimum=None):
        """The shortest prefix of ``oid`` that no other object shares, at least as long as git's default (7, growing with the size of big repositories)."""
        if minimum is None:
            bits = self.approximate_count().bit_length()
            minimum = max(7, (bits + 1) // 2)
        n = min(max(minimum, 4), 40)
        while n < 40 and self.candidates(oid[:n]) - {oid}:
            n += 1
        return oid[:n]

    # --- writing ---------------------------------------------------------------------------------------------------------------------------
    def write(self, kind, data):
        """Store an object as a loose file (unless it is already there) and return its id."""
        oid = hash_object(kind, data)
        if self.exists(oid):
            return oid
        path = self._loose_path(oid)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix="tmp_obj_")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(zlib.compress(encode_object(kind, data), 1))
            os.chmod(tmp, 0o444)
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return oid
