"""Writing and receiving packfiles for the network commands.

``write_pack`` produces a pack of whole objects (no deltas); ``receive_pack`` takes the bytes a server sent, checks them, resolves every delta (also against objects the
repository already has, for thin packs) and stores the pack with a version 2 index, the way ``git index-pack`` does.
"""
from __future__ import annotations

import hashlib
import os
import struct
import tempfile
import zlib

from .errors import GitError
from .pack import OFS_DELTA, REF_DELTA, TYPE_NAMES, apply_delta

TYPE_CODES = {name: code for code, name in TYPE_NAMES.items()}


def _object_header(code, size):
    first = (code << 4) | (size & 0x0F)
    size >>= 4
    out = bytearray()
    while size:
        out.append(first | 0x80)
        first = size & 0x7F
        size >>= 7
    out.append(first)
    return bytes(out)


def write_pack(objects):
    """A pack holding every ``(kind, data)`` of ``objects`` (a list) as a whole object."""
    body = [b"PACK", struct.pack(">II", 2, len(objects))]
    for kind, data in objects:
        body.append(_object_header(TYPE_CODES[kind], len(data)))
        body.append(zlib.compress(data, 1))
    blob = b"".join(body)
    return blob + hashlib.sha1(blob).digest()


class _Entry:
    __slots__ = ("offset", "end", "code", "size", "base_offset", "base_oid", "crc", "oid", "kind")

    def __init__(self):
        self.base_offset = None
        self.base_oid = None
        self.oid = None
        self.kind = None


def _scan(buf):
    """Walk the objects of a pack held in ``buf`` and note where each starts and ends; nothing is resolved yet."""
    if buf[:4] != b"PACK":
        raise GitError("the server did not send a packfile")
    version, count = struct.unpack(">II", buf[4:12])
    if version not in (2, 3):
        raise GitError("unsupported pack version %d" % version)
    pos = 12
    entries = []
    for _ in range(count):
        e = _Entry()
        e.offset = pos
        c = buf[pos]
        pos += 1
        e.code = (c >> 4) & 7
        size = c & 0x0F
        shift = 4
        while c & 0x80:
            c = buf[pos]
            pos += 1
            size |= (c & 0x7F) << shift
            shift += 7
        e.size = size
        if e.code == OFS_DELTA:
            c = buf[pos]
            pos += 1
            off = c & 0x7F
            while c & 0x80:
                c = buf[pos]
                pos += 1
                off = ((off + 1) << 7) | (c & 0x7F)
            e.base_offset = e.offset - off
        elif e.code == REF_DELTA:
            e.base_oid = buf[pos:pos + 20].hex()
            pos += 20
        elif e.code not in TYPE_NAMES:
            raise GitError("unknown object type %d in the pack" % e.code)
        d = zlib.decompressobj()
        consumed = 0
        chunk = 4096
        out_len = 0
        start = pos
        while not d.eof:
            piece = buf[start + consumed:start + consumed + chunk]
            if not piece:
                raise GitError("the pack ends in the middle of an object")
            out_len += len(d.decompress(piece))
            consumed += len(piece)
            if d.eof:
                consumed -= len(d.unused_data)
            chunk = min(chunk * 2, 1 << 20)
        if out_len != e.size:
            raise GitError("an object in the pack does not have the size it declares")
        pos = start + consumed
        e.end = pos
        e.crc = zlib.crc32(buf[e.offset:e.end]) & 0xFFFFFFFF
        entries.append(e)
    return entries, pos


def _inflate_at(buf, entry):
    # the compressed data starts after the header (type/size, base reference); find it again by decoding the header
    pos = entry.offset
    c = buf[pos]
    pos += 1
    while c & 0x80:
        c = buf[pos]
        pos += 1
    if entry.code == OFS_DELTA:
        c = buf[pos]
        pos += 1
        while c & 0x80:
            c = buf[pos]
            pos += 1
    elif entry.code == REF_DELTA:
        pos += 20
    return zlib.decompress(buf[pos:entry.end])


