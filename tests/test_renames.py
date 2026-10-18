"""Rename detection in diff, show and log against git."""
import io
import os
import random

import pytest

import gitoracle as go
from kendi_git import cli
from test_repo_history import fresh

pytestmark = pytest.mark.skipif(go.GIT is None, reason="real git is not installed")


def kgit(repo, *argv):
    out, err = io.BytesIO(), io.BytesIO()
    code = cli.main(list(argv), stdout=out, stderr=err, cwd=repo, env=go.env())
    assert code == 0, err.getvalue()
    return out.getvalue().decode()


WORDS = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta", "iota", "kappa", "lambda", "mu", "nu", "xi", "omicron", "pi", "rho", "sigma", "tau"]
DIRS = ["", "a/", "a/b/", "c/", "lib/", "src/util/"]
NAMES = ["f.txt", "g.txt", "main.c", "util.c", "README", "x.md", "data.bin", "h.py"]


def make_text(rng, n):
    return "".join("%s %s %d\n" % (rng.choice(WORDS), rng.choice(WORDS), rng.randrange(40)) for _ in range(n))


def mutate(rng, text):
    lines = text.splitlines(True)
    for _ in range(rng.choice([0, 0, 1, 2, 4, 8])):
        if not lines:
            break
        i = rng.randrange(len(lines))
        r = rng.random()
        if r < 0.5:
            lines[i] = "%s changed %d\n" % (rng.choice(WORDS), rng.randrange(1000))
        elif r < 0.75:
            lines.insert(i, "inserted %s\n" % rng.choice(WORDS))
        else:
            del lines[i]
    return "".join(lines)


def populate(repo, rng):
    files = {}
    for _ in range(rng.randint(6, 14)):
        path = rng.choice(DIRS) + rng.choice(NAMES)
        if path in files:
            continue
        files[path] = make_text(rng, rng.randint(1, 30)) if rng.random() > 0.1 else ""
    for path, text in files.items():
        go.write(repo, path, text)
    return files


def commit_all(repo, message):
    go.git(repo, "add", "-A")
    go.git(repo, "commit", "-qm", message, "--allow-empty")


def rename_round(repo, rng, files):
    """Move, copy-and-edit, delete and add files so that the next commit is full of rename candidates."""
    paths = sorted(files)
    rng.shuffle(paths)
    for path in paths[:rng.randint(1, max(1, len(paths) // 2))]:
        r = rng.random()
        text = files.pop(path)
        os.remove(os.path.join(repo, path))
        if r < 0.75:
            new = rng.choice(DIRS) + (path.rsplit("/", 1)[-1] if rng.random() < 0.5 else rng.choice(NAMES))
            if new in files:
                continue
            files[new] = mutate(rng, text) if rng.random() < 0.7 else text
            go.write(repo, new, files[new])
        elif r < 0.85:
            new = rng.choice(DIRS) + rng.choice(NAMES)
            if new not in files:
                files[new] = make_text(rng, rng.randint(1, 10))
                go.write(repo, new, files[new])
    for path in list(files):
        if rng.random() < 0.2:
            files[path] = mutate(rng, files[path])
            go.write(repo, path, files[path])
    return files


@pytest.mark.parametrize("seed", range(int(os.environ.get("KGIT_RENAME_SEEDS", "8"))))
def test_renames_match_git(tmp_path, seed):
    rng = random.Random(1700 + seed)
    repo = fresh(tmp_path, "r")
    files = populate(repo, rng)
    commit_all(repo, "base")
    for step in range(4):
        files = rename_round(repo, rng, files)
        commit_all(repo, "step %d" % step)
        for argv in (["show"], ["show", "--stat"], ["diff", "HEAD~1", "HEAD"], ["diff", "HEAD~1", "HEAD", "--stat"], ["diff", "HEAD~1", "HEAD", "--numstat"],
                     ["diff", "HEAD~1", "HEAD", "--name-status"], ["diff", "HEAD~1", "HEAD", "--name-only"], ["diff", "HEAD~1", "HEAD", "--summary"],
                     ["diff", "HEAD~1", "HEAD", "--shortstat"], ["diff", "HEAD~1", "HEAD", "-M80%"], ["diff", "HEAD~1", "HEAD", "-M10"], ["diff", "HEAD~1", "HEAD", "--no-renames", "--name-status"],
                     ["log", "-p", "-n", "1"], ["log", "--stat", "-n", "1", "--oneline"]):
            theirs = go.gits(repo, *[a for a in argv], check=False)
            if argv[0] == "log" or argv[0] == "show":
                theirs = go.gits(repo, argv[0], "--no-decorate", *argv[1:], check=False)
            assert kgit(repo, *argv) == theirs, (seed, step, argv)
    # staged and work tree comparisons against HEAD
    files = rename_round(repo, rng, files)
    go.git(repo, "add", "-A")
    for argv in (["diff", "--cached", "--name-status"], ["diff", "--cached"], ["diff", "--cached", "--stat"], ["diff", "HEAD", "--name-status"], ["status", "--porcelain"],
                 ["status", "-s"], ["status", "--porcelain", "-z"], ["status", "-sb"]):
        assert kgit(repo, *argv) == go.gits(repo, *argv, check=False), (seed, argv)
    # edit some of the moved files in the work tree on top of the staged renames
    for path in sorted(files)[:3]:
        files[path] = mutate(rng, files[path]) + "more\n"
        go.write(repo, path, files[path])
    for argv in (["status", "--porcelain"], ["status", "-s"], ["status", "--porcelain", "-z"]):
        assert kgit(repo, *argv) == go.gits(repo, *argv, check=False), (seed, argv, "edited")


def history_with_moves(repo, rng, steps=10):
    """A linear history in which one file keeps being edited, renamed, copied, and replaced by lookalikes."""
    files = {"main.txt": make_text(rng, 24), "other.txt": make_text(rng, 18)}
    for p, t in files.items():
        go.write(repo, p, t)
    commit_all(repo, "base")
    current = "main.txt"
    for k in range(steps):
        r = rng.random()
        if r < 0.35:
            new = rng.choice(DIRS) + rng.choice(NAMES)
            if new in files:
                continue
            os.remove(os.path.join(repo, current))
            files[new] = mutate(rng, files.pop(current))
            go.write(repo, new, files[new])
            current = new
        elif r < 0.6:
            new = rng.choice(DIRS) + rng.choice(NAMES)
            if new in files:
                continue
            files[new] = mutate(rng, files[current])                       # a copy: the original stays
            go.write(repo, new, files[new])
        else:
            files[current] = mutate(rng, files[current]) + "tail %d\n" % k
            go.write(repo, current, files[current])
        if rng.random() < 0.3:
            other = rng.choice(sorted(files))
            files[other] = mutate(rng, files[other]) + "x\n"
            go.write(repo, other, files[other])
        commit_all(repo, "step %d" % k)
    return current


@pytest.mark.parametrize("seed", range(int(os.environ.get("KGIT_FOLLOW_SEEDS", "12"))))
def test_log_follow_matches_git(tmp_path, seed):
    rng = random.Random(2600 + seed)
    repo = fresh(tmp_path, "f")
    current = history_with_moves(repo, rng)
    for argv in (["log", "--follow", "--oneline", "--", current], ["log", "--follow", "-p", "--", current], ["log", "--follow", "--stat", "--", current],
                 ["log", "--follow", "-n", "2", "--oneline", "--", current]):
        theirs = go.gits(repo, argv[0], "--no-decorate", *argv[1:], check=False)
        assert kgit(repo, *argv) == theirs, (seed, argv)
