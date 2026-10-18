"""Reading packfiles: the ``.idx`` (version 2) lookup tables and the ``.pack`` objects, including OFS_DELTA and REF_DELTA chains.

A pack is ``PACK``, a version, an object count, then objects, each ``type+size`` in a variable-length header followed by zlib data; a delta object stores instructions
(copy a range from the base, insert literal bytes) to rebuild its content from another object. The index gives every object's offset by a 256-entry fan-out table over
the first id byte plus a sorted id table, so a lookup is a binary search.
"""
from __future__ import annotations

import bisect
import mmap
import struct
import zlib

from .errors import GitError

TYPE_NAMES = {1: "commit", 2: "tree", 3: "blob", 4: "tag"}
OFS_DELTA, REF_DELTA = 6, 7


def _varint(data, pos):
    """A little-endian base-128 size (the delta header form): ``(value, new position)``."""
    value, shift = 0, 0
    while True:
        b = data[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return value, pos


def apply_delta(base, delta):
    """Rebuild an object from its base and a delta instruction stream."""
    src_size, pos = _varint(delta, 0)
    dst_size, pos = _varint(delta, pos)
    if src_size != len(base):
        raise GitError("delta does not match its base object (size mismatch)")
    out = bytearray()
    while pos < len(delta):
        op = delta[pos]
        pos += 1
        if op & 0x80:                                   # copy from the base
            offset = size = 0
            needed = bin(op & 0x7F).count("1")
            if pos + needed > len(delta):
                raise GitError("delta ends in the middle of a copy instruction")
            for i in range(4):
                if op & (1 << i):
                    offset |= delta[pos] << (8 * i)
                    pos += 1
            for i in range(3):
                if op & (0x10 << i):
                    size |= delta[pos] << (8 * i)
                    pos += 1
            size = size or 0x10000
            if offset + size > len(base):
                raise GitError("delta copies past the end of its base object")
            out += base[offset:offset + size]
        elif op:                                        # insert literal bytes
            if pos + op > len(delta):
                raise GitError("delta ends in the middle of an insert instruction")
            out += delta[pos:pos + op]
            pos += op
        else:
            raise GitError("delta contains the reserved instruction 0")
    if len(out) != dst_size:
        raise GitError("delta produced an object of the wrong size")
    return bytes(out)


def _inflate(buf, pos, expected):
    """Inflate the zlib stream starting at ``pos`` in ``buf`` (an mmap or bytes); returns the data. ``expected`` is the declared size, checked."""
    d = zlib.decompressobj()
    out, chunk = [], 8192
    while not d.eof:
        piece = buf[pos:pos + chunk]
        if not piece:
            raise GitError("truncated pack: zlib stream ends early")
        try:
            out.append(d.decompress(piece))
        except zlib.error as exc:
            raise GitError(f"corrupt zlib data in a packfile: {exc}")
        pos += chunk
        chunk = min(chunk * 2, 1 << 20)
    data = b"".join(out)
    if len(data) != expected:
        raise GitError("pack object size does not match its header")
    return data


class Pack:
    def __init__(self, idx_path, pack_path):
        self.idx_path, self.pack_path = idx_path, pack_path
        with open(idx_path, "rb") as f:
            self.idx = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        with open(pack_path, "rb") as f:
            self.pack = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        if self.idx[:4] != b"\xfftOc" or struct.unpack(">I", self.idx[4:8])[0] != 2:
            raise GitError(f"{idx_path}: only version 2 pack indexes are supported")
        if self.pack[:4] != b"PACK" or struct.unpack(">I", self.pack[4:8])[0] not in (2, 3):
            raise GitError(f"{pack_path}: not a version 2 or 3 packfile")
        self.fanout = struct.unpack(">256I", self.idx[8:8 + 1024])
        self.count = self.fanout[255]
        self._sha_at = 8 + 1024
        self._crc_at = self._sha_at + 20 * self.count
        self._off_at = self._crc_at + 4 * self.count
        self._big_at = self._off_at + 4 * self.count
        self._cache = {}

    def close(self):
        self.idx.close()
        self.pack.close()

    def oid_at(self, i):
        return self.idx[self._sha_at + 20 * i:self._sha_at + 20 * i + 20].hex()

    def __iter__(self):
        return (self.oid_at(i) for i in range(self.count))

    def _index_of(self, raw):
        first = raw[0]
        lo, hi = (self.fanout[first - 1] if first else 0), self.fanout[first]
        while lo < hi:
            mid = (lo + hi) // 2
            cur = self.idx[self._sha_at + 20 * mid:self._sha_at + 20 * mid + 20]
            if cur < raw:
                lo = mid + 1
            elif cur > raw:
                hi = mid
            else:
                return mid
        return None

    def offset_of(self, oid):
        i = self._index_of(bytes.fromhex(oid))
        if i is None:
            return None
        off = struct.unpack(">I", self.idx[self._off_at + 4 * i:self._off_at + 4 * i + 4])[0]
        if off & 0x80000000:
            j = off & 0x7FFFFFFF
            off = struct.unpack(">Q", self.idx[self._big_at + 8 * j:self._big_at + 8 * j + 8])[0]
        return off

    def has(self, oid):
        return self._index_of(bytes.fromhex(oid)) is not None

    def prefix_matches(self, prefix):
        """Ids in this pack starting with the hex ``prefix`` (an even or odd number of digits)."""
        padded_lo = bytes.fromhex(prefix + "0" * (len(prefix) % 2))
        lo_i = bisect.bisect_left(_Seq(self), padded_lo[:(len(prefix) + 1) // 2])
        out = []
        for i in range(lo_i, self.count):
            oid = self.oid_at(i)
            if not oid.startswith(prefix):
                break
            out.append(oid)
        return out

    def read(self, oid, resolve_ref):
        """``(type, data)`` of the object, or ``None``; ``resolve_ref(oid)`` supplies the base of a REF_DELTA that lives outside this pack."""
        off = self.offset_of(oid)
        return None if off is None else self._read_at(off, resolve_ref)

    def _read_at(self, offset, resolve_ref):
        if offset in self._cache:
            return self._cache[offset]
        buf = self.pack
        pos = offset
        c = buf[pos]
        pos += 1
        kind, size, shift = (c >> 4) & 7, c & 15, 4
        while c & 0x80:
            c = buf[pos]
            pos += 1
            size |= (c & 0x7F) << shift
            shift += 7
        if kind in TYPE_NAMES:
            result = (TYPE_NAMES[kind], _inflate(buf, pos, size))
        elif kind == OFS_DELTA:
            c = buf[pos]
            pos += 1
            back = c & 0x7F
            while c & 0x80:
                c = buf[pos]
                pos += 1
                back = ((back + 1) << 7) | (c & 0x7F)
            base_kind, base = self._read_at(offset - back, resolve_ref)
            result = (base_kind, apply_delta(base, _inflate(buf, pos, size)))
        elif kind == REF_DELTA:
            base_oid = buf[pos:pos + 20].hex()
            pos += 20
            found = self.read(base_oid, resolve_ref) or resolve_ref(base_oid)
            if found is None:
                raise GitError(f"pack delta base {base_oid} is missing")
            result = (found[0], apply_delta(found[1], _inflate(buf, pos, size)))
        else:
            raise GitError(f"unknown pack object type {kind}")
        if len(self._cache) > 512:
            self._cache.clear()
        self._cache[offset] = result
        return result

    def raw_delta(self, oid):
        """If ``oid`` is stored here as a delta: ``(base object id, delta size, compressed delta bytes)`` exactly as they sit in the pack, otherwise ``None``."""
        off = self.offset_of(oid)
        if off is None:
            return None
        buf = self.pack
        pos = off
        c = buf[pos]
        pos += 1
        kind, size, shift = (c >> 4) & 7, c & 15, 4
        while c & 0x80:
            c = buf[pos]
            pos += 1
            size |= (c & 0x7F) << shift
            shift += 7
        if kind == OFS_DELTA:
            c = buf[pos]
            pos += 1
            back = c & 0x7F
            while c & 0x80:
                c = buf[pos]
                pos += 1
                back = ((back + 1) << 7) | (c & 0x7F)
            base_oid = self._oid_at_offset(off - back)
        elif kind == REF_DELTA:
            base_oid = buf[pos:pos + 20].hex()
            pos += 20
        else:
            return None
        return base_oid, size, bytes(buf[pos:self._end_of(off)])

    def _offsets(self):
        if getattr(self, "_off_index", None) is None:
            pairs = []
            for i in range(self.count):
                o = struct.unpack(">I", self.idx[self._off_at + 4 * i:self._off_at + 4 * i + 4])[0]
                if o & 0x80000000:
                    j = o & 0x7FFFFFFF
                    o = struct.unpack(">Q", self.idx[self._big_at + 8 * j:self._big_at + 8 * j + 8])[0]
                pairs.append((o, i))
            pairs.sort()
            self._off_index = ([p[0] for p in pairs], [p[1] for p in pairs])
        return self._off_index

    def _oid_at_offset(self, offset):
        offsets, indexes = self._offsets()
        k = bisect.bisect_left(offsets, offset)
        if k == len(offsets) or offsets[k] != offset:
            raise GitError("pack delta base offset does not point at an object")
        return self.oid_at(indexes[k])

    def _end_of(self, offset):
        """Where the object starting at ``offset`` ends: the start of the next one (or the trailer)."""
        offsets, _ = self._offsets()
        k = bisect.bisect_right(offsets, offset)
        return offsets[k] if k < len(offsets) else len(self.pack) - 20

    def verify_checksums(self):
        """Whether the trailing SHA-1 of the pack and of the index match their content (``(pack_ok, idx_ok)``)."""
        import hashlib
        pack_ok = hashlib.sha1(self.pack[:-20]).digest() == self.pack[-20:] and self.pack[-20:] == self.idx[-40:-20]
        idx_ok = hashlib.sha1(self.idx[:-20]).digest() == self.idx[-20:]
        return pack_ok, idx_ok


class _Seq:
    """The sorted id table of a pack seen as a sequence, for ``bisect``."""

    def __init__(self, pack):
        self.pack = pack

    def __len__(self):
        return self.pack.count

    def __getitem__(self, i):
        return self.pack.idx[self.pack._sha_at + 20 * i:self.pack._sha_at + 20 * i + 20]
