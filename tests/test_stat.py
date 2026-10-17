"""``--stat``, ``--shortstat`` and ``--summary`` against git, byte for byte."""
import io
import os
import random

import pytest

import gitoracle as go
from kendi_git import cli
from test_repo_diff import mutate_worktree, setup

pytestmark = pytest.mark.skipif(go.GIT is None, reason="real git is not installed")


def kgit(repo, *argv):
    out, err = io.BytesIO(), io.BytesIO()
    code = cli.main(list(argv), stdout=out, stderr=err, cwd=repo, env=go.env())
    assert code == 0, err.getvalue()
    return out.getvalue()


@pytest.mark.parametrize("seed", range(10))
def test_stat_forms_match_git_on_random_changes(tmp_path, seed):
    repo, rng = setup(tmp_path, seed)
    mutate_worktree(repo, rng)
    for flags in (["--stat"], ["--shortstat"], ["--summary"], ["--stat", "--summary"]):
        assert kgit(repo, "diff", *flags) == go.git(repo, "diff", "--no-renames", *flags), flags
    go.git(repo, "add", "-A")
    for flags in (["--stat"], ["--shortstat"], ["--summary"]):
        assert kgit(repo, "diff", "--cached", *flags) == go.git(repo, "diff", "--cached", "--no-renames", *flags), flags
    go.git(repo, "commit", "-qm", "two", extra=go.commit_env(2))
    assert kgit(repo, "diff", "HEAD~1", "HEAD", "--stat") == go.git(repo, "diff", "--no-renames", "--stat", "HEAD~1", "HEAD")


def test_wide_graphs_and_long_names_are_scaled_like_git(tmp_path):
    repo = str(tmp_path / "w")
    os.makedirs(repo)
    go.git(repo, "init", "-q", "-b", "main")
    rng = random.Random(3)
    names = ["short.txt", "a/much/longer/path/to/a/file/with/a/name/that/does/not/fit/in/the/name/column/at/all.txt", "dir/" + "x" * 70 + ".py", "mid/dle.txt"]
    for name in names:
        go.write(repo, name, "".join("line %d\n" % i for i in range(rng.randint(5, 400))))
    go.git(repo, "add", "-A")
    go.git(repo, "commit", "-qm", "base", extra=go.commit_env(1))
    for name in names:
        text = open(os.path.join(repo, name)).read().splitlines(True)
        go.write(repo, name, "".join(text[:rng.randint(0, len(text))] + ["new %d\n" % i for i in range(rng.randint(0, 300))]))
    assert kgit(repo, "diff", "--stat") == go.git(repo, "diff", "--stat", "--no-renames")
    assert b"..." in kgit(repo, "diff", "--stat")                                         # the long names really were truncated


def test_log_and_show_stat_match_git(history_repo):
    for ours, theirs in [(["log", "--stat"], ["log", "--no-color", "--no-decorate", "--stat", "--no-renames"]),
                         (["log", "--oneline", "--stat", "-n", "5"], ["log", "--oneline", "--no-decorate", "--stat", "--no-renames", "-n", "5"])]:
        # merge commits show no stat in git log, the same here
        assert kgit(history_repo, *ours) == go.git(history_repo, *theirs), ours
    tip = go.gits(history_repo, "rev-list", "--no-merges", "-n", "1", "HEAD").strip()
    assert kgit(history_repo, "show", "--stat", tip) == go.git(history_repo, "show", "--no-color", "--no-decorate", "--no-renames", "--stat", "--format=medium", tip)
