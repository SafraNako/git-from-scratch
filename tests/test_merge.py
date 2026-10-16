import os
import random
import shutil

import pytest

import gitoracle as go
from kendi_git import index as ix
from kendi_git import merge as mg
from kendi_git.diff import split_lines
from kendi_git.errors import GitError
from kendi_git.repo import Repo
from test_repo_history import build_dag, fresh, our_repo
from twin import edit_text

pytestmark = pytest.mark.skipif(go.GIT is None, reason="real git is not installed")


# --- merge bases -------------------------------------------------------------------------------------------------------------------------------------
def test_merge_bases_agree_with_git_on_random_dags(tmp_path):
    rng = random.Random(21)
    multi = 0
    for k in range(10):
        repo = fresh(tmp_path, f"mb{k}")
        ids = build_dag(repo, rng, 45)
        ours = our_repo(repo)
        for _ in range(10):
            a, b = rng.sample(ids, 2)
            theirs = sorted(go.gits(repo, "merge-base", "--all", a, b, check=False).split())
            assert mg.merge_bases(ours, a, b) == theirs, (a, b)
            multi += len(theirs) > 1
    assert multi >= 1, "the random DAGs should contain at least one criss-cross pair"


# --- the line merge -----------------------------------------------------------------------------------------------------------------------------------
def merge_file(tmp_path, base, ours, theirs):
    paths = []
    for name, data in (("ours", ours), ("base", base), ("theirs", theirs)):
        p = str(tmp_path / name)
        open(p, "wb").write(data)
        paths.append(p)
    proc = go.git(str(tmp_path), "merge-file", "-p", *paths, check=False)
    return proc.returncode, proc.stdout


def lines_of(rng, n, unique=True, alphabet=5):
    return [b"line-%d-%d\n" % (i, rng.randrange(10**6)) if unique else b"row %d\n" % rng.randrange(alphabet) for i in range(n)]


def edit_lines(rng, lines, count):
    lines = list(lines)
    for _ in range(count):
        pos = rng.randrange(len(lines) + 1)
        action = rng.random()
        if action < 0.3 and lines:
            del lines[min(pos, len(lines) - 1)]
        elif action < 0.65:
            lines.insert(pos, b"ins-%d\n" % rng.randrange(10**6))
        elif lines:
            lines[min(pos, len(lines) - 1)] = b"chg-%d\n" % rng.randrange(10**6)
    return lines


def test_line_merge_agrees_with_git_merge_file_on_distinct_lines(tmp_path):
    rng = random.Random(1)
    clean = conflicted = 0
    for _ in range(300):
        base = lines_of(rng, rng.randint(0, 30))
        ours, theirs = edit_lines(rng, base, rng.randint(0, 4)), edit_lines(rng, base, rng.randint(0, 4))
        code, out = merge_file(tmp_path, b"".join(base), b"".join(ours), b"".join(theirs))
        mine = mg.merge3(base, ours, theirs)
        if code == 0:
            assert mine is not None and b"".join(mine) == out, (base, ours, theirs)
            clean += 1
        else:
            assert mine is None, (base, ours, theirs, out)
            conflicted += 1
    assert clean > 60 and conflicted > 60                    # the generator reaches both outcomes often


def test_line_merge_on_repetitive_lines_measured_against_git(tmp_path):
    """With many equal lines several shortest edit scripts exist, and xdiff may choose another one than this code. The verdict (clean or conflicted) and the merged text
    are compared anyway; a small disagreement rate is the documented price, a large one would be a bug."""
    rng = random.Random(2)
    total = agree = 0
    for _ in range(300):
        base = lines_of(rng, rng.randint(0, 25), unique=False, alphabet=3)
        ours, theirs = edit_lines(rng, base, rng.randint(0, 3)), edit_lines(rng, base, rng.randint(0, 3))
        code, out = merge_file(tmp_path, b"".join(base), b"".join(ours), b"".join(theirs))
        mine = mg.merge3(base, ours, theirs)
        total += 1
        agree += (code == 0 and mine is not None and b"".join(mine) == out) or (code != 0 and mine is None)
    assert agree / total >= 0.98, f"only {agree}/{total} merges agree with git"
    print(f"repetitive-line merges agreeing with git: {agree}/{total}")


