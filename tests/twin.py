"""Two repositories driven in lock step: ours through the Python API, real git through its command line. After every step their states must agree."""
import os

import gitoracle as go
from kendi_git import checkout as co
from kendi_git import merge as mg
from kendi_git.errors import GitError
from kendi_git.repo import Repo, Pathspec

NAMES = ["a.txt", "b.txt", "d/x.txt", "d/y.log", "d/e/z.txt", "bin.dat", "e.sh", "sp ace/f.txt", "ünï.txt", "link", "d/link2", "q/r/s.txt", "tmp/out.log"]
PATTERNS = ["*.log", "d/", "/b.txt", "!d/y.log", "tmp/", "*.dat", "q/**/s.*", "ünï*", "!keep.log"]


class Twin:
    def __init__(self, tmp_path, name="twin", ignorecase="false"):
        self.ours_dir = str(tmp_path / (name + "_ours"))
        self.theirs_dir = str(tmp_path / (name + "_theirs"))
        for d in (self.ours_dir, self.theirs_dir):
            os.makedirs(d)
        self.step = 0
        go.git(self.theirs_dir, "init", "-q", "-b", "main")
        go.git(self.theirs_dir, "config", "core.ignorecase", ignorecase)
        self.ours = Repo.init(self.ours_dir, env=self.env(), now=lambda: 1700000000 + 100 * self.step)
        go.git(self.ours_dir, "config", "core.ignorecase", ignorecase)      # git's own setting command: shares the config file format with ours
        self.ours.config = type(self.ours.config)(self.ours.gitdir, env=self.env())

    def env(self):
        stamp = f"{1700000000 + 100 * self.step} +0000"
        return {**go.env(), "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}

    # --- file level changes, identical on both sides ------------------------------------------------------------------------------------------
    def each(self, fn):
        for d in (self.ours_dir, self.theirs_dir):
            fn(d)

    def write(self, rel, content):
        def do(d):
            full = os.path.join(d, rel)
            if os.path.islink(full):
                os.unlink(full)
            if os.path.isdir(full):
                import shutil
                shutil.rmtree(full)
            parent = os.path.dirname(full)
            if os.path.isfile(parent) or os.path.islink(parent):
                os.unlink(parent)
            go.write(d, rel, content)
        self.each(do)

    def delete(self, rel):
        def do(d):
            full = os.path.join(d, rel)
            if os.path.islink(full) or os.path.isfile(full):
                os.unlink(full)
        self.each(do)

    def chmod(self, rel, executable):
        def do(d):
            full = os.path.join(d, rel)
            if os.path.isfile(full) and not os.path.islink(full):
                os.chmod(full, 0o755 if executable else 0o644)
        self.each(do)

    def symlink(self, rel, target):
        def do(d):
            full = os.path.join(d, rel)
            os.makedirs(os.path.dirname(full) or d, exist_ok=True)
            if os.path.lexists(full):
                if os.path.isdir(full) and not os.path.islink(full):
                    import shutil
                    shutil.rmtree(full)
                else:
                    os.unlink(full)
            os.symlink(target, full)
        self.each(do)

    # --- repository level operations: (ours ok?, theirs ok?) ---------------------------------------------------------------------------------
    def both(self, ours_call, *git_args, input=None):
        self.step += 1
        self.ours.env = self.env()
        self.ours.config = type(self.ours.config)(self.ours.gitdir, env=self.env())
        try:
            ours_call()
            ours_ok = True
        except GitError:
            ours_ok = False
        proc = go.git(self.theirs_dir, *git_args, input=input, extra=self.env(), check=False)
        self.last_stderr = proc.stderr.decode(errors="replace").strip() + proc.stdout.decode(errors="replace").strip()
        return ours_ok, proc.returncode == 0

    def add(self, *specs, update=False, force=False):
        args = ["add"] + (["-u"] if update else []) + (["-f"] if force else []) + ["--"] + list(specs)
        return self.both(lambda: self.ours.add(list(specs), update=update, force=force), *args)

    def rm(self, *specs, cached=False, force=False, recursive=False):
        args = ["rm", "-q"] + (["--cached"] if cached else []) + (["-f"] if force else []) + (["-r"] if recursive else []) + ["--"] + list(specs)
        return self.both(lambda: self.ours.rm(list(specs), cached=cached, force=force, recursive=recursive), *args)

    def commit(self, message, allow_empty=False, amend=False):
        args = ["commit", "-q", "-m", message] + (["--allow-empty"] if allow_empty else []) + (["--amend"] if amend else [])
        return self.both(lambda: self.ours.commit(message, amend=amend, allow_empty=allow_empty), *args)

    def checkout(self, target, create=None, detach=False):
        args = ["checkout", "-q"] + (["-b", create] if create else []) + (["--detach"] if detach else []) + [target]
        return self.both(lambda: co.checkout(self.ours, target, create=create, detach=detach), *args)

    def reset(self, target, mode):
        return self.both(lambda: co.reset(self.ours, target, mode), "reset", "-q", "--" + mode, target)

    def branch(self, name, start=None, delete=False, force_delete=False):
        if delete:
            return self.both(lambda: self.ours.delete_branch(name, force=force_delete), "branch", "-q", "-D" if force_delete else "-d", name)
        args = ["branch", "-q", name] + ([start] if start else [])
        return self.both(lambda: self.ours.create_branch(name, start), *args)

    def tag(self, name, target=None, message=None):
        args = ["tag"] + (["-a", "-m", message] if message else []) + [name] + ([target] if target else [])
        return self.both(lambda: self.ours.create_tag(name, target, message), *args)

    def merge(self, spec, ff="allow"):
        flags = {"allow": [], "never": ["--no-ff"], "only": ["--ff-only"]}[ff]
        ok = self.both(lambda: mg.merge(self.ours, spec, ff=ff), "-c", "merge.renames=false", "-c", "diff.renames=false", "merge", "-q", "--no-edit", *flags, spec)
        if not ok[1] and os.path.exists(os.path.join(self.theirs_dir, ".git", "MERGE_HEAD")):
            go.git(self.theirs_dir, "merge", "--abort", check=False)           # real git leaves conflicts in the work tree; ours refuses and leaves nothing behind
        return ok

    # --- comparisons -------------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def stage_listing(d):
        return go.git(d, "ls-files", "--stage", "-z").decode("utf-8", "surrogateescape").split("\x00")

    @staticmethod
    def status_of(d):
        return go.git(d, "status", "--porcelain=v1", "-z", "--no-renames", "-uall").decode("utf-8", "surrogateescape").split("\x00")

    def our_status_z(self, untracked="all"):
        s = self.ours.status(untracked=untracked)
        return ["%s%s %s" % (x, y, p) for x, y, p in s.entries] + ["?? " + p for p in s.untracked] + [""]

    @staticmethod
    def status_normal(d):
        """git's default mode: untracked directories are collapsed to ``dir/``."""
        return go.git(d, "status", "--porcelain=v1", "-z", "--no-renames").decode("utf-8", "surrogateescape").split("\x00")

    def head(self, d):
        p = go.git(d, "rev-parse", "-q", "--verify", "HEAD", check=False)
        return p.stdout.decode().strip() or None

    @staticmethod
    def tree_snapshot(d):
        """Every file below ``d`` outside ``.git``: relative path -> (kind, exec bit, content or link target)."""
        out = {}
        for cur, dirs, files in os.walk(d):
            dirs[:] = [x for x in dirs if x != ".git"]
            for name in dirs + files:
                full = os.path.join(cur, name)
                rel = os.path.relpath(full, d)
                if os.path.islink(full):
                    out[rel] = ("link", False, os.readlink(full))
                elif os.path.isdir(full):
                    out[rel] = ("dir", False, None)
                else:
                    out[rel] = ("file", bool(os.stat(full).st_mode & 0o111), open(full, "rb").read())
        return out

    @staticmethod
    def refs_of(d):
        return go.git(d, "for-each-ref", "--format=%(refname) %(objectname) %(objecttype)").decode().splitlines() + [go.git(d, "symbolic-ref", "-q", "HEAD", check=False).stdout.decode().strip()]

    def assert_same(self, label="", worktree=True):
        assert self.refs_of(self.ours_dir) == self.refs_of(self.theirs_dir), f"refs differ {label}: {self.refs_of(self.ours_dir)} vs {self.refs_of(self.theirs_dir)}"
        if worktree:
            ours_tree, theirs_tree = self.tree_snapshot(self.ours_dir), self.tree_snapshot(self.theirs_dir)
            # empty directories are not tracked by git: the two sides may keep different leftovers
            keep = lambda t: {k: v for k, v in t.items() if v[0] != "dir"}  # noqa: E731
            assert keep(ours_tree) == keep(theirs_tree), f"work trees differ {label}: {sorted(set(keep(ours_tree).items()) ^ set(keep(theirs_tree).items()))[:4]}"
        assert self.stage_listing(self.ours_dir) == self.stage_listing(self.theirs_dir), f"index differs {label}"
        assert self.head(self.ours_dir) == self.head(self.theirs_dir), f"HEAD differs {label}"
        theirs = self.status_of(self.theirs_dir)
        assert self.status_of(self.ours_dir) == theirs, f"git's view of our repo differs from its own twin {label}"
        assert self.our_status_z() == theirs, f"our status differs from git's {label}: {self.our_status_z()} vs {theirs}"
        assert self.our_status_z("normal") == self.status_normal(self.theirs_dir), f"our collapsed status differs from git's {label}"


def random_content(rng):
    kind = rng.random()
    if kind < 0.1:
        return b""
    if kind < 0.2:
        return bytes(rng.randrange(256) for _ in range(rng.randint(1, 40))) + b"\x00"
    return ("line %d\n" % rng.randrange(5) * rng.randint(1, 4)).encode()


def random_step(twin, rng):
    """Apply one random operation to both repositories; returns a description."""
    op = rng.random()
    name = rng.choice(NAMES)
    if op < 0.22:
        twin.write(name, random_content(rng))
        return f"write {name}"
    if op < 0.30:
        twin.delete(name)
        return f"delete {name}"
    if op < 0.35:
        twin.chmod(name, rng.random() < 0.5)
        return f"chmod {name}"
    if op < 0.40:
        twin.symlink(name, rng.choice(["a.txt", "../x", "d"]))
        return f"symlink {name}"
    if op < 0.44:
        text = "\n".join(rng.choice(PATTERNS) for _ in range(rng.randint(1, 3))) + "\n"
        where = rng.choice([".gitignore", "d/.gitignore"])
        twin.write(where, text.encode())
        return f"write {where}"
    if op < 0.62:
        spec = rng.choice([".", name, "d", "*.txt", "d/*", "q", "nope.zzz", "tmp/out.log", "*.log"])
        res = twin.add(spec, update=rng.random() < 0.2, force=rng.random() < 0.15)
        assert res[0] == res[1], f"add {spec} outcome differs: ours={res[0]} git={res[1]} ({twin.last_stderr})"
        return f"add {spec} -> {res}"
    if op < 0.70:
        spec = rng.choice([name, "d", "*.txt", "q/r"])
        res = twin.rm(spec, cached=rng.random() < 0.4, force=rng.random() < 0.2, recursive=rng.random() < 0.6)
        assert res[0] == res[1], f"rm {spec} outcome differs: ours={res[0]} git={res[1]} ({twin.last_stderr})"
        return f"rm {spec} -> {res}"
    res = twin.commit("step %d" % twin.step, allow_empty=rng.random() < 0.1, amend=rng.random() < 0.1)
    assert res[0] == res[1], f"commit outcome differs: ours={res[0]} git={res[1]} ({twin.last_stderr})"
    return f"commit -> {res}"


def edit_text(rng, previous):
    """A small edit of (mostly unique-line) text so that two branches can touch different places of one file."""
    lines = previous.split(b"\n")[:-1] if previous else []
    if not lines or rng.random() < 0.03:
        lines = [b"row-%d-%06d" % (i, rng.randrange(10**6)) for i in range(rng.randint(25, 45))]
    else:
        pos = rng.randrange(len(lines) + 1)
        action = rng.random()
        if action < 0.3 and lines:
            del lines[min(pos, len(lines) - 1)]
        elif action < 0.65:
            lines.insert(pos, b"new-%06d" % rng.randrange(10**6))
        elif lines:
            lines[min(pos, len(lines) - 1)] = b"chg-%06d" % rng.randrange(10**6)
    return b"\n".join(lines) + b"\n"


BRANCHES = ["main", "feat", "side", "x/y"]
FILES = NAMES[:6]


def seed_files(twin, rng):
    """A first commit holding every file the random edits touch, so that branches modify existing files instead of adding them independently."""
    for name in FILES:
        twin.write(name, edit_text(rng, b""))
    assert twin.add(".") == (True, True)
    assert twin.commit("seed files") == (True, True)


def _same(res, what, twin):
    assert res[0] == res[1], f"{what} outcome differs: ours={res[0]} git={res[1]} ({twin.last_stderr})"
    return f"{what} -> {res}"


def _edit(twin, rng, name=None):
    name = name or rng.choice(FILES)
    full = os.path.join(twin.ours_dir, name)
    previous = open(full, "rb").read() if os.path.isfile(full) and not os.path.islink(full) else b""
    twin.write(name, edit_text(rng, previous))
    return f"edit {name}"


def _current_branch(twin):
    return go.git(twin.theirs_dir, "symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.decode().strip() or None


def _commit_all(twin, rng, steps, check):
    steps.append(_same(twin.add("."), "add .", twin))
    steps.append(_same(twin.commit("step %d" % twin.step), "commit", twin))
    check(steps)


def workflow_merge(twin, rng, check):
    """Two branches diverge by a few commits each and are merged (or the merge is refused) in a randomly dirty or clean state."""
    steps = []
    shuffled = rng.sample(FILES, len(FILES))
    left, right = shuffled[:3], shuffled[3:]
    disjoint = rng.random() < 0.7                                           # mostly different files on the two sides, sometimes the same ones
    pick_left = (lambda: rng.choice(left)) if disjoint else (lambda: rng.choice(FILES))
    pick_right = (lambda: rng.choice(right)) if disjoint else (lambda: rng.choice(FILES))
    steps.append(_same(twin.add("."), "add .", twin))
    steps.append(_same(twin.commit("before the workflow %d" % twin.step), "commit", twin))
    base = _current_branch(twin)
    other = rng.choice(["feat", "side", "x/y"])
    if base == other:
        other = "feat" if base != "feat" else "side"
    if rng.random() < 0.9 or not twin.head(twin.theirs_dir):
        steps.append(_same(twin.branch(other, rng.choice([None, None, "HEAD~1"])), f"branch {other}", twin))
    check(steps)
    steps.append(_same(twin.checkout(other), f"checkout {other}", twin))
    check(steps)
    for _ in range(rng.randint(1, 3)):
        for _ in range(rng.randint(1, 2)):
            steps.append(_edit(twin, rng, pick_right()))
        _commit_all(twin, rng, steps, check)
    if base:
        steps.append(_same(twin.checkout(base), f"checkout {base}", twin))
        check(steps)
    for _ in range(rng.randint(0, 2)):
        for _ in range(rng.randint(1, 2)):
            steps.append(_edit(twin, rng, pick_left()))
        _commit_all(twin, rng, steps, check)
    if rng.random() < 0.2:
        steps.append(_edit(twin, rng))                                       # a dirty work tree at merge time
        if rng.random() < 0.5:
            steps.append(_same(twin.add("."), "add .", twin))
    steps.append(_same(twin.merge(other, ff=rng.choice(["allow", "allow", "never", "only"])), f"merge {other}", twin))
    check(steps)
    return steps


def workflow_dirty_checkout(twin, rng, check):
    """Edit files (staged or not) and try to switch branches or reset: refusals must agree."""
    steps = []
    for _ in range(rng.randint(1, 2)):
        steps.append(_edit(twin, rng))
    if rng.random() < 0.4:
        steps.append(_same(twin.add("."), "add .", twin))
    if rng.random() < 0.3:
        steps.append(_edit(twin, rng, rng.choice(FILES)))
    target = rng.choice(BRANCHES + ["HEAD~1", "HEAD~2"])
    steps.append(_same(twin.checkout(target), f"checkout {target}", twin))
    check(steps)
    return steps


def random_branch_step(twin, rng, check=None):
    """One random step (or a short scripted workflow) of a branching session; returns the list of descriptions. Outcomes of repository operations must agree."""
    check = check or (lambda steps: None)
    op = rng.random()
    if op < 0.34:
        return workflow_merge(twin, rng, check)
    if op < 0.44:
        return workflow_dirty_checkout(twin, rng, check)
    name = rng.choice(FILES)
    if op < 0.60:
        return [_edit(twin, rng, name)]
    if op < 0.63:
        twin.delete(name)
        return [f"delete {name}"]
    if op < 0.65:
        twin.chmod(name, rng.random() < 0.5)
        return [f"chmod {name}"]
    if op < 0.75:
        steps = [_same(twin.add("."), "add .", twin)]
        if rng.random() < 0.8:
            steps.append(_same(twin.commit("step %d" % twin.step, allow_empty=rng.random() < 0.1), "commit", twin))
        return steps
    if op < 0.80:
        b = rng.choice(BRANCHES)
        return [_same(twin.branch(b, rng.choice([None, None, "HEAD~1"])), f"branch {b}", twin)]
    if op < 0.84:
        b = rng.choice(BRANCHES)
        return [_same(twin.branch(b, delete=True, force_delete=rng.random() < 0.5), f"delete branch {b}", twin)]
    if op < 0.92:
        b = rng.choice(BRANCHES + ["HEAD~1", "HEAD^"])
        create = rng.choice(["nb1", "nb2", "nb3"]) if rng.random() < 0.15 else None
        return [_same(twin.checkout(b, create=create, detach=rng.random() < 0.1 and not create), f"checkout {b} {create}", twin)]
    if op < 0.95:
        return [_same(twin.reset(rng.choice(["HEAD", "HEAD~1", rng.choice(BRANCHES)]), rng.choice(["soft", "mixed", "hard"])), "reset", twin)]
    t = rng.choice(["v1", "v2", "v3"])
    return [_same(twin.tag(t, rng.choice([None, "HEAD~1"]), rng.choice([None, "annotated"])), f"tag {t}", twin)]


__all__ = ["Twin", "random_step", "random_branch_step", "seed_files", "Pathspec"]
