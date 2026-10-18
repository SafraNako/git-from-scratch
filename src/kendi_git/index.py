"""The index (staging area): ``.git/index``.

Layout (versions 2 and 3): the signature ``DIRC``, a version, an entry count; each entry is ten 32-bit stat fields (ctime s/ns, mtime s/ns, dev, ino, mode, uid, gid,
size), the 20-byte object id, a 16-bit flags field (assume-valid, extended, stage, name length) and, for version 3 extended entries, 16 more flag bits; then the path,
NUL-padded so that the entry length is a multiple of eight; after the entries optional extensions (cached trees and so on, which this program ignores and does not
write), and finally the SHA-1 of everything before it. Entries are sorted by path bytes, then stage.
"""
from __future__ import annotations

import hashlib
import os
import struct
from dataclasses import dataclass, field

from .errors import GitError
from .objects import MODE_EXEC, MODE_FILE, MODE_GITLINK, MODE_LINK, MODE_TREE, TreeEntry, parse_tree, serialize_tree

ASSUME_VALID, EXTENDED, STAGE_MASK, NAME_MASK = 0x8000, 0x4000, 0x3000, 0x0FFF
INTENT_TO_ADD, SKIP_WORKTREE = 0x2000, 0x4000            # extended flags
U32 = 0xFFFFFFFF


@dataclass
class IndexEntry:
    path: str
    mode: int
    oid: str
    stage: int = 0
    ctime: tuple = (0, 0)
    mtime: tuple = (0, 0)
    dev: int = 0
    ino: int = 0
    uid: int = 0
    gid: int = 0
    size: int = 0
    assume_valid: bool = False
    extended: int = 0

    def key(self):
        return (os.fsencode(self.path), self.stage)


def _offset_varint(data, pos):
    """Git's offset encoding (also used by OFS_DELTA): big-endian base-128 where every continuation adds one. Returns ``(value, next position)``."""
    c = data[pos]
    pos += 1
    value = c & 0x7F
    while c & 0x80:
        c = data[pos]
        pos += 1
        value = ((value + 1) << 7) + (c & 0x7F)
    return value, pos


