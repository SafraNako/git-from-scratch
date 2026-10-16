"""Patch output of this program against ``git diff`` / ``git show`` byte for byte, on files whose lines are all distinct (where the edit script is unique)."""
import os
import random

import pytest

import gitoracle as go
from kendi_git.repo import Repo
from twin import edit_text

pytestmark = pytest.mark.skipif(go.GIT is None, reason="real git is not installed")

NAMES = ["a.txt", "dir/b.txt", "sp ace/c d.txt", "ünï/çöd.txt", "deep/er/e.txt", "run.sh", "empty.txt", "bin.dat", "link", "f.py", "tab\tname.txt"]


def our_repo(path):
    return Repo(os.path.join(path, ".git"), path, env=go.env())


def setup(tmp_path, seed):
    rng = random.Random(seed)
    repo = str(tmp_path / f"r{seed}")
    os.makedirs(repo)
    go.git(repo, "init", "-q", "-b", "main")
    for name in NAMES:
        if name == "empty.txt":
            go.write(repo, name, b"")
        elif name == "bin.dat":
            go.write(repo, name, bytes(rng.randrange(256) for _ in range(60)) + b"\x00")
        elif name == "link":
            os.symlink("a.txt", os.path.join(repo, name))
        elif name == "f.py":
            go.write(repo, name, b"import os\n\ndef alpha():\n" + b"".join(b"    x%d = %d\n" % (i, rng.randrange(10**6)) for i in range(30)) + b"\nclass K:\n" +
                     b"".join(b"    y%d = %d\n" % (i, rng.randrange(10**6)) for i in range(30)))
        else:
            go.write(repo, name, edit_text(rng, b""))
    os.chmod(os.path.join(repo, "run.sh"), 0o755)
    go.git(repo, "add", "-A")
    go.git(repo, "commit", "-qm", "one", extra=go.commit_env(1))
    return repo, rng


def mutate_worktree(repo, rng):
    for name in NAMES:
        full = os.path.join(repo, name)
        action = rng.random()
        if name == "link":
            if action < 0.3:
                os.unlink(full)
                os.symlink(rng.choice(["b.txt", "dir"]), full)
            elif action < 0.4:
                os.unlink(full)
                go.write(repo, name, b"now a regular file\n")
            continue
        if not os.path.exists(full):
            continue
        if action < 0.15:
            os.unlink(full)
        elif action < 0.55:
            if name == "bin.dat":
                go.write(repo, name, bytes(rng.randrange(256) for _ in range(40)) + b"\x00")
            else:
                previous = open(full, "rb").read()
                go.write(repo, name, edit_text(rng, previous) if previous or rng.random() < 0.5 else b"was empty\n")
        elif action < 0.65:
            os.chmod(full, 0o755 if not os.access(full, os.X_OK) else 0o644)
    for extra in ("new file.txt", "dir/new.txt", "ünï/new.txt"):
        if rng.random() < 0.5:
            go.write(repo, extra, edit_text(rng, b""))
    if rng.random() < 0.4:
        go.write(repo, "new empty", b"")


@pytest.mark.parametrize("seed", range(12))
def test_worktree_cached_and_commit_diffs_match_git_exactly(tmp_path, seed):
    repo, rng = setup(tmp_path, seed)
    mutate_worktree(repo, rng)
    ours = our_repo(repo)
    git_diff = lambda *args: go.git(repo, "diff", "--no-color", "--no-renames", *args)  # noqa: E731
    theirs = git_diff()
    mine = ours.diff_worktree()
    assert mine == theirs, (seed, mine.decode(errors="replace")[:600], theirs.decode(errors="replace")[:600])
    assert len(theirs) > 100
    # stage a random part of it
    chosen = [n for n in NAMES + ["new file.txt", "dir/new.txt", "ünï/new.txt", "new empty"] if rng.random() < 0.5 and os.path.lexists(os.path.join(repo, n))]
    if chosen:
        go.git(repo, "add", "-A", "--", *chosen)
    assert ours.diff_cached() == git_diff("--cached")
    assert ours.diff_worktree() == git_diff()
    go.git(repo, "add", "-A")
    go.git(repo, "commit", "-qm", "two", "--allow-empty", extra=go.commit_env(2))
    head, first = go.gits(repo, "rev-parse", "HEAD").strip(), go.gits(repo, "rev-parse", "HEAD~1").strip()
    assert ours.diff_commits(first, head) == git_diff(first, head)
    assert ours.diff_commits(head, first) == git_diff(head, first)
    assert ours.diff_commits(None, first) == git_diff("4b825dc642cb6eb9a060e54bf8d69288fbee4904", first)          # the root commit against the empty tree
    for oid in (head, first):
        assert ours.show_commit(oid) == go.git(repo, "show", "--no-color", "--no-decorate", "--no-renames", oid), oid


def test_pathspec_limited_diff_matches_git(tmp_path):
    repo, rng = setup(tmp_path, 99)
    mutate_worktree(repo, rng)
    ours = our_repo(repo)
    from kendi_git.repo import Pathspec
    for spec in ["dir", "a.txt", "sp ace", "*.txt", "ünï"]:
        theirs = go.git(repo, "diff", "--no-color", "--no-renames", "--", spec)
        assert ours.diff_worktree(paths=Pathspec([spec])) == theirs, spec


def test_context_option_matches_git(tmp_path):
    repo, rng = setup(tmp_path, 5)
    mutate_worktree(repo, rng)
    ours = our_repo(repo)
    for n in (0, 1, 5):
        assert ours.diff_worktree(context=n) == go.git(repo, "diff", "--no-color", "--no-renames", f"-U{n}"), n
