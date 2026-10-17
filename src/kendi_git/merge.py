"""Merging: merge bases, a three-way line merge, a three-way tree merge and the ``merge`` command.

What is implemented: the common ancestors of two commits, the line-level merge rule of git's xdiff (two changes conflict when they overlap *or touch*, unless they are
the same change), a tree merge by path (one side unchanged: take the other; both changed: merge the text; modes merged independently) and the fast-forward / true merge
of a branch into the current one; a conflicted merge is left the way git leaves it (conflict markers in the files, the three stages in the index, ``MERGE_HEAD`` and
``MERGE_MSG``) and can be concluded with ``commit`` or undone with ``abort``. What is not done: rename detection (a rename on one side plus an edit on the other is a
conflict, where git merges it), merging more than one common ancestor (criss-cross histories are refused) and the exotic conflicts (symlinks, submodules, a file against
a directory), which are refused before anything is touched.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from . import checkout as co
from . import index as ix
from .diff import is_binary, myers, split_lines
from .errors import GitError
from .objects import MODE_EXEC, MODE_FILE


# --- common ancestors -----------------------------------------------------------------------------------------------------------------------------
def ancestors(repo, tip):
    """All commits reachable from ``tip``, itself included."""
    return repo.reachable([tip])


def merge_bases(repo, a, b):
    """The best common ancestors of two commits (usually one): the common ancestors that are not themselves ancestors of another common ancestor."""
    common = ancestors(repo, a) & ancestors(repo, b)
    covered = set()
    stack = [p for c in common for p in repo.read_commit(c).parents]
    while stack:
        oid = stack.pop()
        if oid in covered:
            continue
        covered.add(oid)
        stack.extend(repo.read_commit(oid).parents)
    return sorted(common - covered)


# --- the line merge: a port of xdiff's xmerge.c ----------------------------------------------------------------------------------------------------
MERGE_MINIMAL, MERGE_EAGER, MERGE_ZEALOUS, MERGE_ZEALOUS_ALNUM = 0, 1, 2, 3


def _hunks(a, b):
    """``[(i, removed, j, added)]``: runs of change turning ``a`` into ``b`` (ranges start at ``i`` in ``a`` and ``j`` in ``b``), as git's xdiff finds them."""
    removed, added = myers(a, b)
    out, i, j = [], 0, 0
    while i < len(a) or j < len(b):
        if i < len(a) and not removed[i] and j < len(b) and not added[j]:
            i += 1
            j += 1
            continue
        si, sj = i, j
        while i < len(a) and removed[i]:
            i += 1
        while j < len(b) and added[j]:
            j += 1
        out.append((si, i - si, sj, j - sj))
    return out


class _Merge:
    """xdmerge_t: one stretch of the result. ``mode`` 0 is a conflict, 1 takes our side, 2 takes theirs."""
    __slots__ = ("mode", "i0", "chg0", "i1", "chg1", "i2", "chg2")

    def __init__(self, mode, i0, chg0, i1, chg1, i2, chg2):
        self.mode, self.i0, self.chg0, self.i1, self.chg1, self.i2, self.chg2 = mode, i0, chg0, i1, chg1, i2, chg2


def _append_merge(merges, mode, i0, chg0, i1, chg1, i2, chg2):
    m = merges[-1] if merges else None
    if m is not None and (i1 <= m.i1 + m.chg1 or i2 <= m.i2 + m.chg2):          # touching or overlapping the previous stretch: fuse them
        if mode != m.mode:
            m.mode = 0
        m.chg0 = i0 + chg0 - m.i0
        m.chg1 = i1 + chg1 - m.i1
        m.chg2 = i2 + chg2 - m.i2
    else:
        merges.append(_Merge(mode, i0, chg0, i1, chg1, i2, chg2))


