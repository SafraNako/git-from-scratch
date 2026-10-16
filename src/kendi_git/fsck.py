"""Integrity checking in the manner of ``git fsck --strict``.

Every stored object is read back and verified (loose files: zlib stream, header, and that the content hashes to the file name; packs: the checksums and every
object, deltas resolved), parsed according to its type and checked for the structural rules git's fsck enforces (tree entries sorted, unique and well formed;
commit and tag headers complete and well formed), and the links between objects are followed from the references and the index: a missing or wrongly typed target is a
broken link, and objects nothing reaches are reported as dangling (which is a notice, not an error).
"""
from __future__ import annotations

import hashlib
import os
import re
import zlib
from dataclasses import dataclass, field

from .errors import GitError
from .objects import KINDS, MODE_GITLINK, MODE_TREE, is_oid

_IDENT = re.compile(r"^[^<>]*<[^<>]*> (0|[1-9][0-9]*) [+-][0-9]{4}$")
_MODES = {b"40000": MODE_TREE, b"100644": 0o100644, b"100755": 0o100755, b"120000": 0o120000, b"160000": MODE_GITLINK}
_HEX = re.compile(r"^[0-9a-f]{40}$")


@dataclass
class Report:
    errors: list = field(default_factory=list)            # problems: the repository is damaged or malformed
    warnings: list = field(default_factory=list)          # git's informational findings: reported, but not a failure
    dangling: list = field(default_factory=list)          # [(kind, oid)] objects nothing refers to
    objects: int = 0
    bad_objects: set = field(default_factory=set)        # ids that are corrupt, malformed, or missing

    @property
    def ok(self):
        return not self.errors


def _tree_entries(data):
    """Strict tree parser: returns ``[(mode_text, name_bytes, oid)]`` or raises ``ValueError`` for a malformed tree."""
    out, pos = [], 0
    while pos < len(data):
        space = data.find(b" ", pos)
        nul = data.find(b"\x00", space + 1) if space >= 0 else -1
        if space < 0 or nul < 0 or nul + 21 > len(data):
            raise ValueError("truncated tree entry")
        out.append((data[pos:space], data[space + 1:nul], data[nul + 1:nul + 21].hex()))
        pos = nul + 21
    return out


def _sort_key(name, mode):
    return name + (b"/" if mode == MODE_TREE else b"")


INFO_ONLY = {"missingTaggerEntry", "badFilemode"}      # git reports these but does not fail on them, not even with --strict (found by comparing with git)


def check_tree(data, strict=True):
    """Problems found in a tree object, as ``[(code, text)]``, and the references it holds as ``[(oid, wanted kind or None)]``."""
    problems, links = [], []
    try:
        entries = _tree_entries(data)
    except ValueError as exc:
        return [("badTree", str(exc))], links
    previous = None
    seen = set()
    for mode_text, name, oid in entries:
        mode = _MODES.get(mode_text)
        if mode is None and mode_text == b"040000":
            mode = MODE_TREE
            problems.append(("zeroPaddedFilemode", "contains a zero-padded file mode"))
        elif mode is None:
            problems.append(("badFilemode", "contains a bad file mode %r" % mode_text.decode("ascii", "replace")))
            continue
        if not name:
            problems.append(("emptyName", "contains an empty name"))
        if b"/" in name:
            problems.append(("fullPathname", "contains a full pathname"))
        if name == b".":
            problems.append(("hasDot", "contains '.'"))
        if name == b"..":
            problems.append(("hasDotdot", "contains '..'"))
        if name.lower() == b".git":
            problems.append(("hasDotgit", "contains '.git'"))
        if name in seen:
            problems.append(("duplicateEntries", "contains duplicate file entries"))
        seen.add(name)
        key = _sort_key(name, mode)
        if previous is not None and key < previous:
            problems.append(("treeNotSorted", "not properly sorted"))
        elif previous is not None and key == previous:
            problems.append(("duplicateEntries", "contains duplicate file entries"))
        previous = key
        if mode != MODE_GITLINK:
            links.append((oid, "tree" if mode == MODE_TREE else "blob"))
    if not strict:
        problems = [p for p in problems if p[0] in ("badTree", "treeNotSorted", "duplicateEntries", "badFilemode")]
    return problems, links


def _header_lines(data):
    head, sep, _msg = data.partition(b"\n\n")
    if not sep and not data.endswith(b"\n"):
        return None
    return head.decode("utf-8", "surrogateescape").split("\n")


def check_commit(data):
    problems, links = [], []
    lines = _header_lines(data)
    if lines is None:
        return [("unterminatedHeader", "unterminated header")], links
    i = 0
    if not lines or not lines[0].startswith("tree ") or not _HEX.match(lines[0][5:]):
        problems.append(("missingTree", "invalid format - expected 'tree <hash>' line"))
    else:
        links.append((lines[0][5:], "tree"))
        i = 1
    while i < len(lines) and lines[i].startswith("parent "):
        if _HEX.match(lines[i][7:]):
            links.append((lines[i][7:], "commit"))
        else:
            problems.append(("badParentSha1", "invalid 'parent' line format"))
        i += 1
    if i >= len(lines) or not lines[i].startswith("author "):
        problems.append(("missingAuthor", "invalid format - expected 'author' line"))
    else:
        if not _IDENT.match(lines[i][7:]):
            problems.append(("badAuthor", "invalid author identity"))
        i += 1
        if i >= len(lines) or not lines[i].startswith("committer "):
            problems.append(("missingCommitter", "invalid format - expected 'committer' line"))
        elif not _IDENT.match(lines[i][10:]):
            problems.append(("badCommitter", "invalid committer identity"))
    return problems, links


