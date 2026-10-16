"""``kgit dogrulama``: a self-verification battery.

Known-answer checks (object ids computed with real git and written down here) run anywhere. When a real ``git`` is on the PATH the differential checks run too: each builds
a repository state, asks real git and this program the same question, and compares the answers (object bytes, index, status, history order, patches, ignore decisions,
merge results, corruption detection ...). Everything happens in a temporary directory with git's own configuration switched off.
"""
from __future__ import annotations

import os
import random
import re
import shutil
import subprocess
import tempfile
import time

from . import checkout as co
from . import diff as df
from . import fsck as fsck_mod
from . import index as ix
from . import merge as mg
from .config import ConfigFile
from .errors import GitError
from .objects import Commit, Ident, Tag, TreeEntry, hash_object, serialize_tree
from .pack import apply_delta
from .repo import Repo

GIT = shutil.which("git")
FIXED = {"GIT_AUTHOR_NAME": "A U Thor", "GIT_AUTHOR_EMAIL": "a@example.org", "GIT_COMMITTER_NAME": "A U Thor", "GIT_COMMITTER_EMAIL": "a@example.org"}


def _env(extra=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "HOME": tempfile.gettempdir(), "TZ": "UTC", "LC_ALL": "C", **FIXED})
    env.update(extra or {})
    return env


def git(cwd, *args, input=None, extra=None, check=True):
    p = subprocess.run([GIT, *args], cwd=cwd, input=input, capture_output=True, env=_env(extra))
    if check and p.returncode != 0:
        raise AssertionError("git %s failed: %s" % (" ".join(args), p.stderr.decode(errors="replace").strip()))
    return p.stdout if check else p


def gits(cwd, *args, **kw):
    out = git(cwd, *args, **kw)
    return (out if isinstance(out, bytes) else out.stdout).decode("utf-8", "surrogateescape")


def write(root, rel, data):
    full = os.path.join(root, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode())
    return full


def dated(n):
    stamp = "%d +0000" % (1700000000 + 100 * n)
    return {"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}


def unique_text(rng, previous=b"", lines=30):
    """Text whose lines are all distinct (so that its edit script is unique), or a small random edit of ``previous``."""
    rows = previous.split(b"\n")[:-1] if previous else [b"row-%d-%06d" % (i, rng.randrange(10**6)) for i in range(lines)]
    pos = rng.randrange(len(rows) + 1)
    if previous:
        action = rng.random()
        if action < 0.3 and rows:
            del rows[min(pos, len(rows) - 1)]
        elif action < 0.65:
            rows.insert(pos, b"new-%06d" % rng.randrange(10**6))
        elif rows:
            rows[min(pos, len(rows) - 1)] = b"chg-%06d" % rng.randrange(10**6)
    return b"\n".join(rows) + b"\n"


def new_repo(tmp, name):
    path = os.path.join(tmp, name)
    os.makedirs(path)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "core.ignorecase", "false")
    return path


def ours(path, n=0):
    return Repo(os.path.join(path, ".git"), path, env=_env(dated(n)), now=lambda: 1700000000 + 100 * n)


