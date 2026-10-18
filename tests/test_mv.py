"""``mv`` against git: renames, moves into directories, directories, refusals."""
import pytest

import gitoracle as go
from twin import Twin

pytestmark = pytest.mark.skipif(go.GIT is None, reason="real git is not installed")


def start(tmp_path):
    twin = Twin(tmp_path, "m")
    twin.write("a.txt", b"a\n")
    twin.write("b.txt", b"b\n")
    twin.write("d/x.txt", b"x\n")
    twin.write("d/e/y.txt", b"y\n")
    twin.write("run.sh", b"#!/bin/sh\n")
    twin.chmod("run.sh", True)
    twin.write("untracked.txt", b"u\n")
    assert twin.add("a.txt", "b.txt", "d", "run.sh") == (True, True)
    assert twin.commit("base") == (True, True)
    return twin


def test_moves_and_renames_match_git(tmp_path):
    twin = start(tmp_path)
    assert twin.mv(["a.txt"], "renamed.txt") == (True, True)
    twin.assert_same("after a rename")
    assert twin.mv(["b.txt", "run.sh"], "d") == (True, True)
    twin.assert_same("after moving two files into a directory")
    assert twin.mv(["d/e"], "moved") == (True, True)
    twin.assert_same("after renaming a directory")
    assert twin.mv(["renamed.txt"], "new/sub/dir.txt") == (False, False)             # the target directory does not exist
    twin.assert_same("after the refused move")
    assert twin.commit("moved") == (True, True)
    twin.assert_same("after committing the moves")


def test_refusals_match_git(tmp_path):
    twin = start(tmp_path)
    assert twin.mv(["untracked.txt"], "u2.txt") == (False, False)
    assert twin.mv(["missing.txt"], "m2.txt") == (False, False)
    assert twin.mv(["a.txt"], "b.txt") == (False, False)
    assert twin.mv(["a.txt", "b.txt"], "nodir") == (False, False)
    assert twin.mv(["d"], "d/e") == (False, False)
    twin.assert_same("after the refusals")
    assert twin.mv(["a.txt"], "b.txt", force=True) == (True, True)
    twin.assert_same("after the forced overwrite")