def check_tag(data):
    problems, links = [], []
    lines = _header_lines(data)
    if lines is None:
        return [("unterminatedHeader", "unterminated header")], links
    if len(lines) < 3 or not lines[0].startswith("object ") or not _HEX.match(lines[0][7:]):
        return [("badTagObject", "invalid format - expected 'object <hash>' line")], links
    if not lines[1].startswith("type ") or lines[1][5:] not in KINDS:
        return [("badType", "invalid 'type' line")], links
    if not lines[2].startswith("tag "):
        return [("missingTagEntry", "invalid format - expected 'tag' line")], links
    links.append((lines[0][7:], lines[1][5:]))
    if len(lines) < 4 or not lines[3].startswith("tagger "):
        problems.append(("missingTaggerEntry", "invalid format - expected 'tagger' line"))
    elif not _IDENT.match(lines[3][7:]):
        problems.append(("badTagger", "invalid tagger identity"))
    return problems, links


def verify_loose(odb, oid):
    """Read a loose object file back and check it end to end; returns an error text or ``None``."""
    path = odb._loose_path(oid)
    try:
        with open(path, "rb") as f:
            raw = zlib.decompress(f.read())
    except zlib.error as exc:
        return f"corrupt zlib stream ({exc})"
    except OSError as exc:
        return f"unreadable ({exc})"
    nul = raw.find(b"\x00")
    if nul < 0:
        return "malformed header"
    kind, _, size = raw[:nul].partition(b" ")
    if kind.decode("ascii", "replace") not in KINDS or not size.isdigit() or int(size) != len(raw) - nul - 1:
        return "malformed header"
    if hashlib.sha1(raw).hexdigest() != oid:
        return "hash mismatch: the content does not hash to the file name"
    return None


def fsck(repo, strict=True):
    odb, report = repo.odb, Report()
    shallow = repo.shallow
    kinds, links, ids = {}, {}, []
    seen = set()
    for oid in odb.iter_oids():
        if oid not in seen:
            seen.add(oid)
            ids.append(oid)
    for pack in odb.packs():
        pack_ok, idx_ok = pack.verify_checksums()
        if not pack_ok:
            report.errors.append(f"{os.path.basename(pack.pack_path)}: the pack checksum does not match its content")
        if not idx_ok:
            report.errors.append(f"{os.path.basename(pack.idx_path)}: the index checksum does not match its content")
    report.objects = len(ids)
    for oid in ids:
        if os.path.exists(odb._loose_path(oid)):
            problem = verify_loose(odb, oid)
            if problem:
                report.errors.append(f"object {oid}: {problem}")
                report.bad_objects.add(oid)
                continue
        try:
            kind, data = odb.read(oid)
        except GitError as exc:
            report.errors.append(f"object {oid}: {exc}")
            report.bad_objects.add(oid)
            continue
        if hashlib.sha1(b"%s %d\x00" % (kind.encode(), len(data)) + data).hexdigest() != oid:
            report.errors.append(f"object {oid}: hash mismatch: the content does not hash to the object id")
            report.bad_objects.add(oid)
            continue
        kinds[oid] = kind
        problems, refs = {"tree": lambda d: check_tree(d, strict), "commit": check_commit, "tag": check_tag}.get(kind, lambda d: ([], []))(data)
        for code, text in problems:
            if code in INFO_ONLY:
                report.warnings.append(f"{kind} {oid}: {code}: {text}")
                continue
            report.errors.append(f"{kind} {oid}: {code}: {text}")
            report.bad_objects.add(oid)
        if kind == "commit" and oid in shallow:                    # the parents of a shallow boundary commit are deliberately absent
            refs = [(t, w) for t, w in refs if w != "commit"]
        links[oid] = refs
    # the roots: every reference and every blob the index names
    roots = []
    for name, oid in repo.refs.list("refs/"):
        roots.append((name, oid, None))
    head = None
    try:
        head = repo.refs.head_oid()
    except GitError as exc:
        report.errors.append(f"HEAD: {exc}")
    if head:
        roots.append(("HEAD", head, None))
    for name, oid, _ in list(roots):
        if name.startswith("refs/heads/") and oid in kinds and kinds[oid] != "commit":
            report.errors.append(f"{name}: not a commit (it points to a {kinds[oid]})")
    try:
        index = repo.read_index()
        for entry in index.entries.values():
            if entry.mode != MODE_GITLINK:
                roots.append(("index:" + entry.path, entry.oid, "blob"))
    except GitError as exc:
        report.errors.append(f"index: {exc}")
    used = set()
    for name, oid, _want in roots:
        if oid not in kinds and oid not in report.bad_objects:
            report.errors.append(f"{name}: points to missing object {oid}")
            report.bad_objects.add(oid)
        used.add(oid)
    for oid in ids:
        if oid not in kinds:
            continue
        for target, want in links.get(oid, []):
            used.add(target)
            if target not in kinds:
                state = "corrupt" if target in seen else "missing"
                report.errors.append(f"broken link from {kinds[oid]} {oid} to {want or 'object'} {target}: {state} {want or 'object'} {target}")
                report.bad_objects.add(target)
            elif want and kinds[target] != want:
                report.errors.append(f"broken link from {kinds[oid]} {oid} to {want} {target}: it is a {kinds[target]}")
                report.bad_objects.add(target)
    # git calls an object dangling when no other object (and no reference or index entry) refers to it
    for oid in ids:
        if oid in kinds and oid not in used:
            report.dangling.append((kinds[oid], oid))
    return report


__all__ = ["fsck", "Report", "check_tree", "check_commit", "check_tag", "verify_loose", "is_oid"]
