import os
import random
import subprocess
import zlib

import pytest

import gitoracle as go
from kendi_git import objects as ob
from kendi_git import pack as packmod
from kendi_git.errors import GitError
from kendi_git.odb import ObjectDB

pytestmark = pytest.mark.skipif(go.GIT is None, reason="real git is not installed")


def all_objects(repo):
    """``{oid: (type, size, content)}`` as real git reports them."""
    out = {}
    listing = go.gits(repo, "cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype) %(objectsize)").splitlines()
    for line in listing:
        oid, kind, size = line.split()
        content = go.git(repo, "cat-file", kind, oid)
        assert len(content) == int(size)
        out[oid] = (kind, int(size), content)
    return out


def compare_with_git(repo):
    truth = all_objects(repo)
    odb = ObjectDB(os.path.join(repo, ".git", "objects"))
    ours = {}
    for oid in odb.iter_oids():
        ours[oid] = odb.read(oid)
    assert set(ours) == set(truth)
    for oid, (kind, size, content) in truth.items():
        assert ours[oid] == (kind, content), oid
        assert ob.hash_object(kind, content) == oid                                    # and our hash of the content is the id git gave it
    return truth


def test_blob_ids_match_git_for_assorted_content():
    rng = random.Random(1)
    samples = [b"", b"\n", b"hello\n", b"hello", b"\x00\x01\x02", b"a" * 70000, bytes(range(256)) * 40, "ünïcode ✓\n".encode()] + [bytes(rng.randrange(256) for _ in range(rng.randint(0, 5000))) for _ in range(30)]
    for data in samples:
        theirs = subprocess.run([go.GIT, "hash-object", "--stdin"], input=data, capture_output=True, env=go.env()).stdout.decode().strip()
        assert ob.hash_object("blob", data) == theirs
    assert ob.hash_object("blob", b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"       # the famous id of the empty blob
    with pytest.raises(GitError):
        ob.hash_object("nonsense", b"")


def test_every_object_of_a_real_repository_reads_identically_loose(history_repo):
    truth = compare_with_git(history_repo)
    kinds = {k for k, _, _ in truth.values()}
    assert kinds == {"blob", "tree", "commit", "tag"} and len(truth) > 80


def test_every_object_reads_identically_from_a_gc_aggressive_pack_with_ofs_deltas(repo_copy):
    go.git(repo_copy, "gc", "--aggressive", "--prune=now", "-q")
    assert not [d for d in os.listdir(os.path.join(repo_copy, ".git", "objects")) if len(d) == 2]        # everything is packed now
    verify = go.gits(repo_copy, "verify-pack", "-v", *[os.path.join(repo_copy, ".git/objects/pack", f) for f in os.listdir(os.path.join(repo_copy, ".git/objects/pack")) if f.endswith(".idx")])
    deltas = [ln for ln in verify.splitlines() if len(ln.split()) == 7]
    assert deltas, "the test repository must contain delta objects, otherwise it proves nothing about delta resolution"
    compare_with_git(repo_copy)


def test_every_object_reads_identically_from_a_pack_with_ref_deltas(repo_copy):
    go.git(repo_copy, "-c", "repack.useDeltaBaseOffset=false", "repack", "-a", "-d", "-f", "-q", "--window=50", "--depth=50")
    go.git(repo_copy, "prune-packed")
    pack_dir = os.path.join(repo_copy, ".git", "objects", "pack")
    idx = next(os.path.join(pack_dir, f) for f in os.listdir(pack_dir) if f.endswith(".idx"))
    p = packmod.Pack(idx, idx[:-4] + ".pack")
    raw = open(idx[:-4] + ".pack", "rb").read()
    ref_deltas = sum(1 for oid in p if (raw[p.offset_of(oid)] >> 4) & 7 == packmod.REF_DELTA)
    assert ref_deltas > 0, "the repository must contain REF_DELTA objects"
    compare_with_git(repo_copy)
    p.close()


def test_a_packed_repository_with_mixed_loose_and_packed_objects(repo_copy):
    go.git(repo_copy, "repack", "-d", "-q")
    go.write(repo_copy, "later.txt", "added after the repack\n")
    go.git(repo_copy, "add", "-A")
    go.git(repo_copy, "commit", "-q", "-m", "after", extra=go.commit_env(99))
    odb = ObjectDB(os.path.join(repo_copy, ".git", "objects"))
    loose = [o for o in odb.iter_oids() if os.path.exists(odb._loose_path(o))]
    assert loose and odb.packs()
    compare_with_git(repo_copy)


def test_parse_and_serialise_round_trip_every_structured_object_of_a_real_repository(history_repo):
    truth = all_objects(history_repo)
    counts = {"tree": 0, "commit": 0, "tag": 0}
    for oid, (kind, _, content) in truth.items():
        if kind == "tree":
            assert ob.serialize_tree(ob.parse_tree(content)) == content
        elif kind == "commit":
            c = ob.Commit.parse(content)
            assert c.serialize() == content and ob.hash_object("commit", c.serialize()) == oid
        elif kind == "tag":
            assert ob.Tag.parse(content).serialize() == content
        else:
            continue
        counts[kind] += 1
    assert counts["commit"] >= 14 and counts["tree"] > 20 and counts["tag"] == 1


def test_commits_with_unusual_headers_keep_their_exact_bytes():
    raw = (b"tree 4b825dc642cb6eb9a060e54bf8d69288fbee4904\nparent " + b"1" * 40 + b"\nauthor A <a@b.c> 1 +0100\ncommitter C <c@d.e> 2 -0530\n"
           b"encoding ISO-8859-1\ngpgsig -----BEGIN PGP SIGNATURE-----\n \n abc\n def\n -----END PGP SIGNATURE-----\nmergetag object " + b"2" * 40 + b"\n type commit\n\n"
           b"merge message \xe9 not utf-8\n\nbody")
    c = ob.Commit.parse(raw)
    assert c.parents == ["1" * 40] and c.author.tz == "+0100" and c.committer.tz == "-0530" and [k for k, _ in c.extra] == ["encoding", "gpgsig", "mergetag"]
    assert "-----BEGIN PGP SIGNATURE-----\n\nabc\ndef\n-----END PGP SIGNATURE-----" in dict(c.extra)["gpgsig"]
    assert c.serialize() == raw
    assert ob.Commit.parse(b"tree " + b"a" * 40 + b"\nauthor A <a@b.c> 1 +0000\ncommitter A <a@b.c> 1 +0000\n").serialize().endswith(b"\n\n")   # a commit without any message


def test_tree_order_matches_gits_including_the_directory_slash_rule(tmp_path):
    repo = str(tmp_path / "r")
    os.makedirs(repo)
    go.git(repo, "init", "-q")
    blob = go.git(repo, "hash-object", "-w", "--stdin", input=b"x\n").decode().strip()
    names = [("a", True), ("a.b", False), ("a-b", False), ("a0", False), ("b", False), ("ab", True), ("a!", False), ("é", False), ("Z", False)]
    entries = []
    for name, is_dir in names:
        if is_dir:
            tree = go.git(repo, "mktree", input=f"100644 blob {blob}\tf\n".encode()).decode().strip()
            entries.append(ob.TreeEntry(ob.MODE_TREE, name, tree))
            go.git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{name}/f")
        else:
            entries.append(ob.TreeEntry(ob.MODE_FILE, name, blob))
            go.git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{name}")
    theirs = go.gits(repo, "write-tree").strip()
    mine = ob.hash_object("tree", ob.serialize_tree(entries[::-1]))
    assert mine == theirs                                                                   # the same order whatever order the entries come in
    listing = [e.name for e in ob.parse_tree(go.git(repo, "cat-file", "tree", theirs))]
    assert listing.index("a.b") < listing.index("a") < listing.index("a0") and listing.index("a-b") < listing.index("a.b")


def test_writing_loose_objects_git_can_read(tmp_path):
    repo = str(tmp_path / "r")
    os.makedirs(repo)
    go.git(repo, "init", "-q")
    odb = ObjectDB(os.path.join(repo, ".git", "objects"))
    blob = odb.write("blob", b"written by kgit\n")
    entries = [ob.TreeEntry(ob.MODE_FILE, "f.txt", blob), ob.TreeEntry(ob.MODE_EXEC, "run.sh", odb.write("blob", b"#!/bin/sh\n"))]
    tree = odb.write("tree", ob.serialize_tree(entries))
    ident = ob.Ident("Me", "me@example.org", 1700000000, "+0000")
    commit = odb.write("commit", ob.Commit(tree, [], ident, ident, "first\n").serialize())
    assert go.gits(repo, "cat-file", "-p", blob) == "written by kgit\n" and go.gits(repo, "cat-file", "-t", commit).strip() == "commit"
    assert go.gits(repo, "ls-tree", tree).splitlines() == [f"100644 blob {blob}\tf.txt", f"100755 blob {entries[1].oid}\trun.sh"]
    go.git(repo, "update-ref", "refs/heads/main", commit)
    go.git(repo, "fsck", "--strict", "--no-dangling")                                       # real git finds nothing wrong with what we wrote
    assert odb.write("blob", b"written by kgit\n") == blob and oct(os.stat(odb._loose_path(blob)).st_mode & 0o777) == "0o444"
    assert len([f for f in os.listdir(os.path.dirname(odb._loose_path(blob))) if f.startswith("tmp_obj")]) == 0


def test_object_name_resolution(history_repo):
    odb = ObjectDB(os.path.join(history_repo, ".git", "objects"))
    for oid in list(odb.iter_oids())[:40]:
        assert odb.resolve_prefix(oid[:12]) == oid == go.gits(history_repo, "rev-parse", oid[:12]).strip()
    with pytest.raises(GitError):
        odb.resolve_prefix("0000")
    with pytest.raises(GitError):
        odb.resolve_prefix("zz12")
    with pytest.raises(GitError):
        odb.resolve_prefix("ab")
    first_two = {}
    for oid in odb.iter_oids():
        first_two.setdefault(oid[:4], []).append(oid)
    clash = [p for p, v in first_two.items() if len(v) > 1]
    if clash:
        with pytest.raises(GitError) as e:
            odb.resolve_prefix(clash[0])
        assert "ambiguous" in str(e.value)


def test_delta_application():
    base = b"The quick brown fox jumps over the lazy dog. " * 5
    def varint(n):
        out = bytearray()
        while True:
            b = n & 0x7F
            n >>= 7
            out.append(b | (0x80 if n else 0))
            if not n:
                return bytes(out)
    # copy 20 bytes from offset 4, insert "!!", copy 10 bytes from offset 0
    delta = varint(len(base)) + varint(32) + bytes([0x80 | 0x01 | 0x10, 4, 20]) + bytes([2]) + b"!!" + bytes([0x80 | 0x10, 10])
    assert packmod.apply_delta(base, delta) == base[4:24] + b"!!" + base[:10]
    with pytest.raises(GitError):
        packmod.apply_delta(b"short", delta)                                                  # the base is not the size the delta expects
    with pytest.raises(GitError):
        packmod.apply_delta(base, varint(len(base)) + varint(99) + bytes([0x80 | 0x10, 10]))      # produces the wrong size
    with pytest.raises(GitError):
        packmod.apply_delta(base, varint(len(base)) + varint(1) + bytes([0]))                 # the reserved instruction
    big = bytes(range(256)) * 300
    d = varint(len(big)) + varint(0x10000) + bytes([0x80])                                    # a copy of size 0 means 0x10000
    assert packmod.apply_delta(big, d) == big[:0x10000]


def test_damaged_objects_are_reported_not_misread(tmp_path):
    repo = str(tmp_path / "r")
    os.makedirs(repo)
    go.git(repo, "init", "-q")
    odb = ObjectDB(os.path.join(repo, ".git", "objects"))
    oid = odb.write("blob", b"precious\n")
    path = odb._loose_path(oid)
    os.chmod(path, 0o644)
    good = open(path, "rb").read()
    open(path, "wb").write(good[:-3])
    with pytest.raises(GitError):
        odb.read(oid)
    open(path, "wb").write(zlib.compress(b"blob 99\x00short"))
    with pytest.raises(GitError) as e:
        odb.read(oid)
    assert "malformed" in str(e.value)
    open(path, "wb").write(zlib.compress(b"widget 5\x00hello"))
    with pytest.raises(GitError):
        odb.read(oid)
    with pytest.raises(GitError):
        ObjectDB(os.path.join(repo, ".git", "objects")).read("f" * 40)
