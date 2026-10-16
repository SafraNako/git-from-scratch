import os
import shutil

import pytest

import gitoracle as go
from kendi_git.errors import GitError
from kendi_git.repo import Repo

pytestmark = pytest.mark.skipif(go.GIT is None, reason="real git is not installed")

MESSAGES = ["release 1.0", "release\n\nwith a body\n", "  leading blank\n\n\n\ntrailing   \n\n", "line one\n# a comment-looking line\nline three", "tabs\tin\tit\n\n   indented\n",
            "ünï ✓ 日本語\n\nbody", "x"]


def pair(tmp_path, history_repo):
    """Two identical copies of the fixture repository: one for real git, one for this program."""
    a, b = str(tmp_path / "git_copy"), str(tmp_path / "our_copy")
    shutil.copytree(history_repo, a, symlinks=True)
    shutil.copytree(history_repo, b, symlinks=True)
    env = {**go.env(), "GIT_COMMITTER_DATE": "1800000000 +0100", "GIT_AUTHOR_DATE": "1800000000 +0100"}
    return a, Repo(os.path.join(b, ".git"), b, env=env, now=lambda: 1800000000), b


def test_annotated_tags_are_byte_identical_to_gits(tmp_path, history_repo):
    git_dir, ours, our_dir = pair(tmp_path, history_repo)
    env = {"GIT_COMMITTER_DATE": "1800000000 +0100", "GIT_AUTHOR_DATE": "1800000000 +0100"}
    targets = ["HEAD", "HEAD~2", "v1", "main^{tree}", "HEAD:" + go.gits(history_repo, "ls-tree", "--name-only", "HEAD").split()[0]]
    for n, message in enumerate(MESSAGES):
        target = targets[n % len(targets)]
        go.git(git_dir, "tag", "-a", "-m", message, f"t{n}", target, extra=env)
        ours.create_tag(f"t{n}", target, message)
        theirs_oid = go.gits(git_dir, "rev-parse", f"refs/tags/t{n}").strip()
        assert ours.refs.read(f"refs/tags/t{n}") == theirs_oid, (n, message)
        assert go.git(our_dir, "cat-file", "tag", theirs_oid) == go.git(git_dir, "cat-file", "tag", theirs_oid)
    go.git(our_dir, "fsck", "--strict")


def test_lightweight_tags_and_errors(tmp_path, history_repo):
    git_dir, ours, our_dir = pair(tmp_path, history_repo)
    ours.create_tag("lw", "HEAD~1")
    go.git(git_dir, "tag", "lw", "HEAD~1")
    assert ours.refs.read("refs/tags/lw") == go.gits(git_dir, "rev-parse", "lw").strip()
    with pytest.raises(GitError):
        ours.create_tag("lw")                                    # exists
    ours.create_tag("lw", "HEAD", force=True)
    assert ours.refs.read("refs/tags/lw") == ours.refs.head_oid()
    for bad in ["bad name", "a..b", "x.lock", "-lead", "~x"]:
        with pytest.raises(GitError):
            ours.create_tag(bad)
        assert go.git(git_dir, "tag", "--", bad, check=False).returncode != 0
    with pytest.raises(GitError):
        ours.create_tag("empty-message", message="   \n\n")
    ours.delete_tag("lw")
    with pytest.raises(GitError):
        ours.delete_tag("lw")
    assert [n for n, _ in ours.tags()] == go.gits(our_dir, "tag", "--list").split()


def test_branch_rename_delete_and_listing_follow_git(tmp_path, history_repo):
    git_dir, ours, our_dir = pair(tmp_path, history_repo)
    head = go.gits(git_dir, "rev-parse", "HEAD").strip()
    for name in ["topic", "other/deep"]:
        ours.create_branch(name)
        go.git(git_dir, "branch", name)
    ours.rename_branch("topic", "renamed")
    go.git(git_dir, "branch", "-m", "topic", "renamed")
    assert ours.branches() == [(n.strip(" *"), go.gits(git_dir, "rev-parse", n.strip(" *")).strip()) for n in go.gits(git_dir, "branch", "--list", "--format=%(refname:short)").split()]
    current = ours.refs.current_branch()
    ours.rename_branch(current, "primary")
    go.git(git_dir, "branch", "-m", current, "primary")
    assert ours.refs.current_branch() == go.gits(git_dir, "symbolic-ref", "--short", "HEAD").strip() == "primary"
    with pytest.raises(GitError):
        ours.delete_branch("primary")                            # checked out
    with pytest.raises(GitError):
        ours.rename_branch("renamed", "primary")                 # target exists
    with pytest.raises(GitError):
        ours.delete_branch("nope")
    ours.delete_branch("renamed")
    assert ours.refs.read("refs/heads/primary") == head
    go.git(our_dir, "fsck", "--strict")


def test_branch_delete_refuses_unmerged_unless_forced(tmp_path):
    repo = str(tmp_path / "r")
    os.makedirs(repo)
    go.git(repo, "init", "-q", "-b", "main")
    go.write(repo, "f", "1\n")
    go.git(repo, "add", "f")
    go.git(repo, "commit", "-qm", "base", extra=go.commit_env(1))
    go.git(repo, "checkout", "-qb", "work")
    go.write(repo, "f", "2\n")
    go.git(repo, "commit", "-qam", "work", extra=go.commit_env(2))
    go.git(repo, "checkout", "-q", "main")
    ours = Repo(os.path.join(repo, ".git"), repo, env=go.env())
    assert go.git(repo, "branch", "-d", "work", check=False).returncode != 0
    with pytest.raises(GitError):
        ours.delete_branch("work")
    ours.delete_branch("work", force=True)
    assert go.gits(repo, "branch", "--list", "work").strip() == ""