def stat_fields(st):
    """The stat data an index entry keeps, as git truncates it to 32 bits."""
    return {"ctime": (st.st_ctime_ns // 10**9 & U32, st.st_ctime_ns % 10**9), "mtime": (st.st_mtime_ns // 10**9 & U32, st.st_mtime_ns % 10**9),
            "dev": st.st_dev & U32, "ino": st.st_ino & U32, "uid": st.st_uid & U32, "gid": st.st_gid & U32, "size": st.st_size & U32}


def mode_of(st):
    import stat
    if stat.S_ISLNK(st.st_mode):
        return MODE_LINK
    return MODE_EXEC if st.st_mode & 0o111 else MODE_FILE


def stat_matches(entry, st):
    """Does the file's stat data equal what the index recorded (so its content need not be read)? Mirrors git's own comparison."""
    f = stat_fields(st)
    return (entry.size == f["size"] and entry.mtime == f["mtime"] and entry.ctime == f["ctime"] and entry.ino == f["ino"] and entry.dev == f["dev"]
            and entry.uid == f["uid"] and entry.gid == f["gid"] and entry.mode == mode_of(st))


@dataclass
class Index:
    entries: dict = field(default_factory=dict)        # (path, stage) -> IndexEntry
    version: int = 2
    mtime: tuple = (0, 0)                              # when the index file itself was last written: an entry not older than this is "racy"

    # --- reading and writing -----------------------------------------------------------------------------------------------------------------
    @staticmethod
    def read(path):
        if not os.path.exists(path):
            return Index()
        with open(path, "rb") as f:
            data = f.read()
        st = os.stat(path)
        if len(data) < 32 or data[:4] != b"DIRC":
            raise GitError("the index file is corrupt (bad signature)")
        if hashlib.sha1(data[:-20]).digest() != data[-20:]:
            raise GitError("the index file is corrupt (checksum mismatch)")
        version, count = struct.unpack(">II", data[4:12])
        if version not in (2, 3, 4):
            raise GitError(f"index version {version} is not supported (this program reads versions 2, 3 and 4)")
        idx = Index(version=version, mtime=(st.st_mtime_ns // 10**9, st.st_mtime_ns % 10**9))
        pos = 12
        previous = b""
        for _ in range(count):
            fields = struct.unpack(">10I", data[pos:pos + 40])
            oid = data[pos + 40:pos + 60].hex()
            (flags,) = struct.unpack(">H", data[pos + 60:pos + 62])
            body = pos + 62
            extended = 0
            if flags & EXTENDED:
                if version < 3:
                    raise GitError("a version 2 index contains an extended entry")
                (extended,) = struct.unpack(">H", data[body:body + 2])
                body += 2
            if version == 4:                                  # path prefix compression: "drop N bytes from the previous path, then append this suffix"
                strip, body = _offset_varint(data, body)
                end = data.index(b"\x00", body)
                name = previous[:len(previous) - strip] + data[body:end]
                previous = name
                total = end + 1 - pos                           # entries are not padded in version 4
            else:
                length = flags & NAME_MASK
                if length == NAME_MASK:
                    length = data.index(b"\x00", body) - body
                name = data[body:body + length]
                total = (body - pos + length + 8) & ~7
            e = IndexEntry(os.fsdecode(name), fields[6], oid, (flags & STAGE_MASK) >> 12, (fields[0], fields[1]), (fields[2], fields[3]), fields[4], fields[5], fields[7],
                           fields[8], fields[9], bool(flags & ASSUME_VALID), extended)
            idx.entries[(e.path, e.stage)] = e
            pos += total
        while pos < len(data) - 20:                  # extensions: the uppercase ones are optional and skipped, the others would change the meaning of the index
            sig = data[pos:pos + 4]
            (size,) = struct.unpack(">I", data[pos + 4:pos + 8])
            if not sig[:1].isupper():
                raise GitError(f"the index uses the extension {sig!r}, which this program does not support")
            pos += 8 + size
        return idx

    def serialize(self):
        entries = sorted(self.entries.values(), key=IndexEntry.key)
        version = 3 if any(e.extended for e in entries) else 2
        out = [b"DIRC", struct.pack(">II", version, len(entries))]
        for e in entries:
            name = os.fsencode(e.path)
            flags = (ASSUME_VALID if e.assume_valid else 0) | (e.stage << 12) | min(len(name), NAME_MASK) | (EXTENDED if e.extended else 0)
            head = struct.pack(">10I", e.ctime[0], e.ctime[1], e.mtime[0], e.mtime[1], e.dev, e.ino, e.mode, e.uid, e.gid, e.size) + bytes.fromhex(e.oid) + struct.pack(">H", flags)
            if e.extended:
                head += struct.pack(">H", e.extended)
            body = head + name
            out.append(body + b"\x00" * (((len(body) + 8) & ~7) - len(body)))
        data = b"".join(out)
        return data + hashlib.sha1(data).digest()

    def write(self, path):
        """Write the index atomically through ``index.lock``, as git does, so a concurrent git (or this program) never sees a half-written file."""
        lock = path + ".lock"
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            raise GitError(f"unable to create '{lock}': another process is using the index (if not, remove that file)")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(self.serialize())
            os.replace(lock, path)
        except BaseException:
            if os.path.exists(lock):
                os.unlink(lock)
            raise
        st = os.stat(path)
        self.mtime = (st.st_mtime_ns // 10**9, st.st_mtime_ns % 10**9)

    # --- editing ---------------------------------------------------------------------------------------------------------------------------
    def stage_zero(self):
        """Entries at stage 0 sorted by path bytes; raises ``GitError`` if the index holds unresolved conflicts."""
        if any(stage for _, stage in self.entries):
            raise GitError("the index contains unresolved conflicts")
        return sorted((e for e in self.entries.values()), key=IndexEntry.key)

    def zero_entries(self):
        """Entries at stage 0 sorted by path bytes, ignoring the ones that are in conflict."""
        return sorted((e for e in self.entries.values() if not e.stage), key=IndexEntry.key)

    def unmerged(self):
        """``{path: {stage: entry}}`` for the paths with conflicts."""
        out = {}
        for (path, stage), e in self.entries.items():
            if stage:
                out.setdefault(path, {})[stage] = e
        return out

    def get(self, path):
        return self.entries.get((path, 0))

    def set(self, entry):
        """Add or replace an entry, removing what it would collide with: the same path at other stages, a file where a directory is needed, and files under a new file."""
        for stage in (1, 2, 3):
            self.entries.pop((entry.path, stage), None)
        parts = entry.path.split("/")
        for i in range(1, len(parts)):
            self.entries.pop(("/".join(parts[:i]), 0), None)                     # "a" is a file but "a/b" is being added
        prefix = entry.path + "/"
        for key in [k for k in self.entries if k[0].startswith(prefix)]:
            del self.entries[key]                                                # "a/b" exist but "a" is becoming a file
        self.entries[(entry.path, 0)] = entry

    def remove(self, path):
        removed = False
        for stage in (0, 1, 2, 3):
            removed |= self.entries.pop((path, stage), None) is not None
        return removed

    def paths(self):
        return sorted({p for p, _ in self.entries}, key=os.fsencode)

    def is_racy(self, entry):
        return (entry.mtime[0], entry.mtime[1]) >= self.mtime


# --- trees <-> index ------------------------------------------------------------------------------------------------------------------------------
def flatten_tree(odb, tree_oid, prefix=""):
    """``{path: (mode, oid)}`` for every non-directory entry below a tree (gitlinks are entries, not descended into)."""
    out = {}
    kind, data = odb.read(tree_oid)
    if kind != "tree":
        raise GitError(f"{tree_oid} is a {kind}, not a tree")
    for e in parse_tree(data):
        path = prefix + e.name
        if e.mode == MODE_TREE:
            out.update(flatten_tree(odb, e.oid, path + "/"))
        else:
            out[path] = (e.mode, e.oid)
    return out


def write_tree(odb, files):
    """Store the tree objects for ``{path: (mode, oid)}`` and return the root tree id (an empty mapping gives the empty tree)."""
    root = {}
    for path, (mode, oid) in files.items():
        node = root
        parts = path.split("/")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = (mode, oid)

    def build(node):
        entries = []
        for name, value in node.items():
            if isinstance(value, dict):
                entries.append(TreeEntry(MODE_TREE, name, build(value)))
            else:
                entries.append(TreeEntry(value[0], name, value[1]))
        return odb.write("tree", serialize_tree(entries))
    return build(root)


def index_to_tree(odb, index):
    return write_tree(odb, {e.path: (e.mode, e.oid) for e in index.stage_zero()})


__all__ = ["Index", "IndexEntry", "flatten_tree", "write_tree", "index_to_tree", "stat_fields", "stat_matches", "mode_of", "MODE_GITLINK"]
