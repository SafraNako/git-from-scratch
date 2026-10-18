"""clone, fetch, pull and push between local repositories, compared step by step with git."""
import os

import pytest

import gitoracle as go
from rtwin import RTwin, commit_file

pytestmark = pytest.mark.skipif(go.GIT is None, reason="real git is not installed")

TRANSPORTS = ["local", "file", "git", "http"]


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
    g, k = rt.both("pull", "--no-ff", "--no-rebase")
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
    assert g[0] == k[0] == 0 and no_hints(rt.norm(g[2], "g")) == no_hints(rt.norm(k[2], "k").replace("kgit", "git")), (g, k)


def no_hints(text):
    """git words its ``hint:`` lines differently from one release to the next; everything else must match."""
    return "".join(line + "\n" for line in text.splitlines() if not line.startswith("hint:"))


def same_output(rt, *args, normalise=True):
    g, k = rt.both(*args)
    assert g[0] == k[0], (args, g, k)
    gt, kt = (rt.norm(g[1], "g"), rt.norm(k[1], "k")) if normalise else (g[1], k[1])
    assert gt == kt, (args, gt, kt)
    return g, k


def test_tracking_information_in_status_and_branch(rt):
    rt.clone()
    rt.step += 1
    for d in (rt.dir["g"], rt.dir["k"]):
        go.git(d, "checkout", "-q", "-b", "work")
        go.write(d, "w.txt", "work\n")
        go.git(d, "add", "-A")
        go.git(d, "commit", "-qm", "work", extra={"GIT_AUTHOR_DATE": "1700000400 +0000", "GIT_COMMITTER_DATE": "1700000400 +0000"})
        go.git(d, "branch", "-q", "--set-upstream-to=origin/dev", "work")
    same_output(rt, "status", "--porcelain", "-b")
    rt.feed(lambda d: (go.git(d, "checkout", "-q", "dev"), commit_file(d, "dd.txt", "dd\n", "dev moves", 1700000500), go.git(d, "checkout", "-q", "main")))
    rt.both("fetch")
    same_output(rt, "status", "--porcelain", "-b")
    same_output(rt, "branch", "-vv")
    same_output(rt, "branch", "-v")
    same_output(rt, "branch", "-r")
    same_output(rt, "branch", "-a")
    same_output(rt, "branch", "-r", "-v")
    same_output(rt, "rev-parse", "work@{u}")
    same_output(rt, "rev-parse", "@{upstream}")
    rt.both("branch", "--unset-upstream", "work")
    rt.assert_same("after the tracking commands")


def test_checkout_creates_tracking_branches(rt):
    rt.clone()
    g, k = rt.both("checkout", "dev")
    assert g[0] == k[0] == 0, (g, k)
    rt.assert_same("checkout of a remote-only branch")
    g, k = rt.both("checkout", "-b", "again", "origin/main")
    assert g[0] == k[0] == 0, (g, k)
    rt.assert_same("checkout -b from a remote-tracking branch")
    g, k = rt.both("branch", "viaremote", "origin/dev")
    assert g[0] == k[0] == 0, (g, k)
    rt.assert_same("branch from a remote-tracking branch")


def test_remote_subcommands_and_ls_remote(rt):
    rt.clone()
    same_output(rt, "remote")
    same_output(rt, "remote", "-v")
    rt.both("remote", "add", "second", rt.url["g"] if False else "/nonexistent/path.git")
    rt.assert_same("after remote add", clients=True)
    same_output(rt, "remote", "-v")
    rt.both("remote", "rename", "second", "other")
    rt.assert_same("after remote rename")
    rt.both("remote", "set-url", "other", "/elsewhere.git")
    same_output(rt, "remote", "get-url", "other")
    rt.both("remote", "remove", "other")
    same_output(rt, "remote")
    rt.assert_same("after remote remove")
    g = go.gits(rt.root, "ls-remote", rt.url["g"])
    k = rt.kgit("ls-remote", rt.url["k"], cwd=rt.root)
    assert rt.norm(g, "g") == rt.norm(k[1], "k")


