"""Moving between trees: ``checkout``, ``reset`` and restoring files.

All of it is one operation, the two-way tree switch of ``git read-tree -m -u``. Given the tree the work tree is based on (``old``) and the tree to move to (``new``),
every path is decided from its entry in each tree, its index entry and its work tree file, following the table of git's ``twoway_merge``:

* the path is the same in ``old`` and ``new``, or the index already has the ``new`` version: nothing happens and local changes are kept;
* the index still equals ``old`` and the file is unmodified: the path moves to ``new`` (is written, or removed when ``new`` lacks it);
* any other combination means local changes in the way: the switch is refused, naming the files.

Untracked files that a written path would overwrite stop the switch too, except ignored ones, which git (and this program) sacrifice. The plan is made completely before
anything is touched, so a refusal leaves the repository exactly as it was.
"""
from __future__ import annotations

import os
import shutil
import stat as statmod
from dataclasses import dataclass

from . import index as ix
from .errors import GitError
from .objects import MODE_EXEC, MODE_GITLINK, MODE_LINK


def _umask():
    old = os.umask(0)
    os.umask(old)
    return old


def write_worktree_file(repo, rel, mode, data):
    """Create (replacing whatever is there) a file or symlink the way git checks one out."""
    full = repo._full(rel)
    parts = rel.split("/")
    for i in range(1, len(parts)):                                    # a file where a directory is needed makes room (the plan has checked that this is allowed)
        parent = repo._full("/".join(parts[:i]))
        if os.path.lexists(parent) and (os.path.islink(parent) or not os.path.isdir(parent)):
            os.unlink(parent)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    if os.path.isdir(full) and not os.path.islink(full):
        if mode == MODE_GITLINK:
            return
        shutil.rmtree(full)
    elif os.path.lexists(full):
        os.unlink(full)
    if mode == MODE_LINK:
        os.symlink(os.fsdecode(data), full)
    elif mode == MODE_GITLINK:
        os.makedirs(full, exist_ok=True)
    else:
        with open(full, "wb") as f:
            f.write(data)
        os.chmod(full, (0o777 if mode == MODE_EXEC else 0o666) & ~_umask())


def _untracked_below(repo, rel, going_away, rules):
    """Non-ignored files below directory ``rel`` that are not about to be removed: they would be lost if a file took the directory's place."""
    found = []
    for root, dirs, files in os.walk(repo._full(rel)):
        for name in files + [d for d in dirs if os.path.islink(os.path.join(root, d))]:
            path = os.path.relpath(os.path.join(root, name), repo.worktree).replace(os.sep, "/")
            if path not in going_away and not rules.is_ignored(path, False):
                found.append(path)
    return found


def plan_switch(repo, old, new, index, force=False):
    """What moving from snapshot ``old`` to ``new`` does: ``(removals, writes, conflicts, blocked)`` where ``conflicts`` are local changes that stand in the way and
    ``blocked`` are untracked files that would be overwritten or removed."""
    rules = repo.ignore_rules()
    entries = {p: e for (p, s), e in index.entries.items() if s == 0}
    unmerged = sorted({p for p, s in index.entries if s})
    if unmerged and not force:
        raise GitError("you need to resolve your current index first: " + ", ".join(unmerged))
    removals, writes, conflicts, blocked = [], [], [], []

    def dirty(path, e):
        st = repo._lstat(path)
        return st is not None and not statmod.S_ISDIR(st.st_mode) and not repo._entry_unchanged(e, path, st, index)

    paths = sorted(set(old) | set(new) | set(entries), key=lambda p: p.encode("utf-8", "surrogateescape"))
    if force:
        for path in sorted(unmerged):                                     # a path that only exists as unmerged stages goes away if the target lacks it
            if path not in new and path not in entries:
                removals.append(path)
        for path in paths:
            n, e = new.get(path), entries.get(path)
            cur = (e.mode, e.oid) if e else None
            if n is None:
                if e is not None:
                    removals.append(path)
            elif cur != n or e is None or dirty_or_missing(repo, path, e, index):
                writes.append((path, n))
        gone = set(removals)
        for path, _target in writes:                                       # even a hard reset does not delete untracked files a directory of the same name holds
            st = repo._lstat(path)
            if st is not None and statmod.S_ISDIR(st.st_mode) and path not in entries:
                blocked += _untracked_below(repo, path, gone, rules)
        return removals, writes, [], sorted(set(blocked))
    for path in paths:
        o, n, e = old.get(path), new.get(path), entries.get(path)
        cur = (e.mode, e.oid) if e else None
        if e is not None:
            if (o is None and n is None) or (o is None and n is not None and cur == n) or (o is not None and n is not None and (o == n or cur == n)):
                continue
            if o is not None and cur == o and (n is None or n != cur):
                if dirty(path, e):
                    conflicts.append(path)
                elif n is None:
                    removals.append(path)
                else:
                    writes.append((path, n))
                continue
            conflicts.append(path)
        elif n is not None:
            if o is not None:
                if o != n:
                    conflicts.append(path)                                 # the path was removed from the index (staged) but differs between the trees
                continue
            writes.append((path, n))
        elif o is not None:
            st = repo._lstat(path)                                         # removed from the index, absent in the target: an untracked file there is not ours to touch
            if st is not None and not statmod.S_ISDIR(st.st_mode) and not rules.is_ignored(path, False):
                blocked.append(path)
    removal_set = set(removals)
    for path, _target in writes:
        if path in entries:
            continue                                                       # a tracked path being updated: its worktree state was judged above
        st = repo._lstat(path)
        if st is not None:
            if statmod.S_ISDIR(st.st_mode):
                blocked += _untracked_below(repo, path, removal_set, rules)
            elif not rules.is_ignored(path, False):
                blocked.append(path)
            continue
        parts = path.split("/")
        for i in range(1, len(parts)):                                     # a *parent* that is a file must be a tracked file going away (or ignored)
            parent = "/".join(parts[:i])
            pst = repo._lstat(parent)
            if pst is None:
                break
            if not statmod.S_ISDIR(pst.st_mode):
                if parent in entries:
                    if parent not in removal_set and parent not in new:
                        removals.append(parent)
                        removal_set.add(parent)
                elif not rules.is_ignored(parent, False):
                    blocked.append(parent)
                break
    return removals, writes, conflicts, sorted(set(blocked))