# --- known answers (no git needed) ------------------------------------------------------------------------------------------------------------------
def check_known_object_ids(tmp, rng, scale):
    assert hash_object("blob", b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"
    assert hash_object("blob", b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
    blob = "ce013625030ba8dba906f756967f9e9ca394464a"
    tree = hash_object("tree", serialize_tree([TreeEntry(0o100755, "run.sh", blob), TreeEntry(0o040000, "empty-dir", "4b825dc642cb6eb9a060e54bf8d69288fbee4904"),
                                                TreeEntry(0o100644, "hello.txt", blob)]))
    assert tree == "64ab9781fd27c3ee456726d0cd8a635f15b2e53a", tree
    who = Ident("A U Thor", "a@example.org", 1700000000, "+0000")
    first = hash_object("commit", Commit(tree, [], who, who, "initial\n").serialize())
    assert first == "9094eb9c6f3afe5351f587fe0c070750fa4e8d06", first
    later = Ident("A U Thor", "a@example.org", 1700003600, "+0100")
    second = hash_object("commit", Commit(tree, [first], who, later, "second\n\nbody text\n").serialize())
    assert second == "346168a56ac3fa883fc3ad927614d7771fc62793", second
    tag = hash_object("tag", Tag(second, "commit", "v1.0", Ident("A U Thor", "a@example.org", 1700007200, "+0000"), "release\n").serialize())
    assert tag == "0668189d5a6380d5b8e2dc3691c251dc208127cb", tag
    return "6 ids reproduced"


def check_known_delta(tmp, rng, scale):
    base = bytes(range(256)) * 4                                     # 1024 bytes
    # base size 1024 and result size 1030 as varints, "copy 1024 bytes from offset 0" (opcode 0xA0: only size byte 1 follows, 0x04 << 8), then "insert 6 bytes"
    delta = bytes([0x80, 0x08, 0x86, 0x08, 0xA0, 0x04, 0x06]) + b"suffix"
    assert apply_delta(base, delta) == base + b"suffix"
    for broken in (delta[:-1], delta + b"x", delta + bytes([0x91]), bytes([0x81, 0x08]) + delta[2:]):     # truncated, dangling insert, dangling copy, wrong base size
        try:
            apply_delta(base, broken)
        except GitError:
            continue
        raise AssertionError("a malformed delta was accepted")
    return "copy+insert delta applied, 4 malformed ones refused"


def check_known_names(tmp, rng, scale):
    from .refs import check_ref_format
    assert check_ref_format("refs/heads/main") and not check_ref_format("refs/heads/a..b") and not check_ref_format("refs/heads/x.lock")
    assert df.quote_path("ünï") == '"\\303\\274n\\303\\257"'
    return "name rules"


# --- differential checks (real git is the oracle) -------------------------------------------------------------------------------------------------
def check_loose_objects(tmp, rng, scale):
    repo = new_repo(tmp, "loose")
    odb = ours(repo).odb
    made = []
    for _ in range(15 * scale):
        data = bytes(rng.randrange(256) for _ in range(rng.randint(0, 600)))
        made.append((odb.write("blob", data), data))
    for oid, data in made:
        assert gits(repo, "cat-file", "-t", oid).strip() == "blob" and git(repo, "cat-file", "blob", oid) == data
        assert git(repo, "hash-object", "--stdin", input=data).decode().strip() == oid
    theirs = [(git(repo, "hash-object", "-w", "--stdin", input=b"git wrote %d" % i).decode().strip(), b"git wrote %d" % i) for i in range(8)]
    for oid, data in theirs:
        assert odb.read(oid) == ("blob", data)
    git(repo, "fsck", "--strict")
    return "%d objects both ways" % (len(made) + len(theirs))


def check_packfiles(tmp, rng, scale):
    repo = new_repo(tmp, "packs")
    names = ["a.txt", "b.txt", "d/c.txt"]
    for n in range(6 * scale):
        for name in names:
            full = os.path.join(repo, name)
            write(repo, name, unique_text(rng, open(full, "rb").read() if os.path.exists(full) else b"", 120))
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "c%d" % n, extra=dated(n))
    count = deltas = 0
    for label in ("delta chains (OFS)", "REF deltas"):
        if label.startswith("REF"):
            git(repo, "-c", "repack.useDeltaBaseOffset=false", "repack", "-adfq", "--window=50", "--depth=50")
        else:
            git(repo, "gc", "-q", "--aggressive", "--prune=now")
        odb = ours(repo).odb
        listing = gits(repo, "cat-file", "--batch-all-objects", "--batch-check").splitlines()
        assert len(listing) > 20 and any(odb.packs()), label
        for line in listing:
            oid, kind, _size = line.split()
            assert odb.read(oid) == (kind, git(repo, "cat-file", kind, oid)), (label, oid)
        verify = gits(repo, "verify-pack", "-v", *[p.idx_path for p in odb.packs()])
        count, deltas = len(listing), sum(1 for ln in verify.splitlines() if len(ln.split()) >= 7)
        assert deltas > 0, label
    return "%d objects (%d deltas), OFS and REF deltas read identically" % (count, deltas)


def check_index_and_trees(tmp, rng, scale):
    repo = new_repo(tmp, "index")
    names = ["z.txt", "a/b/c.txt", "a/b.txt", "a-b", "a.b", "ünï.txt", "dir with space/f", "run.sh", "x" * 120 + ".txt"]
    for name in names:
        write(repo, name, bytes(rng.randrange(256) for _ in range(rng.randint(0, 200))))
    os.chmod(os.path.join(repo, "run.sh"), 0o755)
    os.symlink("a-b", os.path.join(repo, "link"))
    mine = ours(repo)
    mine.add(["."])
    assert gits(repo, "diff-files", "--name-status") == "" and gits(repo, "write-tree").strip() == ix.index_to_tree(mine.odb, mine.read_index())
    listing = git(repo, "ls-files", "--stage", "-z").decode("utf-8", "surrogateescape").split("\x00")
    ours_listing = ["%06o %s %d\t%s" % (e.mode, e.oid, e.stage, e.path) for e in sorted(mine.read_index().entries.values(), key=ix.IndexEntry.key)] + [""]
    assert listing == ours_listing
    git(repo, "fsck", "--strict")
    return "%d index entries identical" % (len(listing) - 1)


def _status_z(path):
    return git(path, "status", "--porcelain=v1", "-z", "--no-renames", "-uall").decode("utf-8", "surrogateescape").split("\x00")


def _refs(path):
    return gits(path, "for-each-ref", "--format=%(refname) %(objectname)") + gits(path, "symbolic-ref", "-q", "HEAD", check=False)


class _Pair:
    """Two repositories, one driven through this program and one through git, receiving the same steps."""

    def __init__(self, tmp, name):
        self.a, self.b = new_repo(tmp, name + "-ours"), new_repo(tmp, name + "-git")
        self.mine = ours(self.a)
        self.n = 0

    def tick(self):
        self.n += 1
        self.mine.env, self.mine.now = _env(dated(self.n)), (lambda n=self.n: 1700000000 + 100 * n)
        self.mine.config = type(self.mine.config)(self.mine.gitdir, env=self.mine.env)

    def files(self, fn):
        for d in (self.a, self.b):
            fn(d)

    def op(self, label, call, *git_args):
        """Run ``call`` here and ``git <git_args>`` there; both must succeed or both must fail."""
        self.tick()
        try:
            call(self.mine)
            ok = True
        except GitError:
            ok = False
        theirs = git(self.b, *git_args, extra=dated(self.n), check=False).returncode == 0
        assert ok == theirs, "%s: ours=%s git=%s" % (label, ok, theirs)

    def agree(self, label):
        assert gits(self.a, "ls-files", "--stage") == gits(self.b, "ls-files", "--stage"), "index differs after %s" % label
        assert gits(self.a, "rev-parse", "-q", "--verify", "HEAD", check=False) == gits(self.b, "rev-parse", "-q", "--verify", "HEAD", check=False), "HEAD differs after %s" % label
        assert _refs(self.a) == _refs(self.b), "refs differ after %s" % label
        theirs = _status_z(self.b)
        assert _status_z(self.a) == theirs, "git's view of our repository differs after %s" % label
        s = self.mine.status()
        assert ["%s%s %s" % (x, y, p) for x, y, p in s.entries] + ["?? " + p for p in s.untracked] + [""] == theirs, "our status differs after %s" % label


def _tree_of(path):
    return {os.path.relpath(os.path.join(c, f), path): open(os.path.join(c, f), "rb").read() for c, _d, fs in os.walk(path) if ".git" not in c.split(os.sep) for f in fs}


def check_sessions(tmp, rng, scale):
    """The same random session through this program and through git; HEAD, index, refs, work tree and status must agree after every step."""
    steps = 0
    for session in range(scale):
        pair = _Pair(tmp, "session%d" % session)
        names = ["a.txt", "b.txt", "d/x.txt", "d/y.log", "run.sh", "sp ace.txt"]
        for step in range(24):
            op, name = rng.random(), rng.choice(names)
            if op < 0.35:
                path = os.path.join(pair.a, name)
                data = unique_text(rng, open(path, "rb").read() if os.path.exists(path) else b"")
                pair.files(lambda d: write(d, name, data))
                label = "edit " + name
            elif op < 0.42:
                pair.files(lambda d: os.unlink(os.path.join(d, name)) if os.path.exists(os.path.join(d, name)) else None)
                label = "delete " + name
            elif op < 0.60:
                pair.op("add", lambda r: r.add(["."]), "add", ".")
                label = "add ."
            elif op < 0.78:
                pair.op("commit", lambda r: r.commit("step %d" % step), "commit", "-q", "-m", "step %d" % step)
                label = "commit"
            elif op < 0.86:
                branch = rng.choice(["x", "y"])
                pair.op("branch", lambda r: r.create_branch(branch), "branch", "-q", branch)
                label = "branch " + branch
            else:
                branch = rng.choice(["x", "y", "main"])
                pair.op("checkout", lambda r: co.checkout(r, branch), "checkout", "-q", branch)
                label = "checkout " + branch
            pair.agree(label)
            steps += 1
        assert _tree_of(pair.a) == _tree_of(pair.b), "work trees differ"
    return "%d steps, state compared after each" % steps


def check_commit_identity(tmp, rng, scale):
    """Same files, same environment, so the very same commit id: tree, parents, author, committer, message cleaning."""
    done = 0
    for n in range(2 * scale):
        pair = _Pair(tmp, "identity%d" % n)
        for step in range(4):
            name = "f%d.txt" % rng.randrange(3)
            data = unique_text(rng, b"", 6)
            pair.files(lambda d: write(d, name, data))
            message = rng.choice(["plain", "two\n\nparagraphs  \n", "  lead\n\n\n\ntrail  \n\n", "# kept hash line\nbody"])
            pair.op("add", lambda r: r.add(["."]), "add", "-A")
            pair.op("commit", lambda r: r.commit(message), "commit", "-q", "-m", message)
            pair.agree("commit %d" % step)
            done += 1
    return "%d commits, each with the very same id as git's" % done


def check_history_order(tmp, rng, scale):
    repo = new_repo(tmp, "dag")
    tree = gits(repo, "hash-object", "-w", "-t", "tree", "--stdin", input=b"").strip()
    ids, when = [], 1700000000
    for i in range(30 * scale):
        parents = [rng.choice(ids[-8:] if rng.random() < 0.7 else ids)] if ids else []
        if ids and rng.random() < 0.25:
            other = rng.choice(ids)
            if other not in parents:
                parents.append(other)
        when += rng.choice([0, 0, 10, 60])
        args = ["commit-tree", tree, "-m", "c%d" % i] + [x for p in parents for x in ("-p", p)]
        ids.append(gits(repo, *args, extra={"GIT_AUTHOR_DATE": "%d +0000" % when, "GIT_COMMITTER_DATE": "%d +0000" % when}).strip())
    mine = ours(repo)
    for _ in range(6 * scale):
        tips = rng.sample(ids, rng.randint(1, 3))
        assert list(mine.walk(tips)) == gits(repo, "log", "--format=%H", *tips).split(), tips
        a, b = rng.sample(ids, 2)
        assert list(mine.walk([b], exclude=[a])) == gits(repo, "log", "--format=%H", "%s..%s" % (a, b)).split()
        assert mg.merge_bases(mine, a, b) == sorted(gits(repo, "merge-base", "--all", a, b, check=False).split())
    return "%d commits: log order, ranges and merge bases agree" % len(ids)


def check_patches(tmp, rng, scale):
    repo = new_repo(tmp, "patch")
    names = ["a.txt", "dir/b.txt", "sp ace/c d.txt", "ünï/d.txt", "e.py"]
    for name in names:
        write(repo, name, unique_text(rng, b"", 40))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base", extra=dated(1))
    for name in names:
        if rng.random() < 0.8:
            write(repo, name, unique_text(rng, open(os.path.join(repo, name), "rb").read()))
    write(repo, "added file.txt", unique_text(rng, b"", 5))
    mine = ours(repo)
    theirs = git(repo, "diff", "--no-color", "--no-renames")
    assert mine.diff_worktree() == theirs and len(theirs) > 50
    git(repo, "add", "-A")
    assert mine.diff_cached() == git(repo, "diff", "--no-color", "--no-renames", "--cached")
    return "%d bytes of patch identical" % len(theirs)


def check_ignore(tmp, rng, scale):
    from .ignore import IgnoreRules
    atoms = ["a", "b", "build", "tmp", ".txt", ".log", "keep", "*", "?", "**", "[ab]", "[!a]", "*.o"]
    total = 0
    for n in range(4 * scale):
        repo = new_repo(tmp, "ign%d" % n)
        lines = []
        for _ in range(rng.randint(2, 6)):
            pat = "/".join("".join(rng.choice(atoms) for _ in range(rng.randint(1, 2))) for _ in range(rng.randint(1, 3)))
            pat = ("**/" if rng.random() < 0.2 else "") + pat + ("/" if rng.random() < 0.3 else "")
            pat = ("!" if rng.random() < 0.2 else "") + ("/" if rng.random() < 0.2 else "") + pat
            lines.append(re.sub(r"(?<=[^/!])\*\*", "*", pat))      # "x**/" glued to text differs between git versions: keep clear of it
        write(repo, ".gitignore", "\n".join(lines) + "\n")
        files = {"/".join(rng.choice(["a", "b", "build", "tmp", "keep", "x.txt", "y.log", "z.o", "ab"]) for _ in range(rng.randint(1, 3))) for _ in range(20)}
        placed = []
        for f in sorted(files):
            if not any(g != f and g.startswith(f + "/") for g in files) and not any(f.startswith(p + "/") for p in placed):
                write(repo, f, b"x")
                placed.append(f)
        mine = ours(repo)
        rules = IgnoreRules(repo, os.path.join(repo, ".git"), mine.config, _env())
        theirs_ignored = set(gits(repo, "ls-files", "-o", "-i", "--exclude-standard", "-z").split("\x00")) - {""}
        mine_ignored = {f for f in placed + [".gitignore"] if rules.is_ignored(f, False)}
        assert mine_ignored == theirs_ignored, (lines, sorted(mine_ignored ^ theirs_ignored))
        total += len(placed)
    return "%d paths judged like git" % total


def check_merges(tmp, rng, scale):
    version = gits(tmp, "version").split()[2].split(".")
    if (int(version[0]), int(version[1])) < (2, 38):
        return "skipped: git older than 2.38 has no merge-tree --write-tree"
    clean = conflicted = 0
    repo = new_repo(tmp, "merge")
    for n in range(1, 6 * scale + 1):
        for entry in os.listdir(repo):
            if entry != ".git":
                full = os.path.join(repo, entry)
                shutil.rmtree(full) if os.path.isdir(full) and not os.path.islink(full) else os.unlink(full)
        names = ["f%d.txt" % i for i in range(5)] + ["d/g.txt"]
        for name in names:
            write(repo, name, unique_text(rng, b"", 10))
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "base", extra=dated(10 * n))
        base = gits(repo, "rev-parse", "HEAD").strip()
        commits = []
        for side in range(2):
            git(repo, "checkout", "-q", "--detach", base)
            for name in names:
                if rng.random() < 0.5:
                    write(repo, name, unique_text(rng, open(os.path.join(repo, name), "rb").read()))
            git(repo, "add", "-A")
            git(repo, "commit", "-qm", "side", "--allow-empty", extra=dated(10 * n + 1 + side))
            commits.append(gits(repo, "rev-parse", "HEAD").strip())
        mine = ours(repo)
        proc = git(repo, "-c", "merge.renames=false", "merge-tree", "--write-tree", "--no-messages", *commits, check=False)
        files = lambda c: mine.tree_files(mine.read_commit(c).tree)  # noqa: E731
        merged, conflicts = mg.merge_trees(mine, files(base), files(commits[0]), files(commits[1]))
        if proc.returncode == 0:
            assert not conflicts and ix.write_tree(mine.odb, merged) == proc.stdout.decode().split()[0]
            clean += 1
        else:
            assert conflicts
            conflicted += 1
    return "%d clean merges identical, %d conflicts agreed" % (clean, conflicted)


