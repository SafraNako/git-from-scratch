import os
import random
import shutil
import struct

import pytest

import gitoracle as go
from kendi_git import index as ix
from kendi_git import objects as ob
from kendi_git.errors import GitError
from kendi_git.odb import ObjectDB

pytestmark = pytest.mark.skipif(go.GIT is None, reason="real git is not installed")


def ls_stage(repo):
    """git's own view of the index: ``[(mode, oid, stage, path)]``."""
    out = []
    for rec in go.git(repo, "ls-files", "--stage", "-z").split(b"\x00"):
        if rec:
            meta, path = rec.split(b"\t", 1)
            mode, oid, stage = meta.decode().split()
            out.append((int(mode, 8), oid, int(stage), os.fsdecode(path)))
    return out


def ours_view(index):
    return [(e.mode, e.oid, e.stage, e.path) for e in sorted(index.entries.values(), key=ix.IndexEntry.key)]


def test_index_written_by_real_git_is_read_exactly(history_repo):
    index = ix.Index.read(os.path.join(history_repo, ".git", "index"))
    assert ours_view(index) == ls_stage(history_repo) and len(index.entries) > 10 and index.version == 2
    modes = {e.mode for e in index.entries.values()}
    assert {ob.MODE_FILE, ob.MODE_EXEC, ob.MODE_LINK} <= modes                       # regular, executable and symlink entries all occur
    paths = [e.path for e in index.entries.values()]
    assert "ünïcode/файл.txt" in paths and "empty.txt" in paths


def parse_debug(text):
    """Parse ``git ls-files --debug -z``: ``path NUL`` then the stat lines (ending in ``flags: N``), directly followed by the next path."""
    pieces = text.split("\x00")
    out = {}
    path = pieces[0]
    for piece in pieces[1:]:
        head, _, rest = piece.partition("flags:")
        flags_line, _, next_path = rest.partition("\n")
        stat = dict(item.split(": ", 1) for line in (head + "flags:" + flags_line).split("\n") for item in line.strip().split("\t") if item)
        out[path] = stat
        path = next_path
    return out


def test_stat_data_agrees_with_gits_own_debug_output(history_repo):
    index = ix.Index.read(os.path.join(history_repo, ".git", "index"))
    debug = parse_debug(go.gits(history_repo, "ls-files", "--debug", "-z"))
    assert len(debug) == len(index.entries) > 10
    for path, fields in debug.items():
        e = index.entries[(path, 0)]
        assert int(fields["size"]) == e.size and int(fields["ino"]) == e.ino and int(fields["dev"]) == e.dev
        assert int(fields["uid"]) == e.uid and int(fields["gid"]) == e.gid
        assert fields["mtime"] == f"{e.mtime[0]}:{e.mtime[1]}" and fields["ctime"] == f"{e.ctime[0]}:{e.ctime[1]}"


def test_serialising_what_git_wrote_reproduces_the_file_byte_for_byte(history_repo):
    path = os.path.join(history_repo, ".git", "index")
    raw = open(path, "rb").read()
    index = ix.Index.read(path)
    rebuilt = index.serialize()
    # real git appends the cached-tree extension; everything before it (header + all entries) must be reproduced exactly
    assert rebuilt[:-20] == raw[:len(rebuilt) - 20]