def dirty_or_missing(repo, path, e, index):
    st = repo._lstat(path)
    return st is None or statmod.S_ISDIR(st.st_mode) or not repo._entry_unchanged(e, path, st, index)


def apply_switch(repo, old, new, force=False):
    """Move the index and work tree from ``old`` to ``new`` (``{path: (mode, oid)}`` snapshots); raises ``GitError`` before changing anything if that is unsafe."""
    index = repo.read_index()
    removals, writes, conflicts, blocked = plan_switch(repo, old, new, index, force)
    if conflicts:
        raise GitError("your local changes to the following files would be overwritten: " + ", ".join(conflicts) +
                       " (commit or stash them first, or use reset --hard to throw them away)")
    if blocked:
        raise GitError("untracked working tree files would be overwritten or removed: " + ", ".join(blocked) + " (move or remove them first)")
    for path in removals:
        full = repo._full(path)
        try:
            if os.path.lexists(full) and not os.path.isdir(full):
                os.unlink(full)
        except OSError as exc:
            raise GitError(f"cannot remove '{path}': {exc}")
        index.remove(path)
    for path in sorted(removals, key=lambda p: -p.count("/")):
        repo._prune_empty_dirs(os.path.dirname(path))
    for path, (mode, oid) in writes:
        data = b"" if mode == MODE_GITLINK else repo.odb.read(oid)[1]
        write_worktree_file(repo, path, mode, data)
        fields = ix.stat_fields(os.lstat(repo._full(path)))
        index.set(ix.IndexEntry(path, mode, oid, **fields))
    if force:
        for key in [k for k in index.entries if k[1]]:
            del index.entries[key]
    repo.write_index(index)
    return [p for p, _ in writes], removals


def snapshot_of_commit(repo, oid):
    return repo.tree_files(repo.read_commit(oid).tree) if oid else {}


@dataclass
class Switched:
    """What a checkout did: ``kind`` is ``already``, ``branch``, ``new-branch`` or ``detached``."""
    kind: str
    name: str = ""
    oid: str = ""
    subject: str = ""


