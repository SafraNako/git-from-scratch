"""Git's wire format: pkt-lines, the side-band wrapping of a pack, and the two commands of protocol version 2 that a client needs (``ls-refs`` and ``fetch``) plus the
version 0 conversation of ``receive-pack`` used by push.

A pkt-line is four hex digits giving the length of the whole line (the four digits included) followed by the payload; ``0000`` is a flush packet, ``0001`` a delimiter and
``0002`` the end of a response.
"""
from __future__ import annotations

from .errors import GitError

FLUSH, DELIM, END = "flush", "delim", "end"
AGENT = "agent=kgit/0.1"
ZERO = "0" * 40


def pkt_line(data):
    if isinstance(data, str):
        data = data.encode("utf-8", "surrogateescape")
    if len(data) > 65516:
        raise GitError("a pkt-line is too long")
    return b"%04x" % (len(data) + 4) + data


def pkt_text(text):
    """A text line: newline-terminated, as git sends commands and arguments."""
    return pkt_line(text + "\n")


class PktReader:
    """Reads pkt-lines from a function ``read(n)`` that returns up to ``n`` bytes (fewer or none at the end of the stream)."""

    def __init__(self, read):
        self._read = read

    def _exact(self, n):
        out = b""
        while len(out) < n:
            piece = self._read(n - len(out))
            if not piece:
                raise GitError("the connection closed unexpectedly (%d of %d bytes read)" % (len(out), n))
            out += piece
        return out

    def packet(self):
        """The next packet: bytes (without the length), or ``FLUSH`` / ``DELIM`` / ``END``; ``None`` at the end of the stream."""
        try:
            head = self._read(4)
            if not head:
                return None
            if len(head) < 4:
                head += self._exact(4 - len(head))
        except GitError:
            return None
        try:
            n = int(head, 16)
        except ValueError:
            raise GitError("bad pkt-line header %r (the server does not speak the git protocol here)" % head)
        if n == 0:
            return FLUSH
        if n == 1:
            return DELIM
        if n == 2:
            return END
        if n < 4:
            raise GitError("bad pkt-line length %d" % n)
        return self._exact(n - 4)

    def line(self):
        """Like ``packet`` but text packets lose their trailing newline."""
        p = self.packet()
        if isinstance(p, bytes):
            return p[:-1] if p.endswith(b"\n") else p
        return p

    def until_flush(self):
        out = []
        while True:
            p = self.line()
            if p is None or p == FLUSH:
                return out
            if p in (DELIM, END):
                continue
            out.append(p)


def demux(reader, progress=None):
    """The pack data of a side-band stream: yields the bytes of band 1, hands band 2 (progress) to ``progress`` and turns band 3 into an error. Stops at a flush."""
    while True:
        p = reader.packet()
        if p is None or p == FLUSH or p == END:
            return
        if p == DELIM:
            continue
        band, payload = p[0], p[1:]
        if band == 1:
            yield payload
        elif band == 2:
            if progress:
                progress(payload.decode("utf-8", "replace"))
        elif band == 3:
            raise GitError("remote: " + payload.decode("utf-8", "replace").strip())
        else:
            raise GitError("unknown side-band channel %d" % band)


# --- protocol version 2 -----------------------------------------------------------------------------------------------------------------------------
def parse_v2_capabilities(reader):
    """The server's greeting: ``version 2`` then capability lines up to a flush. Returns ``{name: value or None}``."""
    first = reader.line()
    if first is None or first == FLUSH:
        raise GitError("the server sent no greeting")
    if first != b"version 2":
        raise GitError("the server does not speak protocol version 2 (it said %r)" % first[:40])
    caps = {}
    for line in reader.until_flush():
        text = line.decode("utf-8", "replace")
        name, _, value = text.partition("=")
        caps[name] = value or None
    return caps


def ls_refs_request(prefixes=(), symrefs=True, peel=True, unborn=False):
    out = [pkt_text("command=ls-refs"), pkt_text(AGENT), pkt_text("object-format=sha1"), b"0001"]
    if peel:
        out.append(pkt_text("peel"))
    if symrefs:
        out.append(pkt_text("symrefs"))
    if unborn:
        out.append(pkt_text("unborn"))
    for p in prefixes:
        out.append(pkt_text("ref-prefix " + p))
    out.append(b"0000")
    return b"".join(out)


