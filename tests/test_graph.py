"""``log --graph`` and the topological orders against git, on random commit graphs."""
import io
import os
import random

import pytest

import gitoracle as go
from kendi_git import cli
from test_repo_history import build_dag, fresh

pytestmark = pytest.mark.skipif(go.GIT is None, reason="real git is not installed")


def kgit(repo, *argv):
    out, err = io.BytesIO(), io.BytesIO()
    code = cli.main(list(argv), stdout=out, stderr=err, cwd=repo, env=go.env())
    assert code == 0, err.getvalue()
    return out.getvalue().decode()


def make_dag(tmp_path, name, rng, n, octopus=False, skew=False):
    repo = fresh(tmp_path, name)
    ids = build_dag(repo, rng, n, skew=skew)
    if octopus:                                           # a few merges with three or four parents
        tree = go.gits(repo, "hash-object", "-w", "-t", "tree", "--stdin", input=b"").strip()
        for k in range(3):
            parents = rng.sample(ids, rng.randint(3, 4))
            args = ["commit-tree", tree, "-m", f"octopus {k}"] + [x for p in parents for x in ("-p", p)]
            ids.append(go.gits(repo, *args, extra={"GIT_AUTHOR_DATE": f"{1700001000 + 100 * k} +0000", "GIT_COMMITTER_DATE": f"{1700001000 + 100 * k} +0000"}).strip())
    for i, oid in enumerate(ids[-6:]):
        go.git(repo, "update-ref", f"refs/heads/b{i}", oid)
    return repo, ids


@pytest.mark.parametrize("order", ["--topo-order", "--date-order"])
def test_topological_orders_match_git(tmp_path, order):
    rng = random.Random(41)
    for k in range(5):
        repo, ids = make_dag(tmp_path, f"t{order[2:6]}{k}", rng, 50, octopus=True)
        tips = ["b0", "b2", "b4"]
        mine = kgit(repo, "log", "--oneline", order, *tips).split("\n")[:-1]
        theirs = go.gits(repo, "log", "--oneline", "--no-decorate", order, *tips).splitlines()
        assert mine == theirs, (order, k)


@pytest.mark.parametrize("seed", range(int(os.environ.get("KGIT_GRAPH_SEEDS", "6"))))
def test_oneline_graph_matches_git(tmp_path, seed):
    rng = random.Random(100 + seed)
    repo, ids = make_dag(tmp_path, f"g{seed}", rng, 40 + 5 * seed, octopus=seed % 2 == 1)
    for tips in (["b5"], ["b0", "b3", "b5"], ["b1", "b2"]):
        mine = kgit(repo, "log", "--oneline", "--graph", *tips)
        theirs = go.git(repo, "log", "--oneline", "--no-decorate", "--graph", *tips).decode()
        assert mine == theirs, (seed, tips)


@pytest.mark.parametrize("seed", range(4))
def test_medium_format_graph_matches_git(tmp_path, seed):
    rng = random.Random(300 + seed)
    messages = ["subject %d" % i if i % 3 else "subject %d\n\nbody line one\nbody line two" % i for i in range(45)]
    repo = fresh(tmp_path, f"m{seed}")
    ids = build_dag(repo, rng, 45, messages=messages)
    for i, oid in enumerate(ids[-6:-1]):
        go.git(repo, "update-ref", f"refs/heads/b{i}", oid)
    for tips in (["b0"], ["b0", "b2", "b4"]):
        assert kgit(repo, "log", "--graph", *tips) == go.git(repo, "log", "--no-color", "--no-decorate", "--graph", *tips).decode(), (seed, tips)
    assert kgit(repo, "log", "--graph", "-n", "4", "b1") == go.git(repo, "log", "--no-color", "--no-decorate", "--graph", "-n", "4", "b1").decode()
    assert kgit(repo, "log", "--graph", "--stat", "b0") == go.git(repo, "log", "--no-color", "--no-decorate", "--graph", "--stat", "b0").decode()