def _do_merge(base, ours, theirs, level):
    """xdl_do_merge: the list of stretches (conflicts and one-sided changes) of a three-way merge."""
    x1, x2 = _hunks(base, ours), _hunks(base, theirs)
    merges = []
    a = b = 0
    while a < len(x1) and b < len(x2):
        s1, s2 = x1[a], x2[b]
        if s1[0] + s1[1] < s2[0]:
            _append_merge(merges, 1, s1[0], s1[1], s1[2], s1[3], s2[2] - s2[0] + s1[0], s1[1])
            a += 1
            continue
        if s2[0] + s2[1] < s1[0]:
            _append_merge(merges, 2, s2[0], s2[1], s1[2] - s1[0] + s2[0], s2[1], s2[2], s2[3])
            b += 1
            continue
        if level == MERGE_MINIMAL or s1[0] != s2[0] or s1[1] != s2[1] or s1[3] != s2[3] or ours[s1[2]:s1[2] + s1[3]] != theirs[s2[2]:s2[2] + s2[3]]:
            off = s1[0] - s2[0]
            ffo = off + s1[1] - s2[1]
            i0, i1, i2 = s1[0], s1[2], s2[2]
            if off > 0:
                i0 -= off
                i1 -= off
            else:
                i2 += off
            chg0 = s1[0] + s1[1] - i0
            chg1 = s1[2] + s1[3] - i1
            chg2 = s2[2] + s2[3] - i2
            if ffo < 0:
                chg0 -= ffo
                chg1 -= ffo
            else:
                chg2 += ffo
            _append_merge(merges, 0, i0, chg0, i1, chg1, i2, chg2)
        end1, end2 = s1[0] + s1[1], s2[0] + s2[1]
        if end1 >= end2:
            b += 1
        if end2 >= end1:
            a += 1
    while a < len(x1):
        s1 = x1[a]
        _append_merge(merges, 1, s1[0], s1[1], s1[2], s1[3], s1[0] + len(theirs) - len(base), s1[1])
        a += 1
    while b < len(x2):
        s2 = x2[b]
        _append_merge(merges, 2, s2[0], s2[1], s2[0] + len(ours) - len(base), s2[1], s2[2], s2[3])
        b += 1
    if level >= MERGE_ZEALOUS:
        merges = _refine_conflicts(ours, theirs, merges)
        _simplify_non_conflicts(ours, merges, level > MERGE_ZEALOUS)
    return merges


def _refine_conflicts(ours, theirs, merges):
    """xdl_refine_conflicts: shrink every conflict to the lines that really differ between the two sides (and drop it if they turn out identical)."""
    out = []
    for m in merges:
        if m.mode != 0 or m.chg1 == 0 or m.chg2 == 0:
            out.append(m)
            continue
        left, right = ours[m.i1:m.i1 + m.chg1], theirs[m.i2:m.i2 + m.chg2]
        script = _hunks(left, right)
        if not script:
            m.mode = 1                                           # both sides say the same thing
            out.append(m)
            continue
        for k, (i, removed, j, added) in enumerate(script):
            out.append(_Merge(0, m.i0, m.chg0, m.i1 + i, removed, m.i2 + j, added))
    return out


