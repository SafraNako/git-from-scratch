"""Merging: merge bases, a three-way line merge, a three-way tree merge and the ``merge`` command.

What is implemented: the common ancestors of two commits, the line-level merge rule of git's xdiff (two changes conflict when they overlap *or touch*, unless they are
the same change), a tree merge by path (one side unchanged: take the other; both changed: merge the text; modes merged independently) and the fast-forward / true merge
of a branch into the current one. What is not: rename detection (a rename on one side plus an edit on the other is reported as a conflict, where git merges it), merging
more than one common ancestor (criss-cross histories are refused), and leaving conflict markers behind: a conflicted merge is refused and nothing is changed.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import checkout as co
from . import index as ix
from .diff import is_binary, myers, split_lines
from .errors import GitError
from .objects import MODE_EXEC, MODE_FILE


class MergeConflict(GitError):
    def __init__(self, conflicts):
        self.conflicts = conflicts
        super().__init__("merge conflict in: " + ", ".join(conflicts) + " (nothing was changed; resolve by merging by hand or rebase the branches)")


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


# --- the line merge -------------------------------------------------------------------------------------------------------------------------------
def _hunks(a, b):
    """``[(i, removed, j, added)]``: runs of change turning ``a`` into ``b`` (ranges start at ``i`` in ``a`` and ``j`` in ``b``)."""
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


def merge3(base, ours, theirs):
    """Merge two line lists derived from ``base``. Returns the merged lines, or ``None`` when some change cannot be combined."""
    h1, h2 = _hunks(base, ours), _hunks(base, theirs)
    result, pos, a, b = [], 0, 0, 0
    while a < len(h1) or b < len(h2):
        x = h1[a] if a < len(h1) else None
        y = h2[b] if b < len(h2) else None
        if y is None or (x is not None and x[0] + x[1] < y[0]):          # ours ends before theirs starts, with a line in between
            result += base[pos:x[0]] + ours[x[2]:x[2] + x[3]]
            pos = x[0] + x[1]
            a += 1
            continue
        if x is None or y[0] + y[1] < x[0]:
            result += base[pos:y[0]] + theirs[y[2]:y[2] + y[3]]
            pos = y[0] + y[1]
            b += 1
            continue
        # the two changes overlap or touch: fine only when they are the very same change
        if (x[0], x[1], x[3]) == (y[0], y[1], y[3]) and ours[x[2]:x[2] + x[3]] == theirs[y[2]:y[2] + y[3]]:
            result += base[pos:x[0]] + ours[x[2]:x[2] + x[3]]
            pos = x[0] + x[1]
            a += 1
            b += 1
            continue
        start, end = min(x[0], y[0]), max(x[0] + x[1], y[0] + y[1])
        mine, theirs_hunks = [x], [y]
        a += 1
        b += 1
        grew = True
        while grew:                                                       # absorb every further change that touches the region
            grew = False
            if a < len(h1) and h1[a][0] <= end:
                mine.append(h1[a])
                end = max(end, h1[a][0] + h1[a][1])
                a += 1
                grew = True
            if b < len(h2) and h2[b][0] <= end:
                theirs_hunks.append(h2[b])
                end = max(end, h2[b][0] + h2[b][1])
                b += 1
                grew = True
        left, right = _region(base, ours, mine, start, end), _region(base, theirs, theirs_hunks, start, end)
        if left != right:
            return None
        result += base[pos:start] + left
        pos = end
    return result + base[pos:]


def _region(base, side, hunks, start, end):
    text, p = [], start
    for i, removed, j, added in hunks:
        text += base[p:i] + side[j:j + added]
        p = i + removed
    return text + base[p:end]


# --- the tree merge -------------------------------------------------------------------------------------------------------------------------------
def merge_entry(repo, base, ours, theirs):
    """Merge one path's three versions (each ``(mode, oid)`` or ``None``). Returns ``(result, None)`` or ``(None, reason)``."""
    if ours == theirs:
        return ours, None
    if base == ours:
        return theirs, None
    if base == theirs:
        return ours, None
    if ours is None or theirs is None:
        return None, "modified on one side and deleted on the other"
    regular = (MODE_FILE, MODE_EXEC)
    if ours[0] not in regular or theirs[0] not in regular or (base is not None and base[0] not in regular):
        return None, "changed differently on both sides (symlinks and submodules are not merged)"
    base_mode = base[0] if base else None
    if ours[0] == theirs[0]:
        mode = ours[0]
    elif ours[0] == base_mode:
        mode = theirs[0]
    elif theirs[0] == base_mode:
        mode = ours[0]
    else:
        return None, "the file mode was changed differently on both sides"
    if ours[1] == theirs[1]:
        return (mode, ours[1]), None
    data = [repo.odb.read(v[1])[1] if v else b"" for v in (base, ours, theirs)]
    if base is not None and base[1] == ours[1]:
        return (mode, theirs[1]), None
    if base is not None and base[1] == theirs[1]:
        return (mode, ours[1]), None
    if any(is_binary(d) for d in data):
        return None, "binary file changed on both sides"
    merged = merge3(split_lines(data[0]), split_lines(data[1]), split_lines(data[2]))
    if merged is None:
        return None, "the same lines were changed on both sides"
    return (mode, repo.odb.write("blob", b"".join(merged))), None


def merge_trees(repo, base, ours, theirs):
    """Three-way merge of ``{path: (mode, oid)}`` snapshots. Returns ``(merged, conflicts)``; ``conflicts`` is ``[(path, reason)]``."""
    merged, conflicts = {}, []
    for path in sorted(set(base) | set(ours) | set(theirs), key=lambda p: p.encode("utf-8", "surrogateescape")):
        result, reason = merge_entry(repo, base.get(path), ours.get(path), theirs.get(path))
        if reason:
            conflicts.append((path, reason))
        elif result is not None:
            merged[path] = result
    for path in list(merged):                                              # a path cannot be a file and a directory at once
        parts = path.split("/")
        for i in range(1, len(parts)):
            if "/".join(parts[:i]) in merged:
                conflicts.append((path, "a file and a directory with the same name were added on different sides"))
                break
    return merged, conflicts


# --- the command ----------------------------------------------------------------------------------------------------------------------------------
@dataclass
class MergeResult:
    kind: str                 # "up-to-date", "fast-forward" or "merge"
    oid: object               # the commit the current branch now points at
    old: object = None        # where it pointed before


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


def merge(repo, spec, message=None, ff="allow"):
    if ff not in ("allow", "never", "only"):
        raise GitError(f"unknown fast-forward mode '{ff}'")
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
    merged, conflicts = merge_trees(repo, co.snapshot_of_commit(repo, bases[0]), old_files, co.snapshot_of_commit(repo, target))
    if conflicts:
        raise MergeConflict([p for p, _r in conflicts])
    co.apply_switch(repo, old_files, merged)
    tree = ix.write_tree(repo.odb, merged)
    oid = repo.make_commit(tree, [head, target], message if message is not None else default_message(repo, spec, target))
    _advance(repo, oid, head, "merge %s: Merge made by the 'ort' strategy." % spec)
    return MergeResult("merge", oid, head)


def _advance(repo, oid, old, message):
    repo._advance_head(oid, old, message)
