#!/usr/bin/env python3
"""Compare this program with real git on real repositories.

    python tools/real_world_sweep.py ~/Desktop/GitHub-Projeler [--limit N]

For every directory below the given one that holds a git repository with at least one commit, the sweep clones it into a temporary directory (``git clone --no-local``, so
the history arrives as a real pack with deltas) and then checks, against real git:

  objects   every object is read back and must hash to its own id (a self-authenticating check, no oracle needed), with the kind git reports
  refs      the reference list and the commit HEAD resolves to
  history   ``rev-list`` order of HEAD and the set of commits reachable from all references
  index     the clone's index parsed here equals ``git ls-files --stage``
  status    the clone is clean for both, and the *original* work tree (stat data written by real git, untracked and ignored files, whatever is in progress) gives
            the same ``status --porcelain`` for both
  fsck      the verdict (clean or not) of ``git fsck --strict``
  diffs     the last few commits' patches: byte-identical fraction, and equal added/removed counts (a patch can differ yet be equally minimal)

Nothing is written to the original repositories. The output is a table and a summary; the exit status is 1 if any hard check (everything but ``diffs``) disagreed.
"""
import argparse
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from kendi_git import diff as df  # noqa: E402
from kendi_git import fsck as fsck_mod  # noqa: E402
from kendi_git import index as ix  # noqa: E402
from kendi_git.errors import GitError  # noqa: E402
from kendi_git.objects import hash_object  # noqa: E402
from kendi_git.repo import Repo  # noqa: E402

GIT = shutil.which("git")
ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0"}


def git(cwd, *args, check=True):
    p = subprocess.run([GIT, *args], cwd=cwd, capture_output=True, env=ENV)
    if check and p.returncode != 0:
        raise RuntimeError("git %s: %s" % (" ".join(args), p.stderr.decode(errors="replace").strip()))
    return p


def out_of(cwd, *args):
    return git(cwd, *args).stdout.decode("utf-8", "surrogateescape")


SAMPLE = 0          # when positive, only this many randomly chosen objects are read back (for repositories too big to read in full)


def check_objects(path, repo):
    listing = out_of(path, "cat-file", "--batch-all-objects", "--batch-check").splitlines()
    total = len(listing)
    if SAMPLE and total > SAMPLE:
        listing = random.Random(1).sample(listing, SAMPLE)
    for line in listing:
        oid, kind, _size = line.split()
        got_kind, data = repo.odb.read(oid)
        if got_kind != kind or hash_object(kind, data) != oid:
            return False, "object %s differs (%s vs %s)" % (oid, got_kind, kind)
    return True, "%d objects" % len(listing) + (" (a sample of %d)" % total if len(listing) < total else "")


def check_refs(path, repo):
    theirs = [tuple(line.split(" ")) for line in out_of(path, "for-each-ref", "--format=%(refname) %(objectname)").splitlines()]
    if repo.refs.list("refs/") != theirs:
        return False, "reference lists differ"
    head = git(path, "rev-parse", "-q", "--verify", "HEAD", check=False).stdout.decode().strip() or None
    if repo.refs.head_oid() != head:
        return False, "HEAD differs"
    return True, "%d refs" % len(theirs)


def check_history(path, repo):
    head = repo.refs.head_oid()
    if head is None:
        return True, "unborn"
    theirs = out_of(path, "rev-list", "HEAD").split()
    if list(repo.walk([head])) != theirs:
        return False, "rev-list order of HEAD differs"
    tips = sorted({oid for _n, oid in repo.refs.list("refs/")} | {head})
    mine_all = set(repo.reachable(tips))
    their_all = set(out_of(path, "rev-list", "--all").split())
    if mine_all != their_all:
        return False, "reachable sets differ (%d vs %d)" % (len(mine_all), len(their_all))
    return True, "%d commits on HEAD, %d overall" % (len(theirs), len(their_all))


def check_index(path, repo):
    theirs = out_of(path, "ls-files", "--stage", "-z").split("\x00")
    index = repo.read_index()
    mine = ["%06o %s %d\t%s" % (e.mode, e.oid, e.stage, e.path) for e in sorted(index.entries.values(), key=ix.IndexEntry.key)] + [""]
    return mine == theirs, "%d entries" % len(index.entries)


def porcelain(path, repo=None):
    theirs = git(path, "status", "--porcelain=v1", "-z", "--no-renames", "-uall").stdout.decode("utf-8", "surrogateescape").split("\x00")
    s = repo.status()
    mine = ["%s%s %s" % (x, y, p) for x, y, p in s.entries] + ["?? " + p for p in s.untracked] + [""]
    return mine, theirs


def check_status(path, repo):
    mine, theirs = porcelain(path, repo)
    return mine == theirs, "%d lines" % (len(theirs) - 1)


def check_fsck(path, repo):
    ours = fsck_mod.fsck(repo).ok
    theirs = git(path, "fsck", "--strict", check=False).returncode == 0
    return ours == theirs, "both %s" % ("clean" if theirs else "report problems")