def _lines_contain_alnum(lines):
    return any(c in b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" for line in lines for c in line)


def _simplify_non_conflicts(ours, merges, simplify_if_no_alnum):
    """xdl_simplify_non_conflicts: two conflicts at most three lines apart become one."""
    k = 0
    while k + 1 < len(merges):
        m, nxt = merges[k], merges[k + 1]
        begin, end = m.i1 + m.chg1, nxt.i1
        if m.mode != 0 or nxt.mode != 0 or (end - begin > 3 and (not simplify_if_no_alnum or _lines_contain_alnum(ours[begin:end]))):
            k += 1
        else:
            m.chg1 = nxt.i1 + nxt.chg1 - m.i1
            m.chg2 = nxt.i2 + nxt.chg2 - m.i2
            del merges[k + 1]


def _is_eol_crlf(lines, i):
    """git's is_eol_crlf: 1 if line ``i`` ends in CR LF, 0 if in LF, -1 when the file gives no hint (empty, or one unterminated line)."""
    if i < len(lines) - 1:
        return 1 if len(lines[i]) > 1 and lines[i][-2:-1] == b"\r" else 0
    if not lines:
        return -1
    if lines[i].endswith(b"\n"):
        return 1 if len(lines[i]) > 1 and lines[i][-2:-1] == b"\r" else 0
    if i == 0:
        return -1
    return 1 if len(lines[i - 1]) > 1 and lines[i - 1][-2:-1] == b"\r" else 0


def _needs_cr(base, ours, theirs, m):
    """is_cr_needed: do the conflict markers get CR LF line ends? Only if the surrounding text of both sides and the base uses them."""
    needs = _is_eol_crlf(ours, m.i1 - 1 if m.i1 else 0)
    if needs:
        needs = _is_eol_crlf(theirs, m.i2 - 1 if m.i2 else 0)
    if needs:
        needs = _is_eol_crlf(base, 0)
    return needs > 0


def xdl_merge(base, ours, theirs, name1="HEAD", name2="theirs", level=MERGE_ZEALOUS, marker_size=7):
    """Merge ``ours`` and ``theirs`` (lists of lines with their endings) around ``base`` like git's xdl_merge. Returns ``(lines, number of conflicts)``; conflicts are written
    with ``<<<<<<<`` / ``=======`` / ``>>>>>>>`` markers naming the two sides."""
    merges = _do_merge(base, ours, theirs, level)
    out, pos, conflicts = [], 0, 0
    for m in merges:
        if m.mode == 0:
            conflicts += 1
            out += ours[pos:m.i1]
            nl = b"\r\n" if _needs_cr(base, ours, theirs, m) else b"\n"
            out.append(b"<" * marker_size + (b" " + name1.encode() if name1 else b"") + nl)
            out += _with_newline(ours[m.i1:m.i1 + m.chg1], nl)
            out.append(b"=" * marker_size + nl)
            out += _with_newline(theirs[m.i2:m.i2 + m.chg2], nl)
            out.append(b">" * marker_size + (b" " + name2.encode() if name2 else b"") + nl)
        else:
            out += ours[pos:m.i1]
            out += ours[m.i1:m.i1 + m.chg1] if m.mode & 1 else []
            out += theirs[m.i2:m.i2 + m.chg2] if m.mode & 2 else []
        pos = m.i1 + m.chg1
    out += ours[pos:]
    return out, conflicts


def _with_newline(lines, nl=b"\n"):
    if lines and not lines[-1].endswith(b"\n"):
        return lines[:-1] + [lines[-1] + nl]
    return lines


def merge3(base, ours, theirs):
    """Merge two line lists derived from ``base``. Returns the merged lines, or ``None`` when some change cannot be combined."""
    lines, conflicts = xdl_merge(base, ours, theirs)
    return None if conflicts else lines


# --- the tree merge -------------------------------------------------------------------------------------------------------------------------------
@dataclass
class Conflict:
    """A path the merge could not settle: what goes into the index (stages 1 = base, 2 = ours, 3 = theirs) and what is left in the work tree."""
    path: str
    kind: str                  # "content", "add/add", "modify/delete" or "binary"
    stages: dict               # {1: (mode, oid), 2: ..., 3: ...} with the sides that exist
    worktree: tuple            # (mode, oid) of the file left in the work tree (conflict markers for text), or None to leave nothing
    detail: str = ""


def _blob(repo, entry):
    return repo.odb.read(entry[1])[1] if entry else b""


def merge_entry_full(repo, path, base, ours, theirs, names):
    """Merge one path's three versions (each ``(mode, oid)`` or ``None``). Returns ``(merged entry or None, Conflict or None, auto_merged)``."""
    if ours == theirs:
        return ours, None, False
    if base == ours:
        return theirs, None, False
    if base == theirs:
        return ours, None, False
    n1, n2 = names
    if ours is None or theirs is None:                                       # one side deleted what the other changed
        keeper = theirs if ours is None else ours
        side = n2 if ours is None else n1
        gone = n1 if ours is None else n2
        stages = {1: base, (3 if ours is None else 2): keeper}
        return keeper, Conflict(path, "modify/delete", stages, keeper, "%s deleted in %s and modified in %s.  Version %s of %s left in tree." % (path, gone, side, side, path)), False
    regular = (MODE_FILE, MODE_EXEC)
    if ours[0] not in regular or theirs[0] not in regular or (base is not None and base[0] not in regular):
        raise GitError("%s was changed differently on both sides in a way (symlink or submodule) this program does not merge" % path)
    base_mode = base[0] if base else None
    if ours[0] == theirs[0]:
        mode = ours[0]
    elif ours[0] == base_mode:
        mode = theirs[0]
    elif theirs[0] == base_mode:
        mode = ours[0]
    else:
        raise GitError("the file mode of %s was changed differently on both sides" % path)
    if ours[1] == theirs[1]:
        return (mode, ours[1]), None, False
    if base is not None and base[1] == ours[1]:
        return (mode, theirs[1]), None, False
    if base is not None and base[1] == theirs[1]:
        return (mode, ours[1]), None, False
    data = [_blob(repo, v) for v in (base, ours, theirs)]
    stages = {1: base, 2: ours, 3: theirs} if base is not None else {2: ours, 3: theirs}
    if any(is_binary(d) for d in data):
        return ours, Conflict(path, "binary", stages, ours, "Cannot merge binary files: %s (%s vs. %s)" % (path, n1, n2)), True
    lines, conflicts = xdl_merge(split_lines(data[0]), split_lines(data[1]), split_lines(data[2]), n1, n2)
    entry = (mode, repo.odb.write("blob", b"".join(lines)))
    if not conflicts:
        return entry, None, True
    return entry, Conflict(path, "content" if base is not None else "add/add", stages, entry), True


def merge_trees_full(repo, base, ours, theirs, names=("HEAD", "theirs")):
    """Three-way merge of ``{path: (mode, oid)}`` snapshots. Returns ``(merged, conflicts, auto_merged)``: ``merged`` the settled paths, ``conflicts`` a list of
    ``Conflict`` and ``auto_merged`` the paths whose contents had to be merged line by line."""
    merged, conflicts, auto = {}, [], []
    for path in sorted(set(base) | set(ours) | set(theirs), key=lambda p: p.encode("utf-8", "surrogateescape")):
        entry, conflict, was_auto = merge_entry_full(repo, path, base.get(path), ours.get(path), theirs.get(path), names)
        if was_auto:
            auto.append(path)
        if conflict:
            conflicts.append(conflict)
        elif entry is not None:
            merged[path] = entry
    for path in list(merged) + [c.path for c in conflicts]:                  # a path cannot be a file and a directory at once
        parts = path.split("/")
        for i in range(1, len(parts)):
            parent = "/".join(parts[:i])
            if parent in merged or any(c.path == parent for c in conflicts):
                raise GitError("a file and a directory with the same name (%s) were added on different sides; this program does not merge that" % parent)
    return merged, conflicts, auto


def merge_trees(repo, base, ours, theirs):
    """Three-way merge of ``{path: (mode, oid)}`` snapshots. Returns ``(merged, conflicts)``; ``conflicts`` is ``[(path, reason)]`` and ``merged`` holds the settled paths."""
    try:
        merged, conflicts, _auto = merge_trees_full(repo, base, ours, theirs)
    except GitError as exc:
        return {}, [("(tree)", str(exc))]
    return merged, [(c.path, c.kind) for c in conflicts]


# --- the command ----------------------------------------------------------------------------------------------------------------------------------
@dataclass
class MergeResult:
    kind: str                 # "up-to-date", "fast-forward", "merge" or "conflict"
    oid: object               # the commit the current branch now points at
    old: object = None        # where it pointed before
    conflicts: list = None    # for "conflict": the Conflict objects, in path order
    notes: list = None        # (kind, path, text) progress lines like git's "Auto-merging x" / "CONFLICT (content): ..."


STATE_FILES = ("MERGE_HEAD", "MERGE_MSG", "MERGE_MODE", "AUTO_MERGE")


def merge_in_progress(repo):
    return os.path.exists(os.path.join(repo.gitdir, "MERGE_HEAD"))


def clear_merge_state(repo):
    for name in STATE_FILES:
        try:
            os.unlink(os.path.join(repo.gitdir, name))
        except FileNotFoundError:
            pass


def default_message(repo, spec, target_oid):
    branch = repo.refs.current_branch()
    if repo.refs.exists("refs/heads/" + spec):
        what = "branch '%s'" % spec
    elif repo.refs.exists("refs/tags/" + spec):
        what = "tag '%s'" % spec
    elif repo.refs.exists("refs/remotes/" + spec):
        what = "remote-tracking branch '%s'" % spec
    else:
        what = "commit '%s'" % spec
    text = "Merge " + what
    if branch and branch not in ("main", "master"):
        text += " into " + branch
    return text


def _write_state(repo, name, text):
    with open(os.path.join(repo.gitdir, name), "w", encoding="utf-8", errors="surrogateescape") as f:
        f.write(text)


def merge(repo, spec, message=None, ff="allow"):
    if ff not in ("allow", "never", "only"):
        raise GitError(f"unknown fast-forward mode '{ff}'")
    if merge_in_progress(repo):
        raise GitError("you have not concluded your merge (MERGE_HEAD exists): commit the result, or run 'merge --abort'")
    target = repo.commit_of(spec)
    head = repo.refs.head_oid()
    if head is None:                                       # merging into an unborn branch just checks the commit out
        co.apply_switch(repo, {}, co.snapshot_of_commit(repo, target))
        _advance(repo, target, None, "merge %s: Fast-forward" % spec)
        return MergeResult("fast-forward", target, None)
    if target == head or target in ancestors(repo, head):
        return MergeResult("up-to-date", head, head)
    old_files = co.snapshot_of_commit(repo, head)
    if head in ancestors(repo, target) and ff != "never":
        co.apply_switch(repo, old_files, co.snapshot_of_commit(repo, target))
        repo.refs._write_locked("ORIG_HEAD", head)
        _advance(repo, target, head, "merge %s: Fast-forward" % spec)
        return MergeResult("fast-forward", target, head)
    if ff == "only":
        raise GitError("not possible to fast-forward, aborting")
    bases = merge_bases(repo, head, target)
    if not bases:
        raise GitError("refusing to merge unrelated histories")
    if len(bases) > 1:
        raise GitError("the histories have %d common ancestors (a criss-cross merge), which this program does not handle" % len(bases))
    index = repo.read_index()
    if any(stage for _p, stage in index.entries):
        raise GitError("you have unmerged paths: finish resolving them first")
    if repo.index_files(index) != old_files:
        entry = repo.log_entry("merge %s: updating HEAD" % spec)           # git logs this no-op update before it gives up on a dirty index
        if entry:
            repo.refs.append_log("HEAD", head, head, *entry)
        raise GitError("your index has changes that are not committed: commit or reset them before merging")
    merged, conflicts, auto = merge_trees_full(repo, co.snapshot_of_commit(repo, bases[0]), old_files, co.snapshot_of_commit(repo, target), ("HEAD", spec))
    notes = [("auto", p, "Auto-merging " + p) for p in auto]
    snapshot = dict(merged)
    for c in conflicts:                                                    # what the work tree is to hold: for a conflict, markers or the surviving version
        if c.worktree is not None:
            snapshot[c.path] = c.worktree
    co.apply_switch(repo, old_files, snapshot)                             # refuses, touching nothing, if local changes are in the way
    text = message if message is not None else default_message(repo, spec, target)
    repo.refs._write_locked("ORIG_HEAD", head)
    if conflicts:
        index = repo.read_index()
        for c in conflicts:
            index.remove(c.path)
            for stage, (mode, oid) in sorted(c.stages.items()):
                index.entries[(c.path, stage)] = ix.IndexEntry(c.path, mode, oid, stage=stage)
        repo.write_index(index)
        _write_state(repo, "MERGE_HEAD", target + "\n")
        _write_state(repo, "MERGE_MODE", "no-ff" if ff == "never" else "")
        _write_state(repo, "MERGE_MSG", text.rstrip("\n") + "\n\n# Conflicts:\n" + "".join("#\t%s\n" % c.path for c in conflicts))
        for c in conflicts:
            notes.append(("conflict", c.path, c.detail if c.kind in ("modify/delete", "binary") else "Merge conflict in " + c.path))
        return MergeResult("conflict", head, head, conflicts, notes)
    tree = ix.write_tree(repo.odb, merged)
    oid = repo.make_commit(tree, [head, target], text)
    _advance(repo, oid, head, "merge %s: Merge made by the 'ort' strategy." % spec)
    return MergeResult("merge", oid, head, None, notes)


def abort(repo):
    """``merge --abort`` (``reset --merge``): put every path the merge touched back as it is in ``HEAD``, keep unrelated local changes, drop the merge state."""
    if not merge_in_progress(repo):
        raise GitError("there is no merge to abort (MERGE_HEAD missing)")
    head_files = co.snapshot_of_commit(repo, repo.refs.head_oid())
    index = repo.read_index()
    unmerged = {p for p, s in index.entries if s}
    touched = {p for p in {p for p, _s in index.entries} | set(head_files) if p in unmerged or (index.get(p) and (index.get(p).mode, index.get(p).oid) != head_files.get(p))
               or (index.get(p) is None and p in head_files)}
    blocked = []
    for p in sorted(touched - unmerged):
        e, st = index.get(p), repo._lstat(p)
        if e is not None and st is not None and not repo._entry_unchanged(e, p, st, index):
            blocked.append(p)
    if blocked:
        raise GitError("cannot abort: these files have local changes on top of the merge: " + ", ".join(blocked))
    for p in sorted(touched):
        index.remove(p)
        if p in head_files:
            mode, oid = head_files[p]
            co.write_worktree_file(repo, p, mode, repo.odb.read(oid)[1] if mode != 0o160000 else b"")
            index.set(ix.IndexEntry(p, mode, oid, **ix.stat_fields(os.lstat(repo._full(p)))))
        else:
            try:
                os.unlink(repo._full(p))
            except FileNotFoundError:
                pass
            repo._prune_empty_dirs(os.path.dirname(p))
    repo.write_index(index)
    clear_merge_state(repo)
    head = repo.refs.head_oid()
    entry = repo.log_entry("reset: moving to HEAD")
    if entry:
        repo.refs.append_log("HEAD", head, head, *entry)


def _advance(repo, oid, old, message):
    repo._advance_head(oid, old, message)
