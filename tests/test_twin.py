import os
import random

import pytest

import gitoracle as go
from twin import Twin, random_branch_step, random_step, seed_files

pytestmark = pytest.mark.skipif(go.GIT is None, reason="real git is not installed")


@pytest.mark.parametrize("seed", range(int(os.environ.get("KGIT_SEED_BASE", "0")), int(os.environ.get("KGIT_SEED_BASE", "0")) + int(os.environ.get("KGIT_TWIN_SEEDS", "4"))))
def test_random_sessions_keep_both_repositories_identical(tmp_path, seed):
    rng = random.Random(seed)
    twin = Twin(tmp_path)
    history = []
    for n in range(45):
        try:
            history.append(random_step(twin, rng))
            twin.assert_same(f"after step {n}: {history[-1]}")
        except AssertionError as exc:
            raise AssertionError(f"{exc}\nsteps so far: " + "\n  ".join([""] + history)) from None


@pytest.mark.parametrize("seed", range(int(os.environ.get("KGIT_SEED_BASE", "0")), int(os.environ.get("KGIT_SEED_BASE", "0")) + int(os.environ.get("KGIT_BRANCH_SEEDS", "4"))))
def test_random_branching_sessions_keep_both_repositories_identical(tmp_path, seed):
    """Branches, checkouts (including refused ones), resets, merges and tags, step by step against real git."""
    rng = random.Random(500 + seed)
    twin = Twin(tmp_path)
    history = []

    def check(steps):
        twin.assert_same(f"after {steps[-1]}")

    seed_files(twin, rng)
    for n in range(40):
        try:
            steps = random_branch_step(twin, rng, check)
            history.extend(steps)
            twin.assert_same(f"after round {n}: {steps[-1]}")
        except AssertionError as exc:
            raise AssertionError(f"{exc}\nsteps so far: " + "\n  ".join([""] + history)) from None


def test_add_update_does_not_complain_about_ignored_paths_it_would_not_add(tmp_path):
    """Found by a fresh fuzz run: ``add -u tmp/out.log`` with ``tmp/`` ignored succeeds in git 2.39 (``-u`` adds no new files), while a plain ``add`` fails. Later git
    versions complain here as well, so the comparison with git is only made for the version this program follows."""
    twin = Twin(tmp_path)
    twin.write(".gitignore", b"tmp/\n")
    twin.write("tmp/out.log", b"x\n")
    twin.write("a.txt", b"a\n")
    assert twin.add(".") == (True, True)
    assert twin.commit("c") == (True, True)
    assert twin.add("tmp/out.log") == (False, False)
    for spec in ("tmp/out.log", "tmp"):
        ours, theirs = twin.add(spec, update=True)
        assert ours is True
        if go.GIT_VERSION < (2, 40):
            assert theirs is True
    if go.GIT_VERSION < (2, 40):
        twin.assert_same("after the ignored-path adds")