def checkout(repo, target, create=None, detach=False):
    """Switch to a branch (or detach at a commit); with ``create`` also create that branch first. Returns a ``Switched`` describing the result."""
    head_oid = repo.refs.head_oid()
    old = snapshot_of_commit(repo, head_oid)
    branch_ref = None
    if create:
        from .refs import check_branch_name
        if not check_branch_name(create):
            raise GitError(f"'{create}' is not a valid branch name")
        if repo.refs.exists("refs/heads/" + create):
            raise GitError(f"a branch named '{create}' already exists")
        start = repo.commit_of(target) if target else head_oid
        if start is None:
            raise GitError("cannot create a branch from HEAD: the current branch has no commits yet")
        branch_ref, new_oid = "refs/heads/" + create, start
    elif target is not None and not detach and repo.refs.exists("refs/heads/" + target):
        branch_ref = "refs/heads/" + target
        new_oid = repo.refs.read(branch_ref)
        if repo.refs.head() == ("ref", branch_ref):
            entry = repo.log_entry("checkout: moving from %s to %s" % (target, target))        # git records even a switch to the branch it is already on
            if entry:
                repo.refs.append_log("HEAD", head_oid, head_oid, *entry)
            return Switched("already", target, new_oid)
    elif target is None:
        raise GitError("nothing to check out")
    else:
        new_oid = repo.commit_of(target)
    apply_switch(repo, old, snapshot_of_commit(repo, new_oid))
    kind, value = repo.refs.head()
    came_from = value[len("refs/heads/"):] if kind == "ref" and value.startswith("refs/heads/") else (value if kind == "detached" else repo.refs.current_branch() or "HEAD")
    if create:
        repo.refs.update(branch_ref, new_oid, log=repo.log_entry("branch: Created from %s" % (target or "HEAD")))
    if branch_ref:
        repo.refs.set_symbolic("HEAD", branch_ref)
        result = Switched("new-branch" if create else "branch", branch_ref[len("refs/heads/"):], new_oid, repo.read_commit(new_oid).subject)
    else:
        repo.refs._write_locked("HEAD", new_oid)
        result = Switched("detached", "", new_oid, repo.read_commit(new_oid).subject)
    entry = repo.log_entry("checkout: moving from %s to %s" % (came_from, create or (result.name if result.kind == "branch" else target)))
    if entry:
        repo.refs.append_log("HEAD", head_oid, new_oid, *entry)
    return result


def reset(repo, target="HEAD", mode="mixed"):
    """``soft`` moves the branch only, ``mixed`` also resets the index, ``hard`` the work tree as well. Returns ``(new commit id, its subject)``."""
    if mode not in ("soft", "mixed", "hard"):
        raise GitError(f"unknown reset mode '{mode}'")
    from .merge import clear_merge_state, merge_in_progress
    if mode == "soft" and merge_in_progress(repo):
        raise GitError("cannot do a soft reset in the middle of a merge")
    new_oid = repo.commit_of(target)
    head_oid = repo.refs.head_oid()
    new_files = snapshot_of_commit(repo, new_oid)
    if mode == "hard":
        apply_switch(repo, snapshot_of_commit(repo, head_oid), new_files, force=True)
    elif mode == "mixed":
        index = repo.read_index()
        fresh = ix.Index()
        for path, (mode_, oid) in new_files.items():
            kept = index.entries.get((path, 0))
            fresh.entries[(path, 0)] = kept if kept is not None and (kept.mode, kept.oid) == (mode_, oid) else ix.IndexEntry(path, mode_, oid)   # keep stat data
        repo.write_index(fresh)
    if head_oid:
        repo.refs._write_locked("ORIG_HEAD", head_oid)
    repo._advance_head(new_oid, head_oid, "reset: moving to %s" % target)
    clear_merge_state(repo)                                  # a hard or mixed reset ends a merge in progress
    return new_oid, repo.read_commit(new_oid).subject


def restore(repo, specs, prefix="", source=None):
    """``checkout [<rev>] -- <paths>``: copy the paths from ``source`` (a revision; the index when ``None``) into the work tree, and into the index as well when a revision
    is given. Returns the restored paths."""
    from .repo import Pathspec
    index = repo.read_index()
    ps = Pathspec(specs, prefix)
    if source is None:
        origin = {e.path: (e.mode, e.oid) for e in index.stage_zero()}
    else:
        origin = repo.tree_files(repo.peel(repo.rev_parse(source), "tree"))
    done = []
    for path in sorted(origin, key=lambda p: p.encode("utf-8", "surrogateescape")):
        if not ps.match(path):
            continue
        mode, oid = origin[path]
        write_worktree_file(repo, path, mode, b"" if mode == MODE_GITLINK else repo.odb.read(oid)[1])
        index.set(ix.IndexEntry(path, mode, oid, **ix.stat_fields(os.lstat(repo._full(path)))))
        done.append(path)
    missing = ps.unmatched()
    if missing:
        raise GitError(f"pathspec '{missing[0]}' did not match any file(s) known to git")
    repo.write_index(index)
    return done


def restore_stage(repo, specs, prefix, stage):
    """``checkout --ours/--theirs <paths>``: write the version of an unmerged path from stage 2 (ours) or 3 (theirs) to the work tree, leaving the index conflicted."""
    from .repo import Pathspec
    index = repo.read_index()
    ps = Pathspec(specs, prefix)
    done = []
    for (path, st), entry in sorted(index.entries.items(), key=lambda kv: kv[0][0].encode("utf-8", "surrogateescape")):
        if st == stage and ps.match(path):
            write_worktree_file(repo, path, entry.mode, repo.odb.read(entry.oid)[1])
            done.append(path)
    if not done or ps.unmatched():
        raise GitError("path '%s' does not have the %s version" % ((ps.unmatched() or specs)[0], "our" if stage == 2 else "their"))
    return done
