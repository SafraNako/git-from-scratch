"""``diff`` while a merge has conflicts (combined diffs of the work tree files, ``* Unmerged path``) against git."""
import io
import os
import random

import pytest

import gitoracle as go
from kendi_git import cli
from test_repo_history import fresh

pytestmark = pytest.mark.skipif(go.GIT is None, reason="real git is not installed")


def kgit(repo, *argv):
    out, err = io.BytesIO(), io.BytesIO()
    code = cli.main(list(argv), stdout=out, stderr=err, cwd=repo, env=go.env())
    assert code == 0, err.getvalue()
    return out.getvalue().decode()


def lines(rng, n, prefix):
    return "".join("%s%d\n" % (prefix, rng.randrange(6)) for _ in range(n))


def make_conflict(tmp_path, name, seed):
    rng = random.Random(seed)
    repo = fresh(tmp_path, name)
    go.write(repo, "f.txt", "".join("line %d\n" % i for i in range(1, 15)))
    go.write(repo, "g.txt", "x\ny\nz\n")
    go.write(repo, "k.txt", "1\n2\n3\n")
    go.write(repo, "d/m.txt", "".join("m%d\n" % i for i in range(8)))
    go.write(repo, "bin.dat", b"a\0b\n")
    go.write(repo, "keep.txt", "keep\n")
    go.git(repo, "add", "-A")
    go.git(repo, "commit", "-qm", "base")
    go.git(repo, "branch", "-M", "main")
    go.git(repo, "checkout", "-qb", "ours")
    go.write(repo, "f.txt", "".join(("changed ours %d\n" % i if i in (3, 4, 12) else "line %d\n" % i) for i in range(1, 15)))
    go.write(repo, "g.txt", "x\nY ours\nz\n")
    go.write(repo, "k.txt", "1\n2\n3\n4 ours\n")
    go.write(repo, "d/m.txt", "".join(("ours m%d\n" % i if i in (1, 6) else "m%d\n" % i) for i in range(8)))
    go.write(repo, "bin.dat", b"a\0ours\n")
    go.write(repo, "both.txt", "ours added\nshared\n")
    go.git(repo, "add", "-A")
    go.git(repo, "commit", "-qm", "ours")
    go.git(repo, "checkout", "-q", "main")
    go.git(repo, "checkout", "-qb", "theirs")
    go.write(repo, "f.txt", "".join(("changed theirs %d\n" % i if i in (3, 5, 13) else "line %d\n" % i) for i in range(1, 15)))
    os.remove(os.path.join(repo, "g.txt"))
    go.write(repo, "k.txt", "1\n2 theirs\n3\n")
    go.write(repo, "d/m.txt", "".join(("theirs m%d\n" % i if i in (1, 4) else "m%d\n" % i) for i in range(8)))
    go.write(repo, "bin.dat", b"a\0theirs\n")
    go.write(repo, "both.txt", "theirs added\nshared\n")
    go.git(repo, "add", "-A")
    go.git(repo, "commit", "-qm", "theirs")
    go.git(repo, "checkout", "-q", "ours")
    go.git(repo, "merge", "-q", "theirs", check=False)
    return repo, rng


COMMANDS = [["diff"], ["diff", "-c"], ["diff", "--cc"], ["diff", "-U1"], ["diff", "--cached"], ["diff", "HEAD"], ["diff", "--ours"], ["diff", "--theirs"], ["diff", "--base"],
            ["diff", "--name-status"], ["diff", "--name-only"], ["diff", "--cached", "--name-status"], ["diff", "--", "f.txt"], ["diff", "--", "d"], ["diff", "HEAD", "--name-status"]]


def check(repo, label):
    for argv in COMMANDS:
        theirs = go.git(repo, *argv, check=False)
        theirs = theirs.stdout.decode() if not isinstance(theirs, bytes) else theirs.decode()
        assert kgit(repo, *argv) == theirs, (label, argv)


@pytest.mark.parametrize("seed", range(3))
def test_diff_with_conflicts_matches_git(tmp_path, seed):
    repo, rng = make_conflict(tmp_path, "c", seed)
    assert "UU" in go.gits(repo, "status", "--short")
    check(repo, "fresh conflict")
    # resolve some of the files by hand, edit others, delete one
    go.write(repo, "f.txt", "".join("line %d\n" % i if i % 3 else "resolved %d\n" % i for i in range(1, 15)))
    check(repo, "f resolved in the work tree")
    go.write(repo, "g.txt", "x\nY ours\nz\nmore\n")
    check(repo, "g edited")
    os.remove(os.path.join(repo, "both.txt"))
    check(repo, "both.txt deleted")
    go.write(repo, "d/m.txt", "m0\nours m1\nm2\nm3\ntheirs m4\nm5\nours m6\nm7\n")
    go.write(repo, "keep.txt", "keep\nand more\n")
    check(repo, "m merged by hand, keep changed")
    go.git(repo, "add", "f.txt")
    check(repo, "f staged")