def receive_pack(chunks, odb, objects_dir, expected=None):
    """Store the pack that arrives as the iterable ``chunks`` in ``objects_dir/pack`` with an index. Returns the list of object ids it contains."""
    os.makedirs(os.path.join(objects_dir, "pack"), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.join(objects_dir, "pack"), prefix="tmp_pack_")
    try:
        with os.fdopen(fd, "wb") as f:
            for chunk in chunks:
                f.write(chunk)
        with open(tmp, "rb") as f:
            buf = f.read()
        if len(buf) < 32:
            raise GitError("the server sent no pack data")
        if hashlib.sha1(buf[:-20]).digest() != buf[-20:]:
            raise GitError("the pack's checksum does not match")
        entries, end = _scan(buf)
        if end != len(buf) - 20:
            raise GitError("extra data after the last object of the pack")
        by_offset = {e.offset: e for e in entries}
        oid_index = {}
        resolved = {}                                          # offset -> (kind, data) while it is still needed

        def resolve(e):
            got = resolved.get(e.offset)
            if got is not None:
                return got
            data = _inflate_at(buf, e)
            if e.code in TYPE_NAMES:
                got = (TYPE_NAMES[e.code], data)
            else:
                if e.code == OFS_DELTA:
                    base = by_offset.get(e.base_offset)
                    if base is None:
                        raise GitError("a delta in the pack points at a base that is not there")
                    bkind, bdata = resolve(base)
                else:
                    base = oid_index.get(e.base_oid)
                    if base is not None:
                        bkind, bdata = resolve(base)
                    else:
                        try:
                            bkind, bdata = odb.read(e.base_oid)
                        except GitError:
                            raise GitError("a delta in the pack needs the object %s, which is missing" % e.base_oid)
                got = (bkind, apply_delta(bdata, data))
            if len(resolved) > 20000:
                resolved.clear()
            resolved[e.offset] = got
            return got

        # plain objects first so that REF_DELTA bases inside the pack can be found by id; then the deltas, in as many passes as it takes
        for e in entries:
            if e.code in TYPE_NAMES:
                kind, data = resolve(e)
                e.kind = kind
                e.oid = hashlib.sha1(b"%s %d\x00" % (kind.encode(), len(data)) + data).hexdigest()
                oid_index[e.oid] = e
        pending = [e for e in entries if e.code not in TYPE_NAMES]
        while pending:
            rest = []
            for e in pending:
                if e.code == REF_DELTA and e.base_oid not in oid_index and not odb.exists(e.base_oid):
                    rest.append(e)
                    continue
                if e.code == OFS_DELTA and by_offset.get(e.base_offset) is not None and by_offset[e.base_offset].oid is None and by_offset[e.base_offset].code not in TYPE_NAMES:
                    rest.append(e)                             # its base is a delta that has not been resolved yet
                    continue
                kind, data = resolve(e)
                e.kind = kind
                e.oid = hashlib.sha1(b"%s %d\x00" % (kind.encode(), len(data)) + data).hexdigest()
                oid_index[e.oid] = e
            if len(rest) == len(pending):
                raise GitError("the pack has deltas whose base objects are missing")
            pending = rest
        order = sorted(entries, key=lambda e: e.oid)
        fanout = [0] * 256
        for e in order:
            fanout[int(e.oid[:2], 16)] += 1
        total = 0
        for i in range(256):
            total += fanout[i]
            fanout[i] = total
        idx = [b"\xfftOc", struct.pack(">I", 2), b"".join(struct.pack(">I", n) for n in fanout)]
        idx.append(b"".join(bytes.fromhex(e.oid) for e in order))
        idx.append(b"".join(struct.pack(">I", e.crc) for e in order))
        big = []
        offs = []
        for e in order:
            if e.offset >= 1 << 31:
                offs.append(struct.pack(">I", (1 << 31) | len(big)))
                big.append(struct.pack(">Q", e.offset))
            else:
                offs.append(struct.pack(">I", e.offset))
        idx.append(b"".join(offs))
        idx.append(b"".join(big))
        idx.append(buf[-20:])
        idx_data = b"".join(idx)
        idx_data += hashlib.sha1(idx_data).digest()
        name = "pack-" + buf[-20:].hex()
        pack_dir = os.path.join(objects_dir, "pack")
        os.replace(tmp, os.path.join(pack_dir, name + ".pack"))
        tmp = None
        with open(os.path.join(pack_dir, name + ".idx.tmp"), "wb") as f:
            f.write(idx_data)
        os.replace(os.path.join(pack_dir, name + ".idx.tmp"), os.path.join(pack_dir, name + ".idx"))
        os.chmod(os.path.join(pack_dir, name + ".pack"), 0o444)
        os.chmod(os.path.join(pack_dir, name + ".idx"), 0o444)
        odb.refresh()
        return [e.oid for e in entries]
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


__all__ = ["write_pack", "receive_pack"]