def test_an_index_written_by_this_program_is_accepted_by_git(tmp_path):
    repo = str(tmp_path / "r")
    os.makedirs(repo)
    go.git(repo, "init", "-q")
    odb = ObjectDB(os.path.join(repo, ".git", "objects"))
    rng = random.Random(3)
    index = ix.Index()
    names = ["z.txt", "a/b/c.txt", "a/b.txt", "a-b", "a.b", "ünï.txt", "dir with space/f", "x" * 200 + ".txt", "run.sh"]
    for name in names:
        data = bytes(rng.randrange(256) for _ in range(rng.randint(0, 400)))
        full = go.write(repo, name, data)
        if name == "run.sh":
            os.chmod(full, 0o755)
        st = os.lstat(full)
        index.set(ix.IndexEntry(name, ix.mode_of(st), odb.write("blob", data), **ix.stat_fields(st)))
    index.write(os.path.join(repo, ".git", "index"))
    assert ls_stage(repo) == ours_view(index)
    assert go.gits(repo, "diff-files", "--name-status") == ""                                   # git sees no difference between the index and the files
    assert all(line[1] == " " for line in go.gits(repo, "status", "--porcelain", "-uno").splitlines())   # only staged additions (there is no commit yet)
    seen = parse_debug(go.gits(repo, "ls-files", "--debug", "-z"))
    for name in names:                                                                          # git reads the stat data we wrote exactly as we meant it
        e = index.entries[(name, 0)]
        assert (int(seen[name]["size"]), int(seen[name]["ino"]), seen[name]["mtime"]) == (e.size, e.ino, f"{e.mtime[0]}:{e.mtime[1]}")
    tree = ix.index_to_tree(odb, index)
    assert tree == go.gits(repo, "write-tree").strip()                                          # our tree built from our index is the tree git builds from it
    go.git(repo, "fsck", "--strict")
    assert not os.path.exists(os.path.join(repo, ".git", "index.lock"))


def test_long_paths_use_the_extended_name_length_rule(tmp_path):
    """Paths of 4095 bytes and more overflow the 12-bit length field; git stores them with the length found by scanning for the NUL. The paths cannot exist in the
    working tree (name limits), but the index can hold them, so they are planted with ``update-index --cacheinfo``."""
    repo = str(tmp_path / "r")
    os.makedirs(repo)
    go.git(repo, "init", "-q")
    blob = go.gits(repo, "hash-object", "-w", "--stdin", input=b"x\n").strip()
    stem = "/".join(["s" * 200] * 20)                                                  # 4019 bytes
    names = ["short.txt", "d/" + "n" * 100] + [stem + "/" + "u" * k for k in (73, 74, 75, 76, 77, 300)]
    for name in names:
        go.git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{name}")
    index = ix.Index.read(os.path.join(repo, ".git", "index"))
    assert sorted(e.path for e in index.entries.values()) == sorted(names) and ours_view(index) == ls_stage(repo)
    assert {len(n) for n in names} >= {4094, 4095, 4096}                                 # both sides of the 12-bit limit are covered
    raw = open(os.path.join(repo, ".git", "index"), "rb").read()
    assert index.serialize()[:-20] == raw[:len(index.serialize()) - 20]
    for _ in range(2):                                                               # and git accepts the file we wrote
        index.write(os.path.join(repo, ".git", "index"))
        assert ls_stage(repo) == ours_view(index)


def test_conflict_stages_and_unmerged_indexes_are_readable(tmp_path):
    repo = str(tmp_path / "r")
    os.makedirs(repo)
    go.git(repo, "init", "-q", "-b", "main")
    go.write(repo, "f.txt", "base\n")
    go.git(repo, "add", "-A")
    go.git(repo, "commit", "-q", "-m", "base")
    go.git(repo, "checkout", "-q", "-b", "other")
    go.write(repo, "f.txt", "other\n")
    go.git(repo, "commit", "-qam", "other", extra=go.commit_env(1))
    go.git(repo, "checkout", "-q", "main")
    go.write(repo, "f.txt", "main\n")
    go.git(repo, "commit", "-qam", "main", extra=go.commit_env(2))
    go.git(repo, "merge", "other", check=False)
    index = ix.Index.read(os.path.join(repo, ".git", "index"))
    assert sorted(e.stage for e in index.entries.values()) == [1, 2, 3] and ours_view(index) == ls_stage(repo)
    with pytest.raises(GitError):
        index.stage_zero()
    assert index.serialize()[:-20] == open(os.path.join(repo, ".git", "index"), "rb").read()[:len(index.serialize()) - 20]


