import os
import re
import shutil

import pytest

import gitoracle as go
from kendi_git.fsck import INFO_ONLY, check_commit, check_tag, check_tree, fsck
from kendi_git.repo import Repo

pytestmark = pytest.mark.skipif(go.GIT is None, reason="real git is not installed")

HEX = re.compile(r"\b[0-9a-f]{40}\b")


def git_fsck(repo, *extra):
    p = go.git(repo, "fsck", "--strict", "--no-reflogs", *extra, check=False)
    text = (p.stdout + p.stderr).decode(errors="replace")
    dangling = {tuple(line.split()[1:3]) for line in text.splitlines() if line.startswith("dangling ")}
    errors = [line for line in text.splitlines() if line and not line.startswith(("dangling ", "Checking", "notice"))]
    return p.returncode, errors, dangling


def ours(repo_path):
    return fsck(Repo(os.path.join(repo_path, ".git"), repo_path, env=go.env()))


@pytest.fixture
def copy(history_repo, tmp_path):
    path = str(tmp_path / "copy")
    shutil.copytree(history_repo, path, symlinks=True)
    return path


def loose_path(repo, oid):
    return os.path.join(repo, ".git", "objects", oid[:2], oid[2:])


def test_a_healthy_repository_is_clean_in_every_storage_state(copy):
    for stage in ("loose", "gc", "unpacked refs"):
        if stage == "gc":
            go.git(copy, "gc", "-q", "--aggressive", "--prune=now")
        report = ours(copy)
        rc, errors, dangling = git_fsck(copy)
        assert rc == 0 and not errors and report.ok, (stage, report.errors, errors)
        assert {(k, o) for k, o in report.dangling} == dangling, stage
    assert report.objects > 20


def test_dangling_objects_are_found_like_git_finds_them(copy):
    blob = go.gits(copy, "hash-object", "-w", "--stdin", input=b"nobody refers to me\n").strip()
    tree_in = go.gits(copy, "mktree", input=f"100644 blob {blob}\tx\n".encode()).strip()
    commit = go.gits(copy, "commit-tree", tree_in, "-m", "orphan", extra=go.commit_env(50)).strip()
    go.git(copy, "tag", "-d", "light")
    go.git(copy, "branch", "-D", "-f", "dummy", check=False)
    report = ours(copy)
    rc, errors, dangling = git_fsck(copy)
    assert rc == 0 and report.ok
    assert ("commit", commit) in dangling and ("blob", blob) not in dangling       # git names only the head of a dangling chain: the blob is used by the dangling tree
    assert set(report.dangling) == dangling


