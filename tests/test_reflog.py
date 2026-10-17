"""Reflogs: written by this program exactly as git writes them (checked in the lock-step sessions of test_twin), and read from logs git wrote."""
import io
import os
import shutil

import pytest

import gitoracle as go
from kendi_git import cli
from kendi_git.errors import GitError
from kendi_git.repo import Repo

pytestmark = pytest.mark.skipif(go.GIT is None, reason="real git is not installed")


@pytest.fixture
def repo_with_log(history_repo, tmp_path):
    path = str(tmp_path / "copy")
    shutil.copytree(history_repo, path, symlinks=True)
    # give the git-written log some variety: switches, a reset, a new branch
    go.git(path, "checkout", "-q", "-b", "scratch", "HEAD~2")
    go.git(path, "checkout", "-q", "main")
    go.git(path, "checkout", "-q", "--detach", "HEAD~1")
    go.git(path, "checkout", "-q", "main")
    go.git(path, "checkout", "-q", "scratch")
    go.git(path, "reset", "-q", "--hard", "main~1")
    return path


def test_reflog_selectors_agree_with_git(repo_with_log):
    ours = Repo(os.path.join(repo_with_log, ".git"), repo_with_log, env=go.env())
    specs = ["HEAD@{0}", "HEAD@{1}", "HEAD@{3}", "@{0}", "@{1}", "@{2}", "scratch@{0}", "scratch@{1}", "main@{0}", "HEAD@{2}~1", "HEAD@{1}^{tree}", "@{-1}", "@{-2}", "@{-3}", "@{-1}~1",
             "HEAD@{999}", "nosuch@{0}", "@{-99}", "HEAD@{yesterday}"]
    for spec in specs:
        proc = go.git(repo_with_log, "rev-parse", "--verify", "-q", spec, check=False)
        try:
            mine = ours.rev_parse(spec)
        except GitError:
            mine = None
        expected = proc.stdout.decode().strip() or None
        if spec == "HEAD@{yesterday}":
            assert mine is None                      # date selectors are not supported (git answers from the clock)
            continue
        assert mine == expected, spec


def test_reflog_command_matches_git(repo_with_log):
    for ours, theirs in [(["reflog"], ["reflog"]), (["reflog", "scratch"], ["reflog", "show", "scratch"]), (["reflog", "-n", "3"], ["reflog", "-n", "3"]), (["reflog", "main"], ["reflog", "show", "main"])]:
        out = io.BytesIO()
        assert cli.main(ours, stdout=out, stderr=io.BytesIO(), cwd=repo_with_log, env=go.env()) == 0
        assert out.getvalue() == go.git(repo_with_log, *theirs), ours


def test_log_is_not_written_for_tags_or_when_disabled(tmp_path):
    repo = str(tmp_path / "r")
    os.makedirs(repo)
    env = {**go.env(), "GIT_AUTHOR_DATE": "1700000000 +0000", "GIT_COMMITTER_DATE": "1700000000 +0000"}
    ours = Repo.init(repo, env=env)
    go.write(repo, "f", "x")
    ours.add(["f"])
    ours.commit("one")
    ours.create_tag("t")
    assert not os.path.exists(os.path.join(repo, ".git", "logs", "refs", "tags"))
    assert os.path.exists(os.path.join(repo, ".git", "logs", "HEAD")) and os.path.exists(os.path.join(repo, ".git", "logs", "refs", "heads", "main"))
    go.git(repo, "config", "core.logAllRefUpdates", "false")
    ours2 = Repo(os.path.join(repo, ".git"), repo, env=env)
    go.write(repo, "g", "y")
    ours2.add(["g"])
    ours2.commit("two")
    # the existing logs are still appended to (git only skips creating new ones), nothing new appears
    assert len(ours2.refs.read_log("refs/heads/main")) == 2
