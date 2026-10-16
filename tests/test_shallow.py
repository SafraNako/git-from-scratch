"""Shallow clones (``git clone --depth``): the boundary commits have no parents as far as git is concerned, although their parent objects are absent."""
import os

import pytest

import gitoracle as go
from kendi_git import merge as mg
from kendi_git.fsck import fsck
from kendi_git.repo import Repo

pytestmark = pytest.mark.skipif(go.GIT is None, reason="real git is not installed")


@pytest.fixture
def shallow(history_repo, tmp_path):
    dest = str(tmp_path / "shallow")
    go.git(str(tmp_path), "clone", "-q", "--depth", "3", "--no-local", "file://" + history_repo, dest)
    assert os.path.exists(os.path.join(dest, ".git", "shallow"))
    return dest, Repo(os.path.join(dest, ".git"), dest, env=go.env())


def test_history_of_a_shallow_clone_matches_git(shallow):
    dest, repo = shallow
    assert list(repo.walk([repo.refs.head_oid()])) == go.gits(dest, "rev-list", "HEAD").split()
    tips = sorted({oid for _n, oid in repo.refs.list("refs/")} | {repo.refs.head_oid()})
    assert repo.reachable(tips) == set(go.gits(dest, "rev-list", "--all").split())
    boundary = sorted(repo.shallow)
    assert boundary and all(repo.read_commit(oid).parents == [] for oid in boundary)
    oids = go.gits(dest, "rev-list", "HEAD").split()
    for a in oids[:4]:
        for b in oids[:4]:
            assert mg.merge_bases(repo, a, b) == sorted(go.gits(dest, "merge-base", "--all", a, b, check=False).split()), (a, b)


def test_log_show_status_and_fsck_of_a_shallow_clone_match_git(shallow):
    dest, repo = shallow
    from kendi_git import cli
    import io
    out = io.BytesIO()
    assert cli.main(["log"], stdout=out, stderr=io.BytesIO(), cwd=dest, env=go.env()) == 0
    assert out.getvalue() == go.git(dest, "log", "--no-color", "--no-decorate")
    boundary = sorted(repo.shallow)[0]
    assert repo.show_commit(boundary) == go.git(dest, "show", "--no-color", "--no-decorate", "--no-renames", boundary)       # a boundary commit shows as a root commit
    s = repo.status()
    assert ["%s%s %s" % (x, y, p) for x, y, p in s.entries] + ["?? " + p for p in s.untracked] == [ln for ln in go.gits(dest, "status", "--porcelain", "--no-renames", "-uall").splitlines()]
    report = fsck(repo)
    assert report.ok, report.errors
    assert go.git(dest, "fsck", "--strict", check=False).returncode == 0