def test_push_variants(rt):
    rt.clone()

    def commit_both(name, message, when):
        rt.step += 1
        for d in (rt.dir["g"], rt.dir["k"]):
            go.write(d, name, message + "\n")
            go.git(d, "add", "-A")
            go.git(d, "commit", "-qm", message, extra={"GIT_AUTHOR_DATE": "%d +0000" % when, "GIT_COMMITTER_DATE": "%d +0000" % when})
    commit_both("one.txt", "one", 1700000600)
    for d in (rt.dir["g"], rt.dir["k"]):
        go.git(d, "tag", "lt1")
        go.git(d, "tag", "-a", "an1", "-m", "annotated one", extra={"GIT_COMMITTER_DATE": "1700000601 +0000"})
    g, k = rt.both("push", "--tags")
    assert g[0] == k[0] == 0 and no_hints(rt.norm(g[2], "g")) == no_hints(rt.norm(k[2], "k").replace("kgit", "git")), (g, k)
    rt.assert_same("after push --tags", upstream=True)
    g, k = rt.both("push", "origin", "main:refs/heads/renamed")
    assert g[0] == k[0] == 0 and no_hints(rt.norm(g[2], "g")) == no_hints(rt.norm(k[2], "k").replace("kgit", "git")), (g, k)
    rt.assert_same("after pushing to another name", upstream=True)
    # upstream moves on; a push must be refused (fetch first), after a fetch it is a non-fast-forward, with --force it goes through
    rt.feed(lambda d: commit_file(d, "other.txt", "other\n", "upstream", 1700000700))
    commit_both("two.txt", "two", 1700000800)
    g, k = rt.both("push")
    assert g[0] == k[0] == 1 and no_hints(rt.norm(g[2], "g")) == no_hints(rt.norm(k[2], "k").replace("kgit", "git")), (g, k)
    rt.both("fetch")
    g, k = rt.both("push")
    assert g[0] == k[0] == 1 and no_hints(rt.norm(g[2], "g")) == no_hints(rt.norm(k[2], "k").replace("kgit", "git")), (g, k)
    rt.assert_same("after the refused pushes", upstream=True)
    g, k = rt.both("push", "--force")
    assert g[0] == k[0] == 0 and no_hints(rt.norm(g[2], "g")) == no_hints(rt.norm(k[2], "k").replace("kgit", "git")), (g, k)
    rt.assert_same("after the forced push", upstream=True)
    g, k = rt.both("push", "origin", ":renamed")                      # the feeder's mirror push already removed it
    assert g[0] == k[0] == 1 and no_hints(rt.norm(g[2], "g")) == no_hints(rt.norm(k[2], "k").replace("kgit", "git")), (g, k)
    g, k = rt.both("push", "origin", ":dev")
    assert g[0] == k[0] == 0 and no_hints(rt.norm(g[2], "g")) == no_hints(rt.norm(k[2], "k").replace("kgit", "git")), (g, k)
    rt.assert_same("after deleting through :ref", upstream=True)


def test_pull_conflict_and_ff_only(rt):
    rt.clone()
    rt.step += 1
    for d in (rt.dir["g"], rt.dir["k"]):
        go.write(d, "a.txt", "one\nLOCAL\nthree\n")
        go.git(d, "add", "-A")
        go.git(d, "commit", "-qm", "local edit", extra={"GIT_AUTHOR_DATE": "1700000150 +0000", "GIT_COMMITTER_DATE": "1700000150 +0000"})
    rt.feed(lambda d: commit_file(d, "a.txt", "one\nREMOTE\nthree\n", "remote edit", 1700000200))
    g, k = rt.both("pull", "--ff-only")
    assert g[0] != 0 and k[0] != 0, (g, k)
    rt.assert_same("after the refused fast-forward-only pull")
    g, k = rt.both("pull", "--no-rebase")
    assert g[0] == 1 and k[0] == 1, (g, k)
    rt.assert_same("after the conflicted pull")
    assert os.path.exists(os.path.join(rt.dir["k"], ".git", "MERGE_HEAD"))
    g, k = rt.both("merge", "--abort")
    assert g[0] == k[0] == 0, (g, k)
    rt.assert_same("after the abort")


def test_fetch_all_and_fetching_an_url(rt):
    rt.clone()
    rt.feed(lambda d: commit_file(d, "q.txt", "q\n", "q", 1700000100))
    g, k = rt.both("fetch", "--all")
    assert g[0] == k[0] == 0, (g, k)
    rt.assert_same("after fetch --all")
    rt.feed(lambda d: commit_file(d, "r.txt", "r\n", "r", 1700000200))
    g = rt.git("g", "fetch", rt.url["g"], "main")
    k = rt.kgit("fetch", rt.url["k"], "main")
    assert g[0] == k[0] == 0, (g, k)
    assert rt.norm(open(os.path.join(rt.dir["g"], ".git", "FETCH_HEAD")).read(), "g") == rt.norm(open(os.path.join(rt.dir["k"], ".git", "FETCH_HEAD")).read(), "k")
    g, k = rt.both("fetch", "--tags")
    assert g[0] == k[0] == 0, (g, k)