def check_corruption_detection(tmp, rng, scale):
    repo = new_repo(tmp, "fsck")
    for n in range(4):
        write(repo, "f%d.txt" % n, unique_text(rng, b"", 10))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "c", extra=dated(1))
    assert fsck_mod.fsck(ours(repo)).ok and git(repo, "fsck", "--strict", check=False).returncode == 0
    blob = gits(repo, "ls-tree", "HEAD").splitlines()[0].split()[2]
    path = os.path.join(repo, ".git", "objects", blob[:2], blob[2:])
    os.chmod(path, 0o644)
    data = bytearray(open(path, "rb").read())
    data[len(data) // 2] ^= 0xFF
    open(path, "wb").write(bytes(data))
    report = fsck_mod.fsck(ours(repo))
    assert not report.ok and blob in report.bad_objects
    assert git(repo, "fsck", "--strict", check=False).returncode != 0
    os.unlink(path)
    assert blob in fsck_mod.fsck(ours(repo)).bad_objects
    return "damage found by both"


def check_refs_and_config(tmp, rng, scale):
    repo = new_repo(tmp, "refs")
    write(repo, "f", "1")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "c", extra=dated(1))
    for n in range(8):
        git(repo, "branch", "b%d/%d" % (n % 3, n))
        git(repo, "tag", "-a", "-m", "t", "t%d" % n, extra=dated(2))
    git(repo, "pack-refs", "--all")
    git(repo, "branch", "loose")
    mine = ours(repo)
    expect = [tuple(line.split(" ")) for line in gits(repo, "for-each-ref", "--format=%(refname) %(objectname)").splitlines()]
    assert mine.refs.list("refs/") == expect
    cfg = ConfigFile(os.path.join(repo, ".git", "config"))
    cfg.set("user.name", 'Name with "quotes" # and ; marks')
    cfg.set("remote.origin.url", "https://example.org/a b.git")
    cfg.save()
    assert gits(repo, "config", "user.name").strip() == 'Name with "quotes" # and ; marks'
    assert gits(repo, "config", "remote.origin.url").strip() == "https://example.org/a b.git"
    return "%d refs listed like git, config round-trips" % len(expect)


