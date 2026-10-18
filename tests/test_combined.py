"""Combined diffs (``show`` of a merge, ``log --cc`` / ``-c``) against git, on merges whose results mix the parents' lines in random ways."""
import io
import os
import random

import pytest

import gitoracle as go
from kendi_git import cli
from test_log_paths import write_tree
from test_repo_history import fresh

pytestmark = pytest.mark.skipif(go.GIT is None, reason="real git is not installed")

VOCAB = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta", "iota", "kappa", "lambda", "mu", "nu", "xi", "omicron", "pi", "rho", "sigma", "tau", "def func():",
         "    return 1", "}", "", "Header line", "_private", "$var", "    indented text"]
FILES = ["a.txt", "d/b.txt", "d/e/c.txt", "z.md"]


class Ident(dict):
    def __missing__(self, key):
        return key


def kgit(repo, *argv):
    out, err = io.BytesIO(), io.BytesIO()
    code = cli.main(list(argv), stdout=out, stderr=err, cwd=repo, env=go.env())
    assert code == 0, err.getvalue()
    return out.getvalue().decode()


def blob(repo, lines):
    return go.gits(repo, "hash-object", "-w", "--stdin", input="".join(l + "\n" for l in lines).encode()).strip()


def edit(rng, lines):
    lines = list(lines)
    for _ in range(rng.randint(1, 4)):
        kind = rng.random()
        if kind < 0.4 and lines:
            lines[rng.randrange(len(lines))] = rng.choice(VOCAB)
        elif kind < 0.7:
            lines.insert(rng.randint(0, len(lines)), rng.choice(VOCAB))
        elif lines:
            del lines[rng.randrange(len(lines))]
    return lines


def mix(rng, a, b):
    """A result built from chunks of both parents (and sometimes new lines)."""
    out, i, j = [], 0, 0
    while i < len(a) or j < len(b):
        take = rng.random()
        n = rng.randint(1, 4)
        if take < 0.4:
            out += a[i:i + n]
        elif take < 0.8:
            out += b[j:j + n]
        else:
            out.append(rng.choice(VOCAB))
        i += n if rng.random() < 0.8 else 0
        j += n if rng.random() < 0.8 else 0
        if rng.random() < 0.15:
            i, j = min(len(a), i + rng.randint(0, 2)), min(len(b), j + rng.randint(0, 2))
    return out


def build_content_dag(repo, rng, n):
    state, ids, when = {}, [], 1700000000
    for k in range(n):
        parents = []
        if ids:
            parents = [rng.choice(ids[-5:] if rng.random() < 0.7 else ids)]
            if rng.random() < 0.45 and len(ids) > 1:
                other = rng.choice(ids)
                if other not in parents:
                    parents.append(other)
        files = {p: list(v) for p, v in state[parents[0]].items()} if parents else {}
        if len(parents) > 1:
            for path in FILES:
                a, b = state[parents[0]].get(path), state[parents[1]].get(path)
                r = rng.random()
                if a is None and b is None:
                    continue
                if r < 0.15:
                    files.pop(path, None)
                elif r < 0.30 or b is None:
                    files[path] = list(a if a is not None else b)
                elif r < 0.45 or a is None:
                    files[path] = list(b)
                else:
                    files[path] = mix(rng, a, b)
        else:
            for path in rng.sample(FILES, rng.randint(1, 3)):
                files[path] = edit(rng, files.get(path, [rng.choice(VOCAB) for _ in range(rng.randint(3, 14))]))
        when += rng.choice([10, 10, 60])
        tree = write_tree(repo, {p: blob(repo, v) for p, v in files.items()}, Ident())
        args = ["commit-tree", tree, "-m", "commit %d" % k]
        for p in parents:
            args += ["-p", p]
        oid = go.gits(repo, *args, extra={"GIT_AUTHOR_DATE": "%d +0000" % when, "GIT_COMMITTER_DATE": "%d +0000" % when}).strip()
        ids.append(oid)
        state[oid] = files
    return ids, state


@pytest.mark.parametrize("seed", range(int(os.environ.get("KGIT_COMBINED_SEEDS", "6"))))
def test_combined_diffs_match_git(tmp_path, seed):
    rng = random.Random(1200 + seed)
    repo = fresh(tmp_path, "r")
    ids, _state = build_content_dag(repo, rng, 26 + 3 * seed)
    merges = [i for i in ids if len(go.gits(repo, "rev-list", "--parents", "-n1", i).split()) > 2]
    assert merges
    for oid in merges:
        assert kgit(repo, "show", oid) == go.git(repo, "show", "--no-decorate", oid).decode(), ("show", oid)
        assert kgit(repo, "show", "-c", oid) == go.git(repo, "show", "--no-decorate", "-c", oid).decode(), ("show -c", oid)
    go.git(repo, "update-ref", "refs/heads/top", ids[-1])
    for opts in (["--cc"], ["-c"], ["--cc", "-U1"], ["--cc", "--oneline"]):
        assert kgit(repo, "log", *opts, "top") == go.git(repo, "log", "--no-decorate", *opts, "top").decode(), opts
    assert kgit(repo, "log", "--cc", "top", "--", "a.txt") == go.git(repo, "log", "--no-decorate", "--cc", "top", "--", "a.txt").decode()


def test_separate_diffs_for_every_parent_of_a_merge(tmp_path):
    rng = random.Random(222)
    repo = fresh(tmp_path, "m")
    ids, _state = build_content_dag(repo, rng, 24)
    go.git(repo, "update-ref", "refs/heads/top", ids[-1])
    for opts in (["-m", "-p"], ["-m", "-p", "--oneline"], ["-m", "--stat"], ["-m", "--stat", "-p"], ["-m"]):
        assert kgit(repo, "log", *opts, "top") == go.git(repo, "log", "--no-decorate", *opts, "top").decode(), opts
