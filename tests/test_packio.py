"""Receiving real packs (with deltas, thin ones too) and writing packs that git accepts."""
import os
import random
import subprocess

import pytest

import gitoracle as go
from kendi_git import packio
from kendi_git.repo import Repo
from test_repo_history import fresh

pytestmark = pytest.mark.skipif(go.GIT is None, reason="real git is not installed")


def history(tmp_path, name, n=25, seed=3):
    rng = random.Random(seed)
    repo = fresh(tmp_path, name)
    text = ["line %d of the file\n" % i for i in range(400)]
    for k in range(n):
        for _ in range(rng.randint(1, 6)):
            text[rng.randrange(len(text))] = "changed %d %d\n" % (k, rng.randrange(1000))
        go.write(repo, "big.txt", "".join(text))
        go.write(repo, "d/small%d.txt" % (k % 4), "small %d\n" % k)
        go.git(repo, "add", "-A")
        go.git(repo, "commit", "-qm", "commit %d" % k, extra={"GIT_COMMITTER_DATE": "%d +0000" % (1700000000 + 10 * k), "GIT_AUTHOR_DATE": "%d +0000" % (1700000000 + 10 * k)})
    return repo


def test_a_pack_with_deltas_is_stored_and_readable(tmp_path):
    src = history(tmp_path, "src")
    go.git(src, "repack", "-adf", "--depth=40", "--window=40")
    data = go.git(src, "pack-objects", "--stdout", "--revs", "--delta-base-offset", input=b"HEAD\n")
    dest = Repo.init(str(tmp_path / "dest"))
    ids = packio.receive_pack([data[:5000], data[5000:]], dest.odb, os.path.join(dest.gitdir, "objects"))
    assert set(ids) == {line.split()[0] for line in go.gits(src, "rev-list", "--objects", "HEAD").splitlines()}
    for oid in ids:
        assert dest.odb.read(oid) == (go.gits(src, "cat-file", "-t", oid).strip(), go.git(src, "cat-file", go.gits(src, "cat-file", "-t", oid).strip(), oid))
    go.git(dest.worktree, "update-ref", "refs/heads/main", go.gits(src, "rev-parse", "HEAD").strip())
    assert go.gits(dest.worktree, "fsck", "--strict", check=False) == ""


def test_ref_deltas_in_a_thin_pack_use_objects_we_already_have(tmp_path):
    src = history(tmp_path, "src")
    old, new = go.gits(src, "rev-parse", "HEAD~3").strip(), go.gits(src, "rev-parse", "HEAD").strip()
    thin = go.git(src, "pack-objects", "--stdout", "--revs", "--thin", input=("%s\n^%s\n" % (new, old)).encode())
    dest = str(tmp_path / "dest")
    go.git(str(tmp_path), "clone", "-q", "--no-local", src, dest)
    go.git(dest, "reset", "-q", "--hard", old)
    go.git(dest, "gc", "-q", "--prune=all")
    repo = Repo.discover(dest)
    ids = packio.receive_pack([thin], repo.odb, os.path.join(repo.gitdir, "objects"))
    assert new in ids
    assert repo.odb.read(new)[0] == "commit"


def test_written_packs_are_accepted_by_git(tmp_path):
    src = history(tmp_path, "src", n=8)
    ours = Repo.discover(src)
    oids = [line.split()[0] for line in go.gits(src, "rev-list", "--objects", "--all").splitlines()]
    pack = packio.write_pack([ours.odb.read(o) for o in oids])
    dest = fresh(tmp_path, "dest")
    out = subprocess.run([go.GIT, "index-pack", "--stdin", "--strict", "--fix-thin"], cwd=dest, input=pack, capture_output=True, env=go.env())
    assert out.returncode == 0, out.stderr
    assert set(go.gits(dest, "cat-file", "--batch-all-objects", "--batch-check=%(objectname)").split()) == set(oids)
