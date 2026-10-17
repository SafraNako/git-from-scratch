"""Conflicted merges, step by step against git: the index stages, the files left in the work tree (conflict markers included), the state files, abort and conclusion."""
import os

import pytest

import gitoracle as go
from twin import Twin

pytestmark = pytest.mark.skipif(go.GIT is None, reason="real git is not installed")


def scenario(tmp_path, name="c"):
    """base: five files on main; 'side' and 'main' then change them in every way that conflicts."""
    twin = Twin(tmp_path, name)
    twin.write("c.txt", b"1\n2\n3\n4\n5\n")
    twin.write("md.txt", b"m\n")
    twin.write("dm.txt", b"base\n")
    twin.write("bin", b"\0a")
    twin.write("ok.txt", b"x\n")
    twin.write("sub/deep.txt", b"a\nb\nc\n")
    assert twin.add(".") == (True, True) and twin.commit("base") == (True, True)
    assert twin.branch("side") == (True, True)
    assert twin.checkout("side") == (True, True)
    twin.write("c.txt", b"1\nSIDE\n3\n4\n5\n")
    twin.write("md.txt", b"side-mod\n")
    twin.delete("dm.txt")
    twin.write("aa.txt", b"side-add\n")
    twin.write("bin", b"\0side")
    twin.write("ok.txt", b"ok-side\n")
    twin.write("sub/deep.txt", b"a\nb\nc\nSIDE-TAIL\n")
    assert twin.add(".") == (True, True) and twin.commit("side") == (True, True)
    assert twin.checkout("main") == (True, True)
    twin.write("c.txt", b"1\nMAIN\n3\n4\n5\n")
    twin.delete("md.txt")
    twin.write("dm.txt", b"main-mod\n")
    twin.write("aa.txt", b"main-add\n")
    twin.write("bin", b"\0main")
    twin.write("sub/deep.txt", b"MAIN-HEAD\na\nb\nc\n")
    assert twin.add(".") == (True, True) and twin.commit("main") == (True, True)
    return twin


def test_every_kind_of_conflict_leaves_the_same_repository_as_git(tmp_path):
    twin = scenario(tmp_path)
    assert twin.merge("side") == (False, False)
    twin.assert_same("after the conflicted merge")
    stages = go.gits(twin.theirs_dir, "ls-files", "-u").splitlines()
    assert len(stages) == 12 and twin.in_merge()                       # c.txt and bin x3, aa.txt x2, modify/delete x2 each
    assert open(os.path.join(twin.ours_dir, "c.txt")).read() == "1\n<<<<<<< HEAD\nMAIN\n=======\nSIDE\n>>>>>>> side\n3\n4\n5\n"
    assert open(os.path.join(twin.ours_dir, "sub/deep.txt")).read() == "MAIN-HEAD\na\nb\nc\nSIDE-TAIL\n"          # merged without a conflict
    assert go.gits(twin.theirs_dir, "status", "--porcelain").splitlines()[:5] == ["AA aa.txt", "UU bin", "UU c.txt", "UD dm.txt", "DU md.txt"]


def test_everything_in_the_way_of_a_merge_is_refused_like_git_does(tmp_path):
    twin = scenario(tmp_path)
    assert twin.merge("side") == (False, False)
    assert twin.merge("side") == (False, False)                          # "you have not concluded your merge"
    assert twin.commit("too early") == (False, False)                    # unmerged paths
    assert twin.checkout("side") == (False, False)
    assert twin.reset("HEAD", "soft") == (False, False)                  # no soft reset in the middle of a merge
    twin.assert_same("after the refusals")


def test_abort_restores_head_and_keeps_unrelated_local_changes(tmp_path):
    twin = scenario(tmp_path)
    twin.write("untouched.txt", b"local\n")                              # untracked, not part of the merge
    assert twin.merge("side") == (False, False)
    assert twin.merge_abort() == (True, True)
    twin.assert_same("after the abort")
    assert not twin.in_merge()
    assert open(os.path.join(twin.ours_dir, "c.txt")).read() == "1\nMAIN\n3\n4\n5\n"
    assert os.path.exists(os.path.join(twin.ours_dir, "untouched.txt"))
    assert twin.merge_abort() == (False, False)                          # nothing to abort any more


def test_abort_is_refused_when_a_merged_file_was_edited_further(tmp_path):
    twin = scenario(tmp_path)
    assert twin.merge("side") == (False, False)
    twin.write("ok.txt", b"edited after the merge\n")                    # ok.txt merged cleanly and is staged; now it has unstaged changes
    assert twin.merge_abort() == (False, False)
    twin.assert_same("the refused abort changed nothing")


@pytest.mark.parametrize("no_edit", [False, True])
def test_resolving_and_concluding_gives_the_same_merge_commit(tmp_path, no_edit):
    twin = scenario(tmp_path)
    assert twin.merge("side") == (False, False)
    for name in ("c.txt", "aa.txt", "bin"):
        twin.write(name, b"resolved\n")
    twin.delete("md.txt")
    twin.delete("dm.txt")
    assert twin.add(".") == (True, True)
    assert twin.commit("Merge branch side, resolved by hand", no_edit=no_edit) == (True, True)
    twin.assert_same("after concluding")
    assert not twin.in_merge()
    parents = go.gits(twin.theirs_dir, "rev-list", "--parents", "-n", "1", "HEAD").split()
    assert len(parents) == 3                                             # a merge commit, and the very same object on both sides (assert_same compared HEAD)


def test_hard_reset_ends_the_merge(tmp_path):
    twin = scenario(tmp_path)
    assert twin.merge("side") == (False, False)
    assert twin.reset("HEAD", "hard") == (True, True)
    twin.assert_same("after reset --hard")
    assert not twin.in_merge()