def test_index_editing_rules():
    idx = ix.Index()
    e = lambda p: ix.IndexEntry(p, ob.MODE_FILE, "a" * 40)  # noqa: E731
    idx.set(e("a/b/c"))
    idx.set(e("a/d"))
    idx.set(e("a"))                                       # a file named "a" replaces the directory "a/"
    assert idx.paths() == ["a"]
    idx.set(e("a/b"))                                     # and a directory replaces the file
    assert idx.paths() == ["a/b"]
    idx.set(e("m"))
    assert idx.remove("m") and not idx.remove("m")
    z = ix.Index()
    for p in ["b", "a.b", "a/b", "a-b", "a0", "é"]:
        z.set(e(p))
    assert z.paths() == sorted(["b", "a.b", "a/b", "a-b", "a0", "é"], key=os.fsencode)


def test_corrupt_and_unsupported_indexes_are_refused(tmp_path):
    path = str(tmp_path / "index")
    good = ix.Index()
    good.set(ix.IndexEntry("f", ob.MODE_FILE, "b" * 40))
    good.write(path)
    raw = open(path, "rb").read()
    for broken, why in [(raw[:-1] + bytes([raw[-1] ^ 1]), "checksum"), (b"XXXX" + raw[4:], "signature"), (raw[:10], "signature")]:
        open(path, "wb").write(broken)
        with pytest.raises(GitError) as e:
            ix.Index.read(path)
        assert why in str(e.value)
    import hashlib
    v5 = raw[:4] + struct.pack(">I", 5) + raw[8:-20]
    open(path, "wb").write(v5 + hashlib.sha1(v5).digest())
    with pytest.raises(GitError) as e:
        ix.Index.read(path)
    assert "version 5" in str(e.value)
    assert ix.Index.read(str(tmp_path / "missing")).entries == {}


def test_the_lock_file_is_respected_and_released(tmp_path):
    path = str(tmp_path / "index")
    open(path + ".lock", "w").close()
    with pytest.raises(GitError) as e:
        ix.Index().write(path)
    assert "another process" in str(e.value)
    os.unlink(path + ".lock")
    ix.Index().write(path)
    assert os.path.exists(path) and not os.path.exists(path + ".lock")


def test_tree_flattening_and_building_agree_with_git(history_repo):
    odb = ObjectDB(os.path.join(history_repo, ".git", "objects"))
    head_tree = go.gits(history_repo, "rev-parse", "HEAD^{tree}").strip()
    flat = ix.flatten_tree(odb, head_tree)
    theirs = {f: (int(m, 8), o.decode()) for m, o, f in ((a, b, os.fsdecode(c)) for a, b, c in
              (rec.replace(b"\t", b" ").split(b" ", 3)[:1] + rec.replace(b"\t", b" ").split(b" ", 3)[2:3] + [rec.split(b"\t", 1)[1]] for rec in go.git(history_repo, "ls-tree", "-r", "-z", head_tree).split(b"\x00") if rec))}
    assert flat == theirs
    assert ix.write_tree(odb, flat) == head_tree                                            # rebuilding the tree from its flat listing gives the same id
    assert ix.write_tree(odb, {}) == "4b825dc642cb6eb9a060e54bf8d69288fbee4904"            # the empty tree


def test_index_version_4_with_prefix_compressed_paths_is_read(tmp_path, history_repo):
    repo = str(tmp_path / "v4")
    shutil.copytree(history_repo, repo, symlinks=True)
    go.git(repo, "update-index", "--index-version", "4")
    raw = open(os.path.join(repo, ".git", "index"), "rb").read()
    assert struct.unpack(">I", raw[4:8])[0] == 4
    index = ix.Index.read(os.path.join(repo, ".git", "index"))
    assert index.version == 4 and ours_view(index) == ls_stage(repo) and len(index.entries) > 10
    # writing produces a version 2 file that git reads just as well
    index.write(os.path.join(repo, ".git", "index"))
    assert ls_stage(repo) == ours_view(index)
    go.git(repo, "fsck", "--strict")
    assert go.gits(repo, "status", "--porcelain", "-uno") == ""
