import os
import random

import pytest

import gitoracle as go
from kendi_git.errors import GitError
from kendi_git.repo import Repo, format_date

pytestmark = pytest.mark.skipif(go.GIT is None, reason="real git is not installed")


def our_repo(path):
    return Repo(os.path.join(path, ".git"), path, env={**go.env(), "GIT_AUTHOR_DATE": "1700000000 +0000", "GIT_COMMITTER_DATE": "1700000000 +0000"})


def build_dag(repo, rng, n, skew=False, messages=None):
    """A random DAG of commits made with ``git commit-tree`` (so the objects are git's own); returns the ids, oldest first."""
    tree = go.gits(repo, "hash-object", "-w", "-t", "tree", "--stdin", input=b"").strip()
    ids, when = [], 1700000000
    for i in range(n):
        parents = []
        if ids:
            parents = [rng.choice(ids[-8:] if rng.random() < 0.7 else ids)]
            if rng.random() < 0.25 and len(ids) > 1:
                other = rng.choice(ids)
                if other not in parents:
                    parents.append(other)
        if skew:
            when = 1700000000 + rng.randrange(0, 40) * 10                     # arbitrary, repeated, out-of-order dates
        else:
            when += rng.choice([0, 0, 10, 10, 60])                             # non-decreasing, with many ties
        args = ["commit-tree", tree, "-m", messages[i] if messages else f"commit {i}"]
        for p in parents:
            args += ["-p", p]
        ids.append(go.gits(repo, *args, extra={"GIT_AUTHOR_DATE": f"{when} +0000", "GIT_COMMITTER_DATE": f"{when} +0000"}).strip())
    return ids


def fresh(tmp_path, name):
    repo = str(tmp_path / name)
    os.makedirs(repo)
    go.git(repo, "init", "-q")
    return repo


@pytest.mark.parametrize("skew", [False, True])
def test_default_log_order_matches_git_on_random_dags(tmp_path, skew):
    rng = random.Random(7 if skew else 8)
    for k in range(6):
        repo = fresh(tmp_path, f"dag{skew}{k}")
        ids = build_dag(repo, rng, 45, skew=skew)
        ours = our_repo(repo)
        for _ in range(6):
            tips = rng.sample(ids, rng.randint(1, 3))
            theirs = go.gits(repo, "log", "--format=%H", *tips).split()
            assert list(ours.walk(tips)) == theirs, (tips, skew)
        tip = ids[-1]
        assert list(ours.walk([tip], limit=7)) == go.gits(repo, "log", "--format=%H", "-n", "7", tip).split()


def test_ranges_and_exclusions_match_git_with_monotone_dates(tmp_path):
    rng = random.Random(9)
    for k in range(8):
        repo = fresh(tmp_path, f"rng{k}")
        ids = build_dag(repo, rng, 40)
        ours = our_repo(repo)
        for _ in range(8):
            a, b = rng.sample(ids, 2)
            assert list(ours.walk([b], exclude=[a])) == go.gits(repo, "log", "--format=%H", f"{a}..{b}").split()
            c = rng.choice(ids)
            assert list(ours.walk([b, c], exclude=[a])) == go.gits(repo, "log", "--format=%H", b, c, "^" + a).split()


def test_commit_formatting_matches_git_log(tmp_path):
    repo = fresh(tmp_path, "fmt")
    messages = ["subject only", "subject\n\nbody line one\nbody line two", "tabs\tand  trailing   \n\n\n   leading spaces\n\n\nlast", "ünï çödé 日本語\n\nbody ✓",
                "a\n\n  indented\n    more", "x"]
    rng = random.Random(3)
    ids = build_dag(repo, rng, len(messages), messages=messages)
    tree = go.gits(repo, "hash-object", "-w", "-t", "tree", "--stdin", input=b"").strip()
    env = {"GIT_AUTHOR_NAME": "Zoë Çelik", "GIT_AUTHOR_EMAIL": "z@example.org", "GIT_AUTHOR_DATE": "1700003600 +0530", "GIT_COMMITTER_DATE": "1700007200 -0800"}
    ids.append(go.gits(repo, "commit-tree", tree, "-p", ids[-1], "-p", ids[0], "-m", "a merge", extra=env).strip())
    ours = our_repo(repo)
    theirs = go.gits(repo, "log", "--no-color", "--no-decorate", "--date-order", ids[-1])
    mine = "\n".join(ours.format_commit(oid) for oid in ours.walk([ids[-1]]))
    # git indents with four spaces and separates entries with an empty line; commit-tree stores messages verbatim, ours must print them the same way
    assert mine == theirs
    assert go.gits(repo, "log", "--oneline", "--no-decorate", ids[-1]).splitlines() == [ours.format_commit(oid, oneline=True) for oid in ours.walk([ids[-1]])]


