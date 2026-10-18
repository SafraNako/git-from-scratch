"""clone, fetch, pull and push between local repositories, compared step by step with git."""
import os

import pytest

import gitoracle as go
from rtwin import RTwin, commit_file

pytestmark = pytest.mark.skipif(go.GIT is None, reason="real git is not installed")

TRANSPORTS = ["local", "file"]


@pytest.fixture(params=TRANSPORTS)
def rt(request, tmp_path):
    t = RTwin(tmp_path, request.param)
    yield t
    t.close()


def test_clone_matches_git(rt):
    g, k = rt.clone()
    assert g[0] == 0 and k[0] == 0, (g, k)
    rt.assert_same("after clone")


def test_bare_clone_and_branch_option(rt):
    g, k = rt.clone("-b", "dev")
    assert g[0] == 0 and k[0] == 0, (g, k)
    rt.assert_same("after clone -b dev")


def test_fetch_new_commits_branches_and_tags(rt):
    rt.clone()

    def more(d):
        commit_file(d, "a.txt", "one\ntwo\nthree\nfour\n", "third", 1700000100)
        go.git(d, "tag", "light")
        go.git(d, "tag", "-a", "v2", "-m", "version two", extra={"GIT_COMMITTER_DATE": "1700000101 +0000"})
        go.git(d, "checkout", "-q", "-b", "feature")
        commit_file(d, "f.txt", "feature\n", "feature work", 1700000110)
        go.git(d, "checkout", "-q", "main")
    rt.feed(more)
    g, k = rt.both("fetch")
    assert g[0] == k[0] == 0, (g, k)
    assert rt.norm(g[2], "g") == rt.norm(k[2], "k")
    rt.assert_same("after fetch")
    g, k = rt.both("fetch")                                           # nothing new
    assert g[0] == k[0] == 0 and rt.norm(g[2], "g") == rt.norm(k[2], "k")
    rt.assert_same("after the second fetch")


def test_fetch_named_remote_and_refspec(rt):
    rt.clone()
    rt.feed(lambda d: commit_file(d, "x.txt", "x\n", "x", 1700000100))
    g, k = rt.both("fetch", "origin")
    assert g[0] == k[0] == 0 and rt.norm(g[2], "g") == rt.norm(k[2], "k")
    rt.assert_same("fetch origin")
    rt.feed(lambda d: commit_file(d, "y.txt", "y\n", "y", 1700000200))
    g, k = rt.both("fetch", "origin", "main:refs/remotes/origin/copy")
    assert g[0] == k[0], (g, k)
    rt.assert_same("fetch with a refspec")


def test_fetch_prune_and_forced_updates(rt):
    rt.clone()

    def rewrite(d):
        go.git(d, "branch", "-D", "dev")
        go.git(d, "checkout", "-q", "-b", "other")
        commit_file(d, "o.txt", "o\n", "o", 1700000100)
        go.git(d, "checkout", "-q", "main")
        go.git(d, "reset", "-q", "--hard", "HEAD~1")
        commit_file(d, "z.txt", "z\n", "z", 1700000120)
    rt.feed(rewrite)
    g, k = rt.both("fetch", "--prune")
    assert g[0] == k[0] == 0, (g, k)
    assert rt.norm(g[2], "g") == rt.norm(k[2], "k"), (rt.norm(g[2], "g"), rt.norm(k[2], "k"))
    rt.assert_same("after fetch --prune with a rewritten main")


def test_pull_fast_forward_and_merge(rt):
    rt.clone()
    rt.feed(lambda d: commit_file(d, "a.txt", "one\ntwo\nthree\nfour\n", "ahead", 1700000100))
    g, k = rt.both("pull")
    assert g[0] == k[0] == 0, (g, k)
    rt.assert_same("pull fast-forward")
    # diverge: a local commit and an upstream commit
    for who, run in (("g", rt.git), ("k", rt.kgit)):
        pass
    rt.step += 1
    for d in (rt.dir["g"], rt.dir["k"]):
        go.write(d, "local.txt", "local\n")
        go.git(d, "add", "-A")
        go.git(d, "commit", "-qm", "local work", extra={"GIT_AUTHOR_DATE": "1700000150 +0000", "GIT_COMMITTER_DATE": "1700000150 +0000"})
    rt.feed(lambda d: commit_file(d, "up.txt", "up\n", "upstream work", 1700000200))
    g, k = rt.both("pull", "--no-ff")
    assert g[0] == k[0] == 0, (g, k)
    rt.assert_same("pull with a merge", mask_head=True)
    assert go.gits(rt.dir["g"], "show", "-s", "--format=%T %P %B").replace("up_g", "UP") == go.gits(rt.dir["k"], "show", "-s", "--format=%T %P %B").replace("up_k", "UP")


def test_push_new_branch_fast_forward_force_and_delete(rt):
    rt.clone()
    rt.step += 1
    for d in (rt.dir["g"], rt.dir["k"]):
        go.write(d, "p.txt", "pushed\n")
        go.git(d, "add", "-A")
        go.git(d, "commit", "-qm", "to push", extra={"GIT_AUTHOR_DATE": "1700000300 +0000", "GIT_COMMITTER_DATE": "1700000300 +0000"})
    g, k = rt.both("push", "origin", "main")
    assert g[0] == k[0] == 0, (g, k)
    assert rt.norm(g[2], "g") == rt.norm(k[2], "k"), (g[2], k[2])
    rt.assert_same("after the first push", upstream=True)
    for d in (rt.dir["g"], rt.dir["k"]):
        go.git(d, "branch", "topic")
    g, k = rt.both("push", "-u", "origin", "topic")
    assert g[0] == k[0] == 0, (g, k)
    rt.assert_same("push -u new branch", upstream=True)
    g, k = rt.both("push", "origin", "--delete", "topic")
    assert g[0] == k[0] == 0, (g, k)
    rt.assert_same("push --delete", upstream=True)
    g, k = rt.both("push")
    assert g[0] == k[0] == 0 and rt.norm(g[2], "g") == rt.norm(k[2], "k"), (g, k)