def test_bare_and_no_checkout_clones(rt):
    rt.step += 1
    gd, kd = os.path.join(rt.root, "bare_g.git"), os.path.join(rt.root, "bare_k.git")
    assert rt.git("g", "clone", "-q", "--bare", rt.url["g"], gd, cwd=rt.root)[0] == 0
    assert rt.kgit("clone", "--bare", rt.url["k"], kd, cwd=rt.root)[0] == 0
    g, k = rt.snapshot("g", gd, bare=True), rt.snapshot("k", kd, bare=True)
    assert g["refs"] == k["refs"] and g["head"] == k["head"] and g["config"] == k["config"], (g, k)
    assert go.git(kd, "fsck", "--strict", check=False).returncode == 0
    nd = os.path.join(rt.root, "nocheckout")
    assert rt.kgit("clone", "--no-checkout", rt.url["k"], nd, cwd=rt.root)[0] == 0
    assert sorted(f for f in os.listdir(nd) if f != ".git") == []
    assert "D  a.txt" in go.gits(nd, "status", "--porcelain") or "a.txt" in go.gits(nd, "status", "--porcelain")


def test_pushing_into_the_checked_out_branch_of_a_non_bare_repository(tmp_path):
    root = str(tmp_path)
    for who in ("g", "k"):
        d = os.path.join(root, "target_" + who)
        go.git(root, "init", "-q", "-b", "main", d)
        commit_file(d, "t.txt", "target\n", "first", 1700000000)
    gdir, kdir = os.path.join(root, "src_g"), os.path.join(root, "src_k")
    go.git(root, "clone", "-q", os.path.join(root, "target_g"), gdir)
    go.git(root, "clone", "-q", os.path.join(root, "target_k"), kdir)
    for d in (gdir, kdir):
        commit_file(d, "n.txt", "new\n", "second", 1700000100)
    gp = go.git(gdir, "push", "origin", "main", check=False)
    import io
    from kendi_git import cli
    out, err = io.BytesIO(), io.BytesIO()
    code = cli.main(["push", "origin", "main"], stdout=out, stderr=err, cwd=kdir, env=go.env())
    assert gp.returncode == code == 1
    git_text = gp.stderr.decode().replace(os.path.join(root, "target_g"), "T")
    our_text = err.getvalue().decode().replace(os.path.join(root, "target_k"), "T").replace("kgit", "git")
    assert [l for l in git_text.splitlines() if "remote rejected" in l] == [l for l in our_text.splitlines() if "remote rejected" in l], (git_text, our_text)
    for who in ("g", "k"):                                            # neither target moved
        assert go.gits(os.path.join(root, "target_" + who), "rev-parse", "HEAD") == go.gits(os.path.join(root, "src_" + who), "rev-parse", "HEAD~1")


def test_cloning_an_empty_repository_and_a_detached_head(tmp_path):
    import io
    from kendi_git import cli
    root = str(tmp_path)
    # an empty bare repository whose HEAD names a branch other than main
    empty = os.path.join(root, "empty.git")
    go.git(root, "init", "-q", "--bare", "-b", "trunk", empty)
    go.git(root, "clone", "-q", empty, os.path.join(root, "g_empty"))
    out, err = io.BytesIO(), io.BytesIO()
    assert cli.main(["clone", empty, os.path.join(root, "k_empty")], stdout=out, stderr=err, cwd=root, env=go.env()) == 0
    assert go.gits(os.path.join(root, "k_empty"), "symbolic-ref", "HEAD") == go.gits(os.path.join(root, "g_empty"), "symbolic-ref", "HEAD")
    assert "empty repository" in err.getvalue().decode()
    # a repository whose HEAD is detached
    src = os.path.join(root, "src")
    go.git(root, "init", "-q", "-b", "main", src)
    commit_file(src, "a.txt", "a\n", "one", 1700000000)
    commit_file(src, "b.txt", "b\n", "two", 1700000010)
    go.git(src, "checkout", "-q", "--detach", "HEAD~1")
    go.git(root, "clone", "-q", src, os.path.join(root, "g_det"))
    out, err = io.BytesIO(), io.BytesIO()
    assert cli.main(["clone", src, os.path.join(root, "k_det")], stdout=out, stderr=err, cwd=root, env=go.env()) == 0, err.getvalue()
    for who in ("g", "k"):
        d = os.path.join(root, who + "_det")
        assert go.gits(d, "rev-parse", "HEAD") == go.gits(src, "rev-parse", "HEAD")
        assert go.git(d, "symbolic-ref", "-q", "HEAD", check=False).returncode == 1
    assert sorted(os.listdir(os.path.join(root, "k_det"))) == sorted(os.listdir(os.path.join(root, "g_det")))


def test_renaming_and_removing_the_remote_that_a_clone_follows(rt):
    rt.clone()
    g, k = rt.both("remote", "rename", "origin", "upstream")
    assert g[0] == k[0] == 0, (g, k)
    rt.assert_same("after renaming origin", logs=go.GIT_VERSION < (2, 40, 0))        # what newer git writes into the reflogs of a renamed remote is different again
    g, k = rt.both("remote", "remove", "upstream")
    assert g[0] == k[0] == 0, (g, k)
    rt.assert_same("after removing the remote", logs=go.GIT_VERSION < (2, 40, 0))
