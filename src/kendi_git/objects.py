"""The four Git object types and their exact byte formats.

An object's id is the SHA-1 of ``"<type> <size>\\0" + content``; trees list ``<octal mode> <name>\\0<20 raw id bytes>`` sorted bytewise with directories compared as
if their name ended in ``/``; commits and tags are header lines, a blank line and the message. Parsing keeps everything needed to re-serialise to the very same
bytes (unknown commit headers such as ``gpgsig`` and ``encoding`` included), so an object read and written back keeps its id.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass, field

from .errors import GitError

MODE_TREE, MODE_FILE, MODE_EXEC, MODE_LINK, MODE_GITLINK = 0o040000, 0o100644, 0o100755, 0o120000, 0o160000
KINDS = ("blob", "tree", "commit", "tag")
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
NULL_OID = "0" * 40


def hash_object(kind, data):
    """The object id (40 hex digits) of ``data`` stored as ``kind``."""
    if kind not in KINDS:
        raise GitError(f"unknown object type {kind!r}")
    return hashlib.sha1(b"%s %d\x00" % (kind.encode(), len(data)) + data).hexdigest()


def encode_object(kind, data):
    """The bytes git stores in a loose object before compression."""
    return b"%s %d\x00" % (kind.encode(), len(data)) + data


def is_oid(text):
    return bool(re.fullmatch(r"[0-9a-f]{40}", text or ""))


# --- trees ---------------------------------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class TreeEntry:
    mode: int
    name: str                       # a path component, decoded with the filesystem encoding (undecodable bytes survive as surrogate escapes)
    oid: str

    @property
    def is_tree(self):
        return self.mode == MODE_TREE

    def sort_key(self):
        raw = os.fsencode(self.name)
        return raw + b"/" if self.is_tree else raw


def parse_tree(data):
    entries, i = [], 0
    while i < len(data):
        space = data.index(b" ", i)
        nul = data.index(b"\x00", space)
        if nul + 21 > len(data):
            raise GitError("truncated tree object")
        entries.append(TreeEntry(int(data[i:space], 8), os.fsdecode(data[space + 1:nul]), data[nul + 1:nul + 21].hex()))
        i = nul + 21
    return entries


def serialize_tree(entries):
    """The tree object content for these entries, in git's order (names bytewise, a directory sorting as ``name/``)."""
    out = []
    for e in sorted(entries, key=TreeEntry.sort_key):
        out.append(b"%o %s\x00" % (e.mode, os.fsencode(e.name)) + bytes.fromhex(e.oid))
    return b"".join(out)


# --- identities -----------------------------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Ident:
    name: str
    email: str
    time: int
    tz: str                         # "+0100"

    def __str__(self):
        return f"{self.name} <{self.email}> {self.time} {self.tz}"

    @staticmethod
    def parse(text):
        m = re.fullmatch(r"(.*?) ?<([^>]*)> (-?\d+) ([+-]\d{4})", text)
        if not m:
            raise GitError(f"cannot parse identity {text!r}")
        return Ident(m.group(1), m.group(2), int(m.group(3)), m.group(4))


def local_tz(when=None):
    """The local UTC offset in git's ``+HHMM`` form at the given time."""
    off = time.localtime(when).tm_gmtoff if when is not None else time.localtime().tm_gmtoff
    sign = "+" if off >= 0 else "-"
    off = abs(off) // 60
    return f"{sign}{off // 60:02d}{off % 60:02d}"


# --- commits and tags -----------------------------------------------------------------------------------------------------------------------------
def _split_header_block(data):
    head, sep, message = data.partition(b"\n\n")
    if not sep:                                      # a commit with no message at all ends right after its headers
        head, message = data.rstrip(b"\n"), b""
    return head.decode("utf-8", errors="surrogateescape"), message.decode("utf-8", errors="surrogateescape")


def _parse_headers(head):
    """``[(key, value)]`` with continuation lines (those starting with a space, as in ``gpgsig``) folded into the value, newlines kept."""
    out = []
    for line in head.split("\n"):
        if line.startswith(" ") and out:
            out[-1] = (out[-1][0], out[-1][1] + "\n" + line[1:])
        elif line:
            key, _, value = line.partition(" ")
            out.append((key, value))
    return out


def _emit_headers(headers):
    return "".join(f"{k} {v.replace(chr(10), chr(10) + ' ')}\n" for k, v in headers)


@dataclass
class Commit:
    tree: str
    parents: list
    author: Ident
    committer: Ident
    message: str
    extra: list = field(default_factory=list)       # other headers in order: [(key, value)], e.g. gpgsig, encoding, mergetag
    _raw_order: list = field(default=None, repr=False)

    @staticmethod
    def parse(data):
        head, message = _split_header_block(data)
        tree, parents, author, committer, extra, order = None, [], None, None, [], []
        for key, value in _parse_headers(head):
            if key == "tree":
                tree = value
                order.append(("tree", None))
            elif key == "parent":
                parents.append(value)
                order.append(("parent", None))
            elif key == "author":
                author = Ident.parse(value)
                order.append(("author", None))
            elif key == "committer":
                committer = Ident.parse(value)
                order.append(("committer", None))
            else:
                extra.append((key, value))
                order.append(("extra", None))
        if tree is None or author is None or committer is None:
            raise GitError("malformed commit object")
        return Commit(tree, parents, author, committer, message, extra, order)

    def serialize(self):
        pieces = {"tree": [("tree", self.tree)], "parent": [("parent", p) for p in self.parents], "author": [("author", str(self.author))],
                  "committer": [("committer", str(self.committer))]}
        if self._raw_order:                          # re-emit in the order the object had, so that it keeps its id
            queue = {k: list(v) for k, v in pieces.items()}
            queue["extra"] = list(self.extra)
            headers = [queue[kind].pop(0) for kind, _ in self._raw_order]
        else:
            headers = pieces["tree"] + pieces["parent"] + pieces["author"] + pieces["committer"] + list(self.extra)
        return (_emit_headers(headers) + "\n" + self.message).encode("utf-8", errors="surrogateescape")

    @property
    def subject(self):
        return self.message.strip("\n").split("\n", 1)[0]


@dataclass
class Tag:
    object: str
    type: str
    name: str
    tagger: Ident
    message: str

    @staticmethod
    def parse(data):
        head, message = _split_header_block(data)
        fields = dict(_parse_headers(head))
        tagger = Ident.parse(fields["tagger"]) if "tagger" in fields else None
        return Tag(fields["object"], fields["type"], fields["tag"], tagger, message)

    def serialize(self):
        head = f"object {self.object}\ntype {self.type}\ntag {self.name}\n" + (f"tagger {self.tagger}\n" if self.tagger else "")
        return (head + "\n" + self.message).encode("utf-8", errors="surrogateescape")


def clean_message(text, strip_comments=False):
    """What ``git commit -m`` stores: trailing whitespace stripped from every line, runs of blank lines collapsed, leading and trailing blank lines removed, one final newline.
    ``git tag -m`` additionally drops the lines that start with ``#`` (``strip_comments=True``)."""
    lines = [ln.rstrip() for ln in text.split("\n") if not (strip_comments and ln.startswith("#"))]
    out, blank = [], False
    for ln in lines:
        if not ln:
            blank = bool(out)
            continue
        if blank:
            out.append("")
            blank = False
        out.append(ln)
    return "\n".join(out) + "\n" if out else ""