def test_date_formatting_matches_git_in_many_time_zones(tmp_path):
    repo = fresh(tmp_path, "dates")
    tree = go.gits(repo, "hash-object", "-w", "-t", "tree", "--stdin", input=b"").strip()
    ours = our_repo(repo)
    rng = random.Random(4)
    for tz in ["+0000", "-0800", "+0530", "+1245", "-0330", "+1400"]:
        for _ in range(4):
            when = rng.randrange(0, 2_000_000_000)
            oid = go.gits(repo, "commit-tree", tree, "-m", "m", extra={"GIT_AUTHOR_DATE": f"@{when} {tz}", "GIT_COMMITTER_DATE": f"@{when} {tz}"}).strip()
            theirs = [ln for ln in go.gits(repo, "log", "-1", "--no-decorate", oid).splitlines() if ln.startswith("Date:")][0][len("Date:   "):]
            assert format_date(ours.read_commit(oid).author) == theirs, (when, tz)


# --- revisions ------------------------------------------------------------------------------------------------------------------------------------
def test_rev_parse_agrees_with_git(history_repo, tmp_path):
    ours = our_repo(history_repo)
    head = go.gits(history_repo, "rev-parse", "HEAD").strip()
    specs = ["HEAD", "@", "main", "refs/heads/main", "heads/main", "v1", "light", "refs/tags/v1", "v1^{commit}", "v1^{tree}", "v1^{}", "v1^{tag}", "HEAD~1", "HEAD~3", "HEAD^", "HEAD^^",
             "HEAD^0", "HEAD~0", "HEAD^{tree}", "HEAD^{commit}", "HEAD~2^{tree}", "main~1^1", "HEAD:", "HEAD:src", head, head[:7], head[:12], head.upper()[:10], "v1~2", "light^"]
    # every path of the head tree, and the merge's second parent when present
    for path in go.gits(history_repo, "ls-tree", "-r", "--name-only", "-z", "HEAD").split("\x00")[:12]:
        if path:
            specs += ["HEAD:" + path, ":" + path]
    for spec in specs:
        proc = go.git(history_repo, "rev-parse", "--verify", "-q", spec, check=False)
        try:
            mine = ours.rev_parse(spec)
        except GitError:
            mine = None
        assert mine == (proc.stdout.decode().strip() or None), spec
    for spec in ["nonexistent", "HEAD~99", "HEAD^9", "HEAD:no/such/path", "v1^{blob}", "zzzz", "", "HEAD~", "^"]:
        proc = go.git(history_repo, "rev-parse", "--verify", "-q", spec, check=False)
        try:
            mine = ours.rev_parse(spec)
        except GitError:
            mine = None
        if spec in ("HEAD~",):
            continue                                   # "HEAD~" means HEAD~1 in both; covered below
        assert mine == (proc.stdout.decode().strip() or None), spec
    assert ours.rev_parse("HEAD~") == go.gits(history_repo, "rev-parse", "HEAD~").strip()
    merge = go.gits(history_repo, "rev-list", "--merges", "-n", "1", "HEAD").strip()
    assert merge, "the fixture history contains a merge"
    assert ours.rev_parse(merge + "^2") == go.gits(history_repo, "rev-parse", merge + "^2").strip()
    assert ours.rev_parse(merge + "^2~1") == go.gits(history_repo, "rev-parse", merge + "^2~1").strip()


def test_a_branch_named_like_a_short_id_wins_over_the_id(tmp_path):
    repo = fresh(tmp_path, "amb")
    rng = random.Random(1)
    ids = build_dag(repo, rng, 30)
    ours = our_repo(repo)
    target, short = ids[3], ids[10][:6]
    assert ours.rev_parse(short) == ids[10]
    go.git(repo, "update-ref", "refs/heads/" + short, target)
    assert ours.rev_parse(short) == target == go.gits(repo, "rev-parse", short).strip()


def test_ambiguous_short_ids_are_refused(tmp_path):
    repo = fresh(tmp_path, "amb2")
    rng = random.Random(2)
    ids = build_dag(repo, rng, 120)
    ours = our_repo(repo)
    by_prefix = {}
    for oid in ids:
        by_prefix.setdefault(oid[:2], []).append(oid)
    pair = next((v for v in by_prefix.values() if len(v) > 1), None)
    if pair is None:
        pytest.skip("no two commits share a two-character prefix in this run")
    common = os.path.commonprefix(pair[:2])
    if len(common) >= 4:
        with pytest.raises(GitError):
            ours.rev_parse(common)
        assert go.git(repo, "rev-parse", "--verify", "-q", common, check=False).returncode != 0
    assert len(ours.odb.abbrev(pair[0])) >= 7
    assert ours.odb.abbrev(pair[0]) == go.gits(repo, "rev-parse", "--short", pair[0]).strip()