def parse_ls_refs(reader):
    """``[(name, oid, symref target or None, peeled oid or None)]`` from the response of ``ls-refs``."""
    refs = []
    for line in reader.until_flush():
        parts = line.decode("utf-8", "surrogateescape").split(" ")
        oid, name, target, peeled = parts[0], parts[1], None, None
        for attr in parts[2:]:
            if attr.startswith("symref-target:"):
                target = attr[len("symref-target:"):]
            elif attr.startswith("peeled:"):
                peeled = attr[len("peeled:"):]
        refs.append((name, oid, target, peeled))
    return refs


def fetch_request(wants, haves=(), depth=None, progress=False):
    out = [pkt_text("command=fetch"), pkt_text(AGENT), pkt_text("object-format=sha1"), b"0001", pkt_text("ofs-delta")]
    if not progress:
        out.append(pkt_text("no-progress"))
    if depth:
        out.append(pkt_text("deepen %d" % depth))
    for w in wants:
        out.append(pkt_text("want " + w))
    for h in haves:
        out.append(pkt_text("have " + h))
    out += [pkt_text("done"), b"0000"]
    return b"".join(out)


def read_fetch_response(reader, progress=None):
    """The pack bytes (an iterator) of a ``fetch`` response sent after ``done``: skips the optional sections until ``packfile``."""
    while True:
        p = reader.line()
        if p is None:
            raise GitError("the server ended the conversation without sending a pack")
        if p == FLUSH:
            raise GitError("the server sent no pack")
        if p in (DELIM, END):
            continue
        if p == b"packfile":
            return demux(reader, progress)
        if p.startswith(b"ERR "):
            raise GitError("remote: " + p[4:].decode("utf-8", "replace"))
        # "acknowledgments", "shallow-info" or "wanted-refs" sections: read their lines and go on
        while True:
            q = reader.line()
            if q is None or q in (FLUSH, DELIM, END):
                break


# --- receive-pack (push), protocol version 0 --------------------------------------------------------------------------------------------------------
def parse_v0_advertisement(reader):
    """``(refs, capabilities)`` from a version 0/1 ref advertisement: ``[(name, oid)]``."""
    refs, caps = [], set()
    first = True
    while True:
        line = reader.line()
        if line is None or line == FLUSH:
            break
        if line in (DELIM, END):
            continue
        if line.startswith(b"ERR "):
            raise GitError("remote: " + line[4:].decode("utf-8", "replace"))
        if line.startswith(b"version "):
            continue
        text = line.decode("utf-8", "surrogateescape")
        if first:
            text, _, capline = text.partition("\0")
            caps = set(capline.split(" ")) if capline else set()
            first = False
        oid, _, name = text.partition(" ")
        if name == "capabilities^{}":
            continue
        refs.append((name, oid))
    return refs, caps


def receive_pack_request(commands, pack, caps):
    """The bytes a client sends to ``receive-pack``: the commands (``(old, new, ref)``), a flush and the pack."""
    out = []
    for i, (old, new, ref) in enumerate(commands):
        line = "%s %s %s" % (old, new, ref)
        if i == 0:
            line += "\0" + " ".join(caps)
        out.append(pkt_text(line))
    out.append(b"0000")
    if pack is not None:
        out.append(pack)
    return b"".join(out)


def parse_report(reader, sideband):
    """The ``report-status`` of ``receive-pack``: ``(unpack status, {ref: None or the reason it was refused})``."""
    if sideband:
        data = b"".join(demux(reader))
        inner = PktReader(_Bytes(data).read)
    else:
        inner = reader
    unpack = None
    results = {}
    for line in inner.until_flush():
        text = line.decode("utf-8", "replace")
        if text.startswith("unpack "):
            unpack = text[7:]
        elif text.startswith("ok "):
            results[text[3:]] = None
        elif text.startswith("ng "):
            ref, _, why = text[3:].partition(" ")
            results[ref] = why or "failed"
    return unpack, results


class _Bytes:
    def __init__(self, data):
        self.data = data
        self.pos = 0

    def read(self, n):
        piece = self.data[self.pos:self.pos + n]
        self.pos += len(piece)
        return piece
