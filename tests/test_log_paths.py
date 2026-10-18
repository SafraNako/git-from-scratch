"""``log -- <path>`` (history simplification) against git, on random commit graphs whose trees change in random ways."""
import io
import os
import random

import pytest

import gitoracle as go
from kendi_git import cli
from test_repo_history import fresh

pytestmark = pytest.mark.skipif(go.GIT is None, reason="real git is not installed")

FILES = ["a.txt", "b.txt", "d/c.txt", "d/e/f.txt", "g/h.md"]


def kgit(repo, *argv):
    out, err = io.BytesIO(), io.BytesIO()
    code = cli.main(list(argv), stdout=out, stderr=err, cwd=repo, env=go.env())
    assert code == 0, err.getvalue()
    return out.getvalue().decode()


def write_tree(repo, files, blobs):
    """A tree object for ``{path: variant}`` (nested directories built bottom-up with ``git mktree``)."""
    def build(prefix):
        lines = []
        names = {}
        for path, variant in files.items():
            if not path.startswith(prefix):
                continue
            rest = path[len(prefix):]
            head, _, tail = rest.partition("/")
            names.setdefault(head, []).append((path, tail))
        for head, items in sorted(names.items()):
            if items[0][1] == "":
                lines.append("100644 blob %s\t%s" % (blobs[files[items[0][0]]], head))
            else:
                lines.append("040000 tree %s\t%s" % (build(prefix + head + "/"), head))
        return go.gits(repo, "mktree", input=("\n".join(lines) + "\n").encode() if lines else b"").strip()
    return build("")


def build_tree_dag(repo, rng, n, variants=3, skew=False):
    """Commits made with ``commit-tree`` whose trees evolve: a child starts from its first parent's files, a merge takes each path from either parent, and some paths
    change (to one of a few variants, so equal trees come up often). Returns ids oldest first."""
    blobs = [go.gits(repo, "hash-object", "-w", "--stdin", input=b"variant %d\n" % v).strip() for v in range(variants)]
    state, ids, when = {}, [], 1700000000
    for i in range(n):
        parents = []
        if ids:
            parents = [rng.choice(ids[-6:] if rng.random() < 0.7 else ids)]
            if rng.random() < 0.3 and len(ids) > 1:
                other = rng.choice(ids)
                if other not in parents:
                    parents.append(other)
        files = dict(state[parents[0]]) if parents else {}
        if len(parents) > 1:
            for path in FILES:
                src = state[rng.choice(parents)]
                if path in src:
                    files[path] = src[path]
                else:
                    files.pop(path, None)
        if rng.random() < 0.8:                                          # a change to a path or two (or none: an empty commit)
            for path in rng.sample(FILES, rng.randint(1, 2)):
                if path in files and rng.random() < 0.2:
                    del files[path]
                else:
                    files[path] = rng.randrange(variants)
        if skew:
            when = 1700000000 + rng.randrange(0, 40) * 10                # arbitrary, repeated, out-of-order dates
        else:
            when += rng.choice([0, 10, 10, 60])
        tree = write_tree(repo, files, blobs)
        args = ["commit-tree", tree, "-m", "commit %d" % i]
        for p in parents:
            args += ["-p", p]
        oid = go.gits(repo, *args, extra={"GIT_AUTHOR_DATE": "%d +0000" % when, "GIT_COMMITTER_DATE": "%d +0000" % when}).strip()
        ids.append(oid)
        state[oid] = files
    return ids


PATHSPECS = [["a.txt"], ["d"], ["d/e/f.txt"], ["a.txt", "g"], ["nothing.here"], ["*.txt"], ["d/e"]]
OPTIONS = [[], ["--full-history"], ["--first-parent"], ["--topo-order"], ["--date-order", "--full-history"]]


@pytest.mark.parametrize("seed", range(int(os.environ.get("KGIT_PATHLOG_SEEDS", "6"))))
def test_log_with_a_path_matches_git(tmp_path, seed):
    rng = random.Random(900 + seed)
    repo = fresh(tmp_path, "r")
    ids = build_tree_dag(repo, rng, 40 + 4 * seed)
    for i, oid in enumerate(ids[-5:]):
        go.git(repo, "update-ref", "refs/heads/b%d" % i, oid)
    for paths in PATHSPECS:
        for opts in OPTIONS:
            for tips in (["b4"], ["b1", "b3"]):
                args = ["log", "--oneline", *opts, *tips, "--", *paths]
                mine = kgit(repo, *args)
                theirs = go.git(repo, "log", "--oneline", "--no-decorate", *opts, *tips, "--", *paths).decode()
                assert mine == theirs, (seed, args)
    # ranges (some parents are uninteresting) and a limit
    for _ in range(6):
        a, b = rng.sample(ids, 2)
        for paths in (["a.txt"], ["d"]):
            for opts in ([], ["--full-history"]):
                args = ["log", "--oneline", *opts, "%s..%s" % (a, b), "--", *paths]
                assert kgit(repo, *args) == go.git(repo, "log", "--oneline", "--no-decorate", *opts, "%s..%s" % (a, b), "--", *paths).decode(), (seed, args)
    assert kgit(repo, "log", "--oneline", "-n", "3", "b4", "--", "d") == go.git(repo, "log", "--oneline", "--no-decorate", "-n", "3", "b4", "--", "d").decode()