def damaged(copy, how):
    """Break a copy of the repository in a named way; returns the ids that are at fault."""
    blobs = go.gits(copy, "ls-tree", "-r", "HEAD").splitlines()
    blob = blobs[1].split()[2]
    if how == "missing blob":
        os.chmod(loose_path(copy, blob), 0o644)
        os.unlink(loose_path(copy, blob))
        return {blob}
    if how == "corrupt blob":
        path = loose_path(copy, blob)
        data = bytearray(open(path, "rb").read())
        data[len(data) // 2] ^= 0xFF
        os.chmod(path, 0o644)
        open(path, "wb").write(bytes(data))
        return {blob}
    if how == "swapped content":
        other = blobs[2].split()[2]
        os.chmod(loose_path(copy, blob), 0o644)
        shutil.copyfile(loose_path(copy, other), loose_path(copy, blob))     # a valid object stored under the wrong name
        return {blob}
    if how == "truncated":
        path = loose_path(copy, blob)
        data = open(path, "rb").read()
        os.chmod(path, 0o644)
        open(path, "wb").write(data[:len(data) // 2])
        return {blob}
    if how == "missing tree":
        tree = go.gits(copy, "rev-parse", "HEAD^{tree}").strip()
        os.chmod(loose_path(copy, tree), 0o644)
        os.unlink(loose_path(copy, tree))
        return {tree}
    raise AssertionError(how)


@pytest.mark.parametrize("how", ["missing blob", "corrupt blob", "swapped content", "truncated", "missing tree"])
def test_loose_object_damage_is_detected_and_pinpointed(copy, how):
    culprits = damaged(copy, how)
    report = ours(copy)
    rc, errors, _ = git_fsck(copy)
    assert rc != 0 and not report.ok, how
    git_ids = set(HEX.findall("\n".join(errors)))
    assert culprits <= git_ids, (how, errors)                  # git names the culprit ...
    assert culprits <= report.bad_objects, (how, report.errors)  # ... and so do we


def test_pack_damage_is_detected(copy):
    go.git(copy, "gc", "-q", "--prune=now")
    pack = next(os.path.join(copy, ".git", "objects", "pack", f) for f in os.listdir(os.path.join(copy, ".git", "objects", "pack")) if f.endswith(".pack"))
    data = bytearray(open(pack, "rb").read())
    data[len(data) // 2] ^= 0x55
    os.chmod(pack, 0o644)
    open(pack, "wb").write(bytes(data))
    report = ours(copy)
    assert go.git(copy, "fsck", "--strict", check=False).returncode != 0
    assert not report.ok


def literal_object(repo, kind, content):
    return go.gits(repo, "hash-object", "-w", "-t", kind, "--literally", "--stdin", input=content).strip()


def binary_tree(entries):
    return b"".join(m + b" " + n + b"\x00" + bytes.fromhex(o) for m, n, o in entries)


def test_malformed_trees_are_flagged_like_git_does(tmp_path):
    repo = str(tmp_path / "r")
    os.makedirs(repo)
    go.git(repo, "init", "-q")
    blob = go.gits(repo, "hash-object", "-w", "--stdin", input=b"x\n").strip()
    sub = go.gits(repo, "mktree", input=f"100644 blob {blob}\ty\n".encode()).strip()
    cases = {
        "unsorted": [(b"100644", b"b", blob), (b"100644", b"a", blob)],
        "duplicate": [(b"100644", b"a", blob), (b"100644", b"a", blob)],
        "dir sorts as name/": [(b"40000", b"a", sub), (b"100644", b"a.b", blob), (b"100644", b"a0", blob)],     # correct order: "a.b" < "a/" < "a0"
        "zero padded mode": [(b"040000", b"d", sub)],
        "bad mode": [(b"100664", b"f", blob)],
        "dot": [(b"100644", b".", blob)],
        "dotdot": [(b"100644", b"..", blob)],
        "dotgit": [(b"100644", b".git", blob)],
        "slash in name": [(b"100644", b"a/b", blob)],
        "fine": [(b"100644", b"a.b", blob), (b"40000", b"a", sub), (b"100644", b"a0", blob)],
    }
    for name, entries in cases.items():
        data = binary_tree(entries)
        problems = [p for p in check_tree(data)[0] if p[0] not in INFO_ONLY]
        probe = str(tmp_path / ("p_" + name.replace(" ", "_").replace("/", "_")))
        shutil.copytree(repo, probe)
        oid = literal_object(probe, "tree", data)
        go.git(probe, "update-ref", "refs/heads/t", go.gits(probe, "commit-tree", oid, "-m", "m", extra=go.commit_env(1)).strip())
        rc, errors, _ = git_fsck(probe)
        assert (rc != 0) == bool(problems), (name, problems, errors)


def test_malformed_commits_and_tags_are_flagged(tmp_path):
    repo = str(tmp_path / "r")
    os.makedirs(repo)
    go.git(repo, "init", "-q")
    tree = go.gits(repo, "mktree", input=b"").strip()
    ident = "A U Thor <a@example.org> 1700000000 +0000"
    good = f"tree {tree}\nauthor {ident}\ncommitter {ident}\n\nmsg\n".encode()
    cases = {
        "good": good,
        "no tree": f"author {ident}\ncommitter {ident}\n\nmsg\n".encode(),
        "bad tree": f"tree {tree[:-1]}z\nauthor {ident}\ncommitter {ident}\n\nmsg\n".encode(),
        "no author": f"tree {tree}\ncommitter {ident}\n\nmsg\n".encode(),
        "no committer": f"tree {tree}\nauthor {ident}\n\nmsg\n".encode(),
        "bad parent": f"tree {tree}\nparent nothex\nauthor {ident}\ncommitter {ident}\n\nmsg\n".encode(),
        "bad ident": f"tree {tree}\nauthor A U Thor a@example.org 1700000000 +0000\ncommitter {ident}\n\nmsg\n".encode(),
        "bad date": f"tree {tree}\nauthor A <a@x> notadate +0000\ncommitter {ident}\n\nmsg\n".encode(),
        "bad zone": f"tree {tree}\nauthor A <a@x> 1700000000 0000\ncommitter {ident}\n\nmsg\n".encode(),
    }
    for name, data in cases.items():
        problems = [p for p in check_commit(data)[0] if p[0] not in INFO_ONLY]
        probe = str(tmp_path / ("c_" + name.replace(" ", "_")))
        os.makedirs(probe)
        go.git(probe, "init", "-q")
        go.git(probe, "hash-object", "-w", "-t", "tree", "--stdin", input=b"")
        oid = literal_object(probe, "commit", data)
        go.git(probe, "update-ref", "refs/heads/t", oid, check=False)
        rc, errors, _ = git_fsck(probe)
        assert (rc != 0) == bool(problems), (name, problems, errors)
    tag_cases = {
        "good": f"object {tree}\ntype tree\ntag t\ntagger {ident}\n\nm\n".encode(),
        "no tagger": f"object {tree}\ntype tree\ntag t\n\nm\n".encode(),
        "bad type": f"object {tree}\ntype bogus\ntag t\ntagger {ident}\n\nm\n".encode(),
        "no tag line": f"object {tree}\ntype tree\ntagger {ident}\n\nm\n".encode(),
        "bad object": f"object zz\ntype tree\ntag t\ntagger {ident}\n\nm\n".encode(),
    }
    for name, data in tag_cases.items():
        problems = [p for p in check_tag(data)[0] if p[0] not in INFO_ONLY]
        probe = str(tmp_path / ("t_" + name.replace(" ", "_")))
        os.makedirs(probe)
        go.git(probe, "init", "-q")
        go.git(probe, "hash-object", "-w", "-t", "tree", "--stdin", input=b"")
        proc = go.git(probe, "hash-object", "-w", "-t", "tag", "--literally", "--stdin", input=data, check=False)
        if proc.returncode != 0:
            assert problems, (name, "git refuses to store it and we call it fine")
            continue
        go.git(probe, "update-ref", "refs/tags/t", proc.stdout.decode().strip(), check=False)
        rc, errors, _ = git_fsck(probe)
        assert (rc != 0) == bool(problems), (name, problems, errors)


def test_broken_refs_are_reported(copy):
    go.git(copy, "update-ref", "refs/heads/ghost", go.gits(copy, "rev-parse", "HEAD").strip())
    open(os.path.join(copy, ".git", "refs", "heads", "ghost"), "w").write("1" * 40 + "\n")
    report = ours(copy)
    assert not report.ok and any("ghost" in e for e in report.errors)
    assert go.git(copy, "fsck", "--strict", check=False).returncode != 0