def test_identical_and_trivial_cases():
    a = [b"x\n", b"y\n", b"z\n"]
    assert mg.merge3(a, a, a) == a
    assert mg.merge3(a, [b"x\n", b"Y\n", b"z\n"], a) == [b"x\n", b"Y\n", b"z\n"]
    assert mg.merge3(a, [b"x\n", b"Y\n", b"z\n"], [b"x\n", b"Y\n", b"z\n"]) == [b"x\n", b"Y\n", b"z\n"]      # the same change on both sides
    assert mg.merge3(a, [b"x\n", b"Y\n", b"z\n"], [b"x\n", b"Q\n", b"z\n"]) is None                          # different changes to one line
    assert mg.merge3(a, [b"A\n", b"y\n", b"z\n"], [b"x\n", b"y\n", b"Z\n"]) == [b"A\n", b"y\n", b"Z\n"]      # far apart: both apply
    assert mg.merge3(a, [b"A\n", b"y\n", b"z\n"], [b"x\n", b"Y\n", b"z\n"]) is None                          # neighbouring lines touch: a conflict, as in xdiff
    assert mg.merge3([], [b"a\n"], [b"b\n"]) is None and mg.merge3([], [b"a\n"], [b"a\n"]) == [b"a\n"]


# --- tree merges, against git merge-tree -------------------------------------------------------------------------------------------------------------
def random_scenario(repo, rng, n):
    """Commit a base, then two diverging commits with random per-file changes; returns (base, a, b) commit ids."""
    names = ["f%d.txt" % i for i in range(5)] + ["d/g.txt", "d/h.txt", "link", "run.sh"]
    for name in names:
        if name == "link":
            os.symlink("f0.txt", os.path.join(repo, name))
        else:
            go.write(repo, name, edit_text(rng, b"") if name != "run.sh" else b"#!/bin/sh\necho hi\n" + edit_text(rng, b""))
    os.chmod(os.path.join(repo, "run.sh"), 0o755)
    go.git(repo, "add", "-A")
    go.git(repo, "commit", "-qm", "base", extra=go.commit_env(10 * n))
    base = go.gits(repo, "rev-parse", "HEAD").strip()
    commits = []
    for side in ("a", "b"):
        go.git(repo, "checkout", "-q", "--detach", base)
        for name in names + ["new1.txt", "new2.txt", "d/new3.txt"]:
            full = os.path.join(repo, name)
            action = rng.random()
            if action > 0.22:
                continue
            if name == "link":
                if os.path.lexists(full):
                    os.unlink(full)
                os.symlink(rng.choice(["f1.txt", "d"]), full)
            elif not os.path.exists(full):
                go.write(repo, name, edit_text(rng, b"") if rng.random() < 0.5 else b"same new file\n")
            elif action < 0.06:
                os.unlink(full)
            elif action < 0.16:
                go.write(repo, name, edit_text(rng, open(full, "rb").read()))
            elif action < 0.19:
                os.chmod(full, 0o755 if not os.access(full, os.X_OK) else 0o644)
            else:
                go.write(repo, name, b"identical edit on both sides\n")
        go.git(repo, "add", "-A")
        go.git(repo, "commit", "-qm", side, "--allow-empty", extra=go.commit_env(10 * n + 1 + (side == "b")))
        commits.append(go.gits(repo, "rev-parse", "HEAD").strip())
    return base, commits[0], commits[1]


needs_merge_tree = pytest.mark.skipif(go.GIT_VERSION < (2, 38), reason="git merge-tree --write-tree needs git 2.38")


@needs_merge_tree
def test_tree_merges_agree_with_git_merge_tree(tmp_path):
    rng = random.Random(3)
    repo = fresh(tmp_path, "mt")
    clean = conflicted = 0
    for n in range(1, 41):
        for entry in os.listdir(repo):
            if entry != ".git":
                full = os.path.join(repo, entry)
                os.unlink(full) if os.path.islink(full) or os.path.isfile(full) else shutil.rmtree(full)
        base, a, b = random_scenario(repo, rng, n)
        ours = our_repo(repo)
        proc = go.git(repo, "-c", "merge.renames=false", "merge-tree", "--write-tree", "--no-messages", a, b, check=False)
        files = lambda c: ours.tree_files(ours.read_commit(c).tree)  # noqa: E731
        merged, conflicts = mg.merge_trees(ours, files(base), files(a), files(b))
        if proc.returncode == 0:
            assert not conflicts, (n, conflicts)
            assert ix.write_tree(ours.odb, merged) == proc.stdout.decode().split()[0], n
            clean += 1
        else:
            assert conflicts, (n, "git reports a conflict, we found none")
            conflicted += 1
    assert clean >= 10 and conflicted >= 3, (clean, conflicted)