@pytest.mark.parametrize("seed", range(int(os.environ.get("KGIT_PATHLOG_SEEDS", "6"))))
def test_graph_with_a_path_matches_git(tmp_path, seed):
    rng = random.Random(950 + seed)
    repo = fresh(tmp_path, "r")
    ids = build_tree_dag(repo, rng, 36 + 4 * seed)
    for i, oid in enumerate(ids[-5:]):
        go.git(repo, "update-ref", "refs/heads/b%d" % i, oid)
    for paths in (["a.txt"], ["d"], ["g", "b.txt"]):
        for opts in ([], ["--full-history"]):
            args = ["log", "--oneline", "--graph", *opts, "b4", "b2", "--", *paths]
            assert kgit(repo, *args) == go.git(repo, "log", "--oneline", "--no-decorate", "--graph", *opts, "b4", "b2", "--", *paths).decode(), (seed, args)
    # ranges and --first-parent without a path, through the same machinery
    a, b = ids[10], ids[-1]
    assert kgit(repo, "log", "--oneline", "--graph", "%s..%s" % (a, b)) == go.git(repo, "log", "--oneline", "--no-decorate", "--graph", "%s..%s" % (a, b)).decode()
    assert kgit(repo, "log", "--oneline", "--first-parent", "b4") == go.git(repo, "log", "--oneline", "--no-decorate", "--first-parent", "b4").decode()


def test_patches_and_stats_are_limited_to_the_path(tmp_path):
    rng = random.Random(33)
    repo = fresh(tmp_path, "p")
    ids = build_tree_dag(repo, rng, 25)
    go.git(repo, "update-ref", "refs/heads/top", ids[-1])
    for paths in (["a.txt"], ["d"]):
        for opts in (["-p"], ["--stat"], ["--stat", "-p"], ["-p", "--oneline"]):
            mine = kgit(repo, "log", *opts, "top", "--", *paths)
            theirs = go.git(repo, "log", "--no-decorate", *opts, "top", "--", *paths).decode()
            assert mine == theirs, (paths, opts)


@pytest.mark.parametrize("skew", [False, True])
def test_ranges_with_and_without_paths_match_git(tmp_path, skew):
    """What an excluded side hides depends on how far the "uninteresting" mark has spread by the time a commit is looked at, so out-of-order dates matter here."""
    rng = random.Random(77 if skew else 78)
    for k in range(4):
        repo = fresh(tmp_path, "r%d" % k)
        ids = build_tree_dag(repo, rng, 38, skew=skew)
        for _ in range(10):
            a, b = rng.sample(ids, 2)
            c = rng.choice(ids)
            for paths in ([], ["a.txt"], ["d"], ["g", "b.txt"]):
                for opts in ([], ["--full-history"], ["--topo-order"]):
                    for revs in ([a + ".." + b], [b, c, "^" + a]):
                        args = ["log", "--oneline", *opts, *revs] + (["--", *paths] if paths else [])
                        theirs = go.git(repo, "log", "--oneline", "--no-decorate", *opts, *revs, *(["--", *paths] if paths else [])).decode()
                        assert kgit(repo, *args) == theirs, (skew, k, args)
            graph_args = ["log", "--oneline", "--graph", "%s..%s" % (a, b), "--", "d"]
            assert kgit(repo, *graph_args) == go.git(repo, "log", "--oneline", "--no-decorate", "--graph", "%s..%s" % (a, b), "--", "d").decode(), (skew, k, graph_args)


def test_graph_with_patches_and_first_parent(tmp_path):
    rng = random.Random(55)
    repo = fresh(tmp_path, "g")
    ids = build_tree_dag(repo, rng, 30)
    go.git(repo, "update-ref", "refs/heads/top", ids[-1])
    for opts in (["--graph", "--stat"], ["--graph", "-p"], ["--graph", "--stat", "-p"], ["--graph", "--oneline", "--stat"], ["--first-parent", "-p"], ["--first-parent", "--stat", "--oneline"],
                 ["--graph", "--first-parent", "--oneline"]):
        for paths in ([], ["a.txt"], ["d"]):
            args = ["log", *opts, "top"] + (["--", *paths] if paths else [])
            assert kgit(repo, *args) == go.git(repo, "log", "--no-decorate", *opts, "top", *(["--", *paths] if paths else [])).decode(), args
