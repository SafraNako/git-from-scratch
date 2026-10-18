"""Merging a file that one side moved and the other changed, step by step against git."""
import os

import pytest

import gitoracle as go
from twin import Twin

pytestmark = pytest.mark.skipif(go.GIT is None, reason="real git is not installed")


def body(prefix, n=20, change=None):
    lines = ["%s %d" % (prefix, i) for i in range(1, n + 1)]
    for k, v in (change or {}).items():
        lines[k - 1] = v
    return ("\n".join(lines) + "\n").encode()


def start(tmp_path, files):
    twin = Twin(tmp_path, "r")
    for name, content in files.items():
        twin.write(name, content)
    assert twin.add(".") == (True, True) and twin.commit("base") == (True, True)
    assert twin.branch("side") == (True, True)
    return twin


def on(twin, branch, edits, removed=()):
    assert twin.checkout(branch) == (True, True)
    for name in removed:
        twin.delete(name)
    for name, content in edits.items():
        twin.write(name, content)
    assert twin.add(".") == (True, True) and twin.commit("on " + branch) == (True, True)


def merge_ok(twin):
    ours, theirs = twin.merge("side")
    assert ours == theirs, "ours %s, git %s" % (ours, theirs)
    twin.assert_same("after the merge")
    return ours


@pytest.mark.parametrize("renamer, other", [("side", "main"), ("main", "side")])
def test_a_renamed_file_picks_up_the_other_sides_edit(tmp_path, renamer, other):
    twin = start(tmp_path, {"old.txt": body("x"), "keep.txt": b"k\n"})
    on(twin, renamer, {"new.txt": body("x")}, removed=["old.txt"])
    on(twin, other, {"old.txt": body("x", change={5: "FIVE"})})
    assert twin.checkout("main") == (True, True)
    assert merge_ok(twin)
    assert open(os.path.join(twin.ours_dir, "new.txt")).read().splitlines()[4] == "FIVE"


@pytest.mark.parametrize("renamer, other", [("side", "main"), ("main", "side")])
def test_a_rename_with_a_conflicting_edit_conflicts_at_the_new_path(tmp_path, renamer, other):
    twin = start(tmp_path, {"old.txt": body("x")})
    on(twin, renamer, {"new.txt": body("x", change={5: "R-SIDE"})}, removed=["old.txt"])
    on(twin, other, {"old.txt": body("x", change={5: "O-SIDE"})})
    assert twin.checkout("main") == (True, True)
    assert not merge_ok(twin)
    assert twin.in_merge()
    text = open(os.path.join(twin.ours_dir, "new.txt")).read()
    assert "<<<<<<< HEAD:" in text and ">>>>>>> side:" in text


@pytest.mark.parametrize("renamer, deleter, edited", [("side", "main", False), ("main", "side", False), ("side", "main", True), ("main", "side", True)])
def test_rename_against_delete(tmp_path, renamer, deleter, edited):
    twin = start(tmp_path, {"old.txt": body("x"), "keep.txt": b"k\n"})
    on(twin, renamer, {"new.txt": body("x", change={3: "edited"} if edited else None)}, removed=["old.txt"])
    on(twin, deleter, {}, removed=["old.txt"])
    assert twin.checkout("main") == (True, True)
    assert not merge_ok(twin)
    assert twin.in_merge()


@pytest.mark.parametrize("same_spot", [False, True])
def test_both_sides_renamed_to_the_same_name(tmp_path, same_spot):
    twin = start(tmp_path, {"old.txt": body("x")})
    on(twin, "side", {"new.txt": body("x", change={3: "side-3"})}, removed=["old.txt"])
    on(twin, "main", {"new.txt": body("x", change={3 if same_spot else 15: "main-edit"})}, removed=["old.txt"])
    assert twin.checkout("main") == (True, True)
    assert merge_ok(twin) != same_spot


def test_both_sides_renamed_to_different_names(tmp_path):
    twin = start(tmp_path, {"old.txt": body("x")})
    on(twin, "side", {"side.txt": body("x")}, removed=["old.txt"])
    on(twin, "main", {"main.txt": body("x")}, removed=["old.txt"])
    assert twin.checkout("main") == (True, True)
    assert not merge_ok(twin)


def test_a_mode_change_follows_the_rename(tmp_path):
    twin = start(tmp_path, {"old.txt": body("x")})
    on(twin, "side", {"new.txt": body("x")}, removed=["old.txt"])
    assert twin.checkout("main") == (True, True)
    twin.chmod("old.txt", True)
    assert twin.add(".") == (True, True) and twin.commit("exec") == (True, True)
    assert merge_ok(twin)


def test_renames_between_directories_with_several_files(tmp_path):
    twin = start(tmp_path, {"a/one.txt": body("one"), "a/two.txt": body("two"), "b/three.txt": body("three"), "top.txt": body("top")})
    on(twin, "side", {"c/uno.txt": body("one"), "b/drei.txt": body("three", change={2: "d"}), "top.txt": body("top")}, removed=["a/one.txt", "b/three.txt"])
    on(twin, "main", {"a/one.txt": body("one", change={7: "main-1"}), "b/three.txt": body("three", change={15: "main-3"}), "a/two.txt": body("two", change={1: "x"})})
    assert twin.checkout("main") == (True, True)
    assert merge_ok(twin)


def test_added_files_that_look_like_renames_are_left_alone(tmp_path):
    twin = start(tmp_path, {"old.txt": body("x")})
    on(twin, "side", {"new.txt": body("x")}, removed=["old.txt"])
    on(twin, "main", {"new.txt": body("other")})
    assert twin.checkout("main") == (True, True)
    assert not merge_ok(twin)                                            # new.txt was added on both sides with different content


def test_a_directory_rename_is_refused_not_guessed(tmp_path):
    twin = start(tmp_path, {"d/one.txt": body("one"), "d/two.txt": body("two")})
    on(twin, "side", {"e/one.txt": body("one"), "e/two.txt": body("two")}, removed=["d/one.txt", "d/two.txt"])
    on(twin, "main", {"d/three.txt": body("three")})
    assert twin.checkout("main") == (True, True)
    ours, _git = twin.merge("side")
    assert not ours                                                     # git would suggest moving d/three.txt; this program says it does not do that