def check_diffs(path, repo, how_many=6, look_back=400):
    """Patches of the latest ``how_many`` single-parent commits (merges and patches over 400 KB are skipped; at most ``look_back`` commits are looked at)."""
    head = repo.refs.head_oid()
    exact = counted = total = 0
    if head is None:
        return None, "no commits"
    for oid in repo.walk([head], limit=look_back):
        if total >= how_many:
            break
        parents = repo.read_commit(oid).parents
        if len(parents) > 1:
            continue
        base = parents[0] if parents else "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
        theirs = git(path, "diff", "--no-color", "--no-renames", base, oid).stdout
        if len(theirs) > 400_000:
            continue
        changes = repo.changes_commits(parents[0] if parents else None, oid)
        mine = repo.render_changes(changes)
        total += 1
        exact += mine == theirs
        numstat = git(path, "diff", "--no-renames", "--numstat", base, oid).stdout.decode("utf-8", "surrogateescape")
        ours_counts = []
        for p, old, new in changes:
            c = df.counts(old, new)
            ours_counts.append("-\t-" if c is None else "%d\t%d" % c)
        counted += ours_counts == [line.rsplit("\t", 1)[0] for line in numstat.splitlines()] if numstat.count("\n") == len(changes) else 0
    return (exact, counted, total), "%d/%d identical, %d/%d same counts" % (exact, total, counted, total)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--skip-diffs", action="store_true")
    ap.add_argument("--sample-objects", type=int, default=0, help="read back only this many random objects per repository")
    ap.add_argument("--only", default="", help="comma-separated checks to run: objects,refs,history,index,status,fsck,worktree,diffs (default: all)")
    args = ap.parse_args()
    global SAMPLE
    SAMPLE = args.sample_objects
    only = {x for x in args.only.split(",") if x}
    wanted = lambda name: not only or name in only  # noqa: E731
    if not GIT:
        print("git is not installed: nothing to compare with")
        return 2
    root = os.path.abspath(args.root)
    repos = sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d, ".git")))
    if args.limit:
        repos = repos[:args.limit]
    hard_failures, totals, rows = [], {"exact": 0, "counted": 0, "diffs": 0, "objects": 0, "commits": 0}, []
    started = time.time()
    with tempfile.TemporaryDirectory(prefix="kgit-sweep-") as tmp:
        for name in repos:
            source = os.path.join(root, name)
            if git(source, "rev-parse", "-q", "--verify", "HEAD", check=False).returncode != 0:
                rows.append((name, "no commits", ""))
                continue
            clone = os.path.join(tmp, name)
            try:
                git(tmp, "clone", "-q", "--no-local", source, clone)
                repo = Repo(os.path.join(clone, ".git"), clone, env=ENV)
                results = []
                for label, fn in (("objects", check_objects), ("refs", check_refs), ("history", check_history), ("index", check_index), ("status", check_status), ("fsck", check_fsck)):
                    if not wanted(label):
                        continue
                    ok, detail = fn(clone, repo)
                    results.append((label, ok, detail))
                    if not ok:
                        hard_failures.append((name, label, detail))
                if wanted("worktree"):
                    original = Repo(os.path.join(source, ".git"), source, env=ENV)
                    mine, theirs = porcelain(source, original)
                    results.append(("worktree", mine == theirs, "%d changed/untracked" % (len(theirs) - 1)))
                    if mine != theirs:
                        hard_failures.append((name, "worktree", "status of the original work tree differs: %s" % sorted(set(mine) ^ set(theirs))[:4]))
                if not args.skip_diffs and wanted("diffs"):
                    value, detail = check_diffs(clone, repo)
                    if value:
                        totals["exact"] += value[0]
                        totals["counted"] += value[1]
                        totals["diffs"] += value[2]
                    results.append(("diffs", True, detail))
                for r in results:
                    if r[0] == "objects" and r[1]:
                        totals["objects"] += int(r[2].split()[0])
                    if r[0] == "history" and r[2][0].isdigit():
                        totals["commits"] += int(r[2].split()[0])
                rows.append((name, "ok" if all(r[1] for r in results) else "DIFFERS", "; ".join("%s: %s" % (r[0], r[2]) for r in results)))
            except (GitError, RuntimeError, OSError, ValueError) as exc:
                hard_failures.append((name, "error", "%s: %s" % (type(exc).__name__, exc)))
                rows.append((name, "ERROR", "%s: %s" % (type(exc).__name__, exc)))
            finally:
                shutil.rmtree(clone, ignore_errors=True)
    for name, verdict, detail in rows:
        print("%-34s %-8s %s" % (name, verdict, detail))
    print()
    print("%d repositories, %d objects verified, %d commits walked, %.0f s" % (len([r for r in rows if r[1] in ("ok", "DIFFERS")]), totals["objects"], totals["commits"], time.time() - started))
    if totals["diffs"]:
        print("patches of recent commits: %d/%d byte-identical to git diff, %d/%d with the same added/removed counts" % (totals["exact"], totals["diffs"], totals["counted"], totals["diffs"]))
    print("hard disagreements: %d" % len(hard_failures))
    for item in hard_failures:
        print("  ", item)
    return 1 if hard_failures else 0


if __name__ == "__main__":
    sys.exit(main())