KNOWN = [("bilinen nesne kimlikleri (blob, tree, commit, tag)", check_known_object_ids), ("el yapımı delta uygulaması", check_known_delta),
         ("ad kuralları", check_known_names)]
DIFFERENTIAL = [("gevşek nesneler: iki yönlü", check_loose_objects), ("paket dosyaları (delta zincirleri)", check_packfiles),
                ("indeks ve ağaç nesneleri", check_index_and_trees), ("rastgele oturumlar (add/commit/branch/checkout)", check_sessions),
                ("commit kimliği (aynı ortam, aynı nesne)", check_commit_identity), ("geçmiş sırası ve birleştirme tabanı", check_history_order),
                ("yama (diff) çıktısı", check_patches), ("gitignore kararları", check_ignore), ("ağaç birleştirme (merge-tree)", check_merges),
                ("bozulma tespiti (fsck)", check_corruption_detection), ("referanslar ve yapılandırma", check_refs_and_config)]


def run(out, quick=False, seed=1):
    """Run the battery, writing a report to ``out`` (anything with ``line``); returns 0 when every check passed."""
    scale = 1 if quick else 2
    results = []
    with tempfile.TemporaryDirectory(prefix="kgit-dogrulama-") as tmp:
        suites = [("Bilinen yanıtlar (git gerekmez)", KNOWN)]
        if GIT:
            suites.append(("Gerçek git ile karşılaştırma (%s)" % subprocess.run([GIT, "--version"], capture_output=True, text=True).stdout.strip(), DIFFERENTIAL))
        else:
            out.line("Not: PATH üzerinde git bulunamadı; karşılaştırmalı denetimler atlandı.")
        for title, checks in suites:
            out.line(title)
            for n, (name, fn) in enumerate(checks):
                rng = random.Random(seed * 1000 + n)
                workdir = tempfile.mkdtemp(dir=tmp)
                started = time.monotonic()
                try:
                    detail = fn(workdir, rng, scale)
                    results.append(True)
                    out.line("  ✓ %s: %s (%.1f sn)" % (name, detail, time.monotonic() - started))
                except Exception as exc:                     # a failed check is a result, not a crash
                    results.append(False)
                    out.line("  ✗ %s: %s: %s" % (name, type(exc).__name__, exc))
    passed = sum(results)
    out.line("%d/%d denetim başarılı." % (passed, len(results)))
    return 0 if passed == len(results) else 1


__all__ = ["run", "KNOWN", "DIFFERENTIAL"]