@needs_merge_tree
def test_merge_command_matches_git_for_a_clean_merge(tmp_path):
    rng = random.Random(8)
    for n in range(1, 40):
        git_dir = str(tmp_path / f"g{n}")
        os.makedirs(git_dir)
        go.git(git_dir, "init", "-q", "-b", "main")
        base, a, b = random_scenario(git_dir, rng, n)
        proc = go.git(git_dir, "-c", "merge.renames=false", "merge-tree", "--write-tree", "--no-messages", a, b, check=False)
        if proc.returncode != 0:
            continue
        our_dir = str(tmp_path / f"o{n}")
        shutil.copytree(git_dir, our_dir, symlinks=True)
        for d in (git_dir, our_dir):
            go.git(d, "checkout", "-q", "-B", "main", a)
        env = {**go.env(), "GIT_AUTHOR_DATE": "1800000000 +0000", "GIT_COMMITTER_DATE": "1800000000 +0000"}
        go.git(git_dir, "-c", "merge.renames=false", "merge", "-q", "--no-edit", "--no-ff", b, extra={"GIT_AUTHOR_DATE": "1800000000 +0000", "GIT_COMMITTER_DATE": "1800000000 +0000"})
        ours = Repo(os.path.join(our_dir, ".git"), our_dir, env=env)
        result = mg.merge(ours, b, ff="never")
        assert result.kind == "merge"
        assert result.oid == go.gits(git_dir, "rev-parse", "HEAD").strip(), n                  # the very same commit object
        assert go.git(our_dir, "status", "--porcelain").decode() == ""
        go.git(our_dir, "fsck", "--strict")
        return
    pytest.fail("no clean scenario found")


def test_conflicted_merge_changes_nothing(tmp_path):
    repo = str(tmp_path / "r")
    os.makedirs(repo)
    go.git(repo, "init", "-q", "-b", "main")
    go.write(repo, "f.txt", "one\ntwo\nthree\n")
    go.git(repo, "add", "f.txt")
    go.git(repo, "commit", "-qm", "base", extra=go.commit_env(1))
    go.git(repo, "checkout", "-qb", "side")
    go.write(repo, "f.txt", "one\nSIDE\nthree\n")
    go.git(repo, "commit", "-qam", "side", extra=go.commit_env(2))
    go.git(repo, "checkout", "-q", "main")
    go.write(repo, "f.txt", "one\nMAIN\nthree\n")
    go.git(repo, "commit", "-qam", "main", extra=go.commit_env(3))
    ours = Repo(os.path.join(repo, ".git"), repo, env=go.env())
    before = (open(os.path.join(repo, "f.txt")).read(), go.gits(repo, "rev-parse", "HEAD"), go.gits(repo, "status", "--porcelain"))
    with pytest.raises(mg.MergeConflict) as exc:
        mg.merge(ours, "side")
    assert exc.value.conflicts == ["f.txt"]
    assert (open(os.path.join(repo, "f.txt")).read(), go.gits(repo, "rev-parse", "HEAD"), go.gits(repo, "status", "--porcelain")) == before
    assert not os.path.exists(os.path.join(repo, ".git", "MERGE_HEAD"))
    assert go.git(repo, "merge", "side", check=False).returncode != 0                                # git agrees that this is a conflict


def test_refusals(tmp_path):
    repo = str(tmp_path / "r")
    os.makedirs(repo)
    go.git(repo, "init", "-q", "-b", "main")
    go.write(repo, "f", "1\n")
    go.git(repo, "add", "f")
    go.git(repo, "commit", "-qm", "one", extra=go.commit_env(1))
    go.git(repo, "checkout", "-q", "--orphan", "unrelated")
    go.write(repo, "g", "x\n")
    go.git(repo, "add", "g")
    go.git(repo, "commit", "-qm", "orphan", extra=go.commit_env(2))
    go.git(repo, "checkout", "-q", "main")
    ours = Repo(os.path.join(repo, ".git"), repo, env=go.env())
    with pytest.raises(GitError) as exc:
        mg.merge(ours, "unrelated")
    assert "unrelated histories" in str(exc.value)
    assert go.git(repo, "merge", "unrelated", check=False).returncode != 0
    assert mg.merge(ours, "main").kind == "up-to-date"
    with pytest.raises(GitError):
        mg.merge(ours, "no-such-branch")
    with pytest.raises(GitError):
        mg.merge(ours, "main", ff="bogus")


def test_split_lines_roundtrip_used_by_the_merge():
    data = b"a\nb\n\nc"
    assert b"".join(split_lines(data)) == data
