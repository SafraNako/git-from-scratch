"""A repository: the object database, references, index and work tree tied together, and the everyday operations on them.

Everything here is written from the format documentation and checked against real git in the test suite; see the README for what is deliberately left out (no remotes,
no hooks, no reflogs, no attributes/filters such as autocrlf, no rename detection).
"""
from __future__ import annotations

import dataclasses
import heapq
import os
import posixpath
import re
import stat as statmod
import time
from dataclasses import dataclass, field

from . import combine as cb
from . import diff as df
from . import index as ix
from . import rename as rn
from .config import Config, identity
from .errors import GitError
from .ignore import IgnoreRules, translate
from .objects import EMPTY_TREE, NULL_OID, MODE_EXEC, MODE_FILE, MODE_GITLINK, MODE_LINK, MODE_TREE, Commit, Tag, clean_message, hash_object, is_oid, parse_tree
from .odb import ObjectDB
from .refs import RefStore, check_branch_name, check_ref_format

HEAD_TEMPLATE = "ref: refs/heads/%s\n"
_PSEUDOREF = re.compile(r"^[A-Z][A-Z_]*$")


# --- pathspecs ------------------------------------------------------------------------------------------------------------------------------------
class Pathspec:
    """Plain pathspecs: a file or directory (everything below it), ``.`` for the current directory, or a glob in which ``*`` also crosses slashes. Magic such as
    ``:(glob)`` or ``:!x`` is refused rather than half-understood."""

    def __init__(self, specs, prefix=""):
        self.items = []
        self.hit = []
        for spec in specs:
            if spec.startswith(":"):
                raise GitError(f"pathspec magic is not supported: '{spec}'")
            joined = os.path.normpath(os.path.join(prefix, spec)).replace(os.sep, "/") if (prefix or spec) else ""
            if joined == ".":
                joined = ""
            if joined == ".." or joined.startswith("../"):
                raise GitError(f"'{spec}' is outside the repository")
            literal = not any(c in joined for c in "*?[\\")
            regex = None if literal else translate(joined, pathname=False)
            self.items.append((spec, joined, literal, re.compile(regex, re.DOTALL) if regex else None))
            self.hit.append(False)

    def __bool__(self):
        return bool(self.items)

    def match(self, path, mark=True):
        found = False
        for n, (spec, joined, literal, regex) in enumerate(self.items):
            if literal:
                ok = joined == "" or path == joined or path.startswith(joined + "/")
            else:
                ok = regex.fullmatch(path) is not None or any(regex.fullmatch(path[:i]) for i in _slashes(path))
            if ok:
                if mark:
                    self.hit[n] = True
                found = True
        return found

    def names_path(self, path):
        """git's ``exclude_matches_pathspec``: does a pathspec single out this path, or a directory leading to it? Judged on the literal text before the first wildcard,
        so ``tmp/out.log`` and ``tmp/*`` both name the directory ``tmp``, while ``*.log`` names nothing."""
        for _spec, joined, _lit, _r in self.items:
            cut = next((i for i, c in enumerate(joined) if c in "*?[\\"), len(joined))
            prefix = joined[:cut]
            if prefix == path or (len(prefix) > len(path) and prefix.startswith(path) and prefix[len(path)] == "/"):
                return True
        return False

    def unmatched(self, exists=None):
        """Pathspecs nothing matched. With ``exists`` (a function of the joined path) a plain path that exists on disk, even as an empty directory, is not an error."""
        return [spec for (spec, joined, literal, _r), hit in zip(self.items, self.hit) if not hit and not (literal and exists and (joined == "" or exists(joined)))]

    def could_contain(self, directory):
        """May a path below ``directory`` match? Lets a work-tree walk skip subtrees no pathspec can reach."""
        for _s, joined, literal, _r in self.items:
            if not literal or joined == "" or joined.startswith(directory + "/") or directory.startswith(joined + "/") or directory == joined:
                return True
        return False


def _slashes(path):
    return [i for i, c in enumerate(path) if c == "/"]


# --- results ---------------------------------------------------------------------------------------------------------------------------------------
@dataclass
class Status:
    entries: list = field(default_factory=list)          # (X, Y, path), sorted by path bytes
    untracked: list = field(default_factory=list)
    ignored: list = field(default_factory=list)
    branch: object = None                                # branch name, or None when HEAD is detached
    head: object = None                                  # commit id or None (unborn)
    renames: dict = field(default_factory=dict)          # new path -> old path, for the entries whose X is "R"

    def porcelain_lines(self, quote=True, prefix=""):
        """``XY path`` lines. ``prefix`` (the current directory relative to the repository root) makes the paths relative to it, as ``git status -s`` prints them;
        ``--porcelain`` output is always relative to the root."""
        q = (lambda p: df.quote_path(p, space=True)) if quote else (lambda p: p)
        rel = (lambda p: relative_to(p, prefix))
        out = ["%s%s %s%s" % (x, y, q(rel(self.renames[p])) + " -> " if p in self.renames else "", q(rel(p))) for x, y, p in self.entries]
        out += ["?? " + q(rel(p)) for p in self.untracked]
        return out

    @property
    def clean(self):
        return not self.entries and not self.untracked


def relative_to(path, prefix):
    """``path`` (relative to the repository root) as seen from the directory ``prefix``: ``../x`` for something outside it; a trailing slash is kept."""
    if not prefix:
        return path
    r = posixpath.relpath(path.rstrip("/"), prefix)
    return r + "/" if path.endswith("/") else r


def sort_key(path):
    return path.encode("utf-8", "surrogateescape")


class Repo:
    def __init__(self, gitdir, worktree, env=None, now=None):
        self.gitdir = os.path.abspath(gitdir)
        self.worktree = os.path.abspath(worktree) if worktree else None            # None: a bare repository
        self.bare = worktree is None
        self.env = dict(os.environ if env is None else env)
        self.now = now
        self.odb = ObjectDB(os.path.join(self.gitdir, "objects"))
        self.config = Config(self.gitdir, env=self.env)
        self.refs = RefStore(self.gitdir, log_all=self.config.get_bool("core.logAllRefUpdates", not self.bare))
        self._commits = {}
        self._shallow = None

    # --- creating and finding ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def init(path, branch="main", env=None, now=None, bare=False):
        path = os.path.abspath(path)
        gitdir = path if bare else os.path.join(path, ".git")
        existing = os.path.isdir(os.path.join(gitdir, "objects")) and os.path.exists(os.path.join(gitdir, "HEAD"))
        if not check_branch_name(branch):
            raise GitError(f"'{branch}' is not a valid branch name")
        os.makedirs(os.path.join(gitdir, "objects", "info"), exist_ok=True)
        os.makedirs(os.path.join(gitdir, "objects", "pack"), exist_ok=True)
        os.makedirs(os.path.join(gitdir, "refs", "heads"), exist_ok=True)
        os.makedirs(os.path.join(gitdir, "refs", "tags"), exist_ok=True)
        os.makedirs(os.path.join(gitdir, "info"), exist_ok=True)
        if not existing:
            with open(os.path.join(gitdir, "HEAD"), "w") as f:
                f.write(HEAD_TEMPLATE % branch)
            core = ["\trepositoryformatversion = 0", "\tfilemode = true", "\tbare = true" if bare else "\tbare = false"] + ([] if bare else ["\tlogallrefupdates = true"])
            if _case_insensitive(gitdir):
                core.append("\tignorecase = true")
            with open(os.path.join(gitdir, "config"), "w") as f:
                f.write("[core]\n" + "\n".join(core) + "\n")
            with open(os.path.join(gitdir, "info", "exclude"), "w") as f:
                f.write("# git ls-files --others --exclude-from=.git/info/exclude\n# Lines that start with '#' are comments.\n"
                        "# For a project mostly in C, the following would be a good set of\n# exclude patterns (uncomment them if you want to use them):\n"
                        "# *.[oa]\n# *~\n")
            with open(os.path.join(gitdir, "description"), "w") as f:
                f.write("Unnamed repository; edit this file 'description' to name the repository.\n")
        return Repo(gitdir, None if bare else path, env=env, now=now)

    @staticmethod
    def discover(start=None, env=None, now=None):
        env = dict(os.environ if env is None else env)
        if env.get("GIT_DIR"):
            gitdir = os.path.abspath(env["GIT_DIR"])
            tree = os.path.abspath(env.get("GIT_WORK_TREE") or os.getcwd())
            return Repo(gitdir, tree, env=env, now=now)
        cur = os.path.abspath(start or os.getcwd())
        while True:
            if os.path.isfile(os.path.join(cur, "HEAD")) and os.path.isdir(os.path.join(cur, "objects")) and os.path.isdir(os.path.join(cur, "refs")):
                return Repo(cur, None, env=env, now=now)                  # a bare repository: the directory is the git directory
            dot = os.path.join(cur, ".git")
            if os.path.isdir(dot) and os.path.exists(os.path.join(dot, "HEAD")):
                return Repo(dot, cur, env=env, now=now)
            if os.path.isfile(dot):
                with open(dot) as f:
                    first = f.readline().strip()
                if first.startswith("gitdir:"):
                    target = first[len("gitdir:"):].strip()
                    return Repo(os.path.normpath(os.path.join(cur, target)), cur, env=env, now=now)
            parent = os.path.dirname(cur)
            if parent == cur:
                raise GitError("not a git repository (or any of the parent directories)")
            cur = parent

    # --- small helpers -------------------------------------------------------------------------------------------------------------------------
    def log_entry(self, message):
        """``(identity text, message)`` for a reflog line, or ``None`` when no committer identity is configured."""
        try:
            return (str(identity("committer", self.config, self.env, now=self.clock())), message)
        except GitError:
            return None

    def clock(self):
        return int(self.now() if self.now else time.time())

    @property
    def index_path(self):
        return os.path.join(self.gitdir, "index")

    def read_index(self):
        return ix.Index.read(self.index_path)

    def write_index(self, index):
        index.write(self.index_path)

    def rel_cwd(self, cwd=None):
        """The current directory relative to the work tree root (``""`` at the root)."""
        if self.worktree is None:
            return ""
        cwd = os.path.realpath(cwd or os.getcwd())
        root = os.path.realpath(self.worktree)
        if cwd == root:
            return ""
        if not cwd.startswith(root + os.sep):
            return ""
        return os.path.relpath(cwd, root).replace(os.sep, "/")

    def ignore_rules(self):
        """Fresh rules for every operation: ignore files are read once per operation and never trusted across edits."""
        return IgnoreRules(self.worktree, self.gitdir, self.config, self.env)

    def _full(self, rel):
        return os.path.join(self.worktree, *rel.split("/")) if rel else self.worktree

    def filemode(self):
        return self.config.get_bool("core.filemode", True)

    # --- objects ---------------------------------------------------------------------------------------------------------------------------------
    @property
    def shallow(self):
        """Commits listed in ``.git/shallow``: a shallow clone stops there, and git treats them as having no parents (the parent objects are absent)."""
        if self._shallow is None:
            try:
                with open(os.path.join(self.gitdir, "shallow")) as f:
                    self._shallow = {line.strip() for line in f if is_oid(line.strip())}
            except FileNotFoundError:
                self._shallow = set()
        return self._shallow

    def read_commit(self, oid):
        c = self._commits.get(oid)
        if c is None:
            kind, data = self.odb.read(oid)
            if kind != "commit":
                raise GitError(f"{oid} is a {kind}, not a commit")
            c = Commit.parse(data)
            if oid in self.shallow:
                c = dataclasses.replace(c, parents=[])
            self._commits[oid] = c
        return c

    def read_tree_entries(self, oid):
        kind, data = self.odb.read(oid)
        if kind != "tree":
            raise GitError(f"{oid} is a {kind}, not a tree")
        return parse_tree(data)

    def peel(self, oid, want=None):
        """Follow tags (and, for ``want="tree"``, commits) until an object of the wanted kind is reached; ``want=None`` peels tags only."""
        for _ in range(50):
            kind, data = self.odb.read(oid)
            if kind == want or (want is None and kind != "tag"):
                return oid
            if kind == "tag":
                oid = Tag.parse(data).object
            elif kind == "commit" and want == "tree":
                oid = Commit.parse(data).tree
            else:
                raise GitError(f"{oid} is a {kind}, which cannot be peeled to a {want}")
        raise GitError("tag chain is too deep")

    def commit_of(self, spec):
        return self.peel(self.rev_parse(spec), "commit")

    def tree_files(self, tree_oid):
        return ix.flatten_tree(self.odb, tree_oid)

    def head_files(self):
        oid = self.refs.head_oid()
        return self.tree_files(self.read_commit(oid).tree) if oid else {}

    # --- revisions -------------------------------------------------------------------------------------------------------------------------------
    _OPS = re.compile(r"(~\d*|\^\{[^}]*\}|\^\d*)")           # "^{type}" must be tried before the plain parent suffix "^N"

    def _reflog_selector(self, name, selector, original):
        """Resolve ``name@{selector}`` where the selector is a number (the n-th previous value of the ref) or ``-n`` (the branch checked out n switches ago)."""
        if not re.fullmatch(r"-?\d+", selector):
            raise GitError(f"'{original}': only numeric reflog selectors are supported")
        n = int(selector)
        if n < 0:
            switches = [e[5][len("checkout: moving from "):].split(" to ")[0] for e in self.refs.read_log("HEAD") if e[5].startswith("checkout: moving from ")]
            if len(switches) < -n:
                raise GitError(f"'{original}': only {len(switches)} checkout(s) have been made")
            oid = self._resolve_name(switches[n])
            if oid is None:
                raise GitError(f"'{original}': unknown revision '{switches[n]}'")
            return oid
        if name in ("", "@"):
            kind, value = self.refs.head()
            ref = value if kind == "ref" else "HEAD"
        elif name == "HEAD" or name.startswith("refs/"):
            ref = name
        else:
            ref = next((c for c in ("refs/heads/" + name, "refs/tags/" + name, "refs/remotes/" + name) if self.refs.exists(c) or os.path.exists(self.refs._log_path(c))), None)
            if ref is None:
                raise GitError(f"unknown revision or path: '{original}'")
        log = self.refs.read_log(ref)
        if n >= len(log):
            if n == 0 and self.refs.read(ref):
                return self.refs.read(ref)
            raise GitError(f"log for '{name or ref}' only has {len(log)} entries")
        return log[len(log) - 1 - n][1]

    def _resolve_name(self, name):
        if is_oid(name) and self.odb.exists(name):
            return name
        candidates = []
        if _PSEUDOREF.match(name) and (name == "HEAD" or name.endswith("_HEAD")):
            candidates.append(name)
        candidates += ["refs/" + name, "refs/tags/" + name, "refs/heads/" + name, "refs/remotes/" + name, "refs/remotes/" + name + "/HEAD"]
        if name.startswith("refs/"):
            candidates.insert(0, name)
        for ref in candidates:
            if not check_ref_format(ref, onelevel=True):
                continue
            oid = self.refs.read(ref)
            if oid:
                return oid
        if len(name) >= 4 and re.fullmatch(r"[0-9a-fA-F]+", name):
            try:
                return self.odb.resolve_prefix(name)
            except GitError as exc:
                if "ambiguous" in str(exc):
                    raise
        return None

    def rev_parse(self, spec):
        """Resolve a revision expression: ``HEAD``, ``@``, branch/tag names, (abbreviated) ids, ``rev~N``, ``rev^N``, ``rev^{type}``, ``rev:path`` and ``:path``."""
        original = spec
        m = re.match(r"^([^~^:@{}]*)@\{([^}]*)\}(.*)$", spec)
        if m:                                                          # name@{n}, @{n}, @{-n}: resolve through the reflog, then go on with the rest
            spec = self._reflog_selector(m.group(1), m.group(2), original) + m.group(3)
        elif "@{" in spec and not re.search(r"\^\{", spec):
            raise GitError(f"'{spec}': unsupported @{{...}} selector")
        if spec.startswith(":") and not spec.startswith(":/"):
            m = re.fullmatch(r":([0-3]):(.*)", spec)
            stage, path = (int(m.group(1)), m.group(2)) if m else (0, spec[1:])
            entry = self.read_index().entries.get((path, stage))
            if entry is None:
                raise GitError(f"path '{path}' is not in the index" if stage == 0 else f"path '{path}' is not in the index at stage {stage}")
            return entry.oid
        path = None
        depth = 0
        for i, c in enumerate(spec):                                   # the first colon outside ^{...} separates a path
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            elif c == ":" and depth == 0:
                spec, path = spec[:i], spec[i + 1:]
                break
        m = re.match(r"^([^~^]+)((?:~\d*|\^\d*|\^\{[^}]*\})*)$", spec)
        if not m:
            raise GitError(f"unknown revision '{original}'")
        base, ops = m.group(1), m.group(2)
        oid = self._resolve_name("HEAD" if base == "@" else base)
        if oid is None:
            if base == "HEAD":
                raise GitError("HEAD does not point to a commit yet (the current branch has no commits)")
            raise GitError(f"unknown revision or path: '{original}'")
        for op in self._OPS.findall(ops):
            if op.startswith("~"):
                n = int(op[1:] or 1)
                oid = self.peel(oid, "commit")
                for _ in range(n):
                    parents = self.read_commit(oid).parents
                    if not parents:
                        raise GitError(f"'{original}' does not name a commit: it has no parent that far back")
                    oid = parents[0]
            elif op.startswith("^{"):
                kind = op[2:-1]
                if kind in ("", "object"):
                    oid = self.peel(oid) if kind == "" else oid
                elif kind in ("commit", "tree", "blob", "tag"):
                    if kind == "tag" and self.odb.kind_of(oid) != "tag":
                        raise GitError(f"'{original}' does not name a tag object")
                    oid = oid if kind == "tag" else self.peel(oid, kind)
                else:
                    raise GitError(f"unsupported revision suffix '{op}' (only ^{{}}, ^{{commit}}, ^{{tree}}, ^{{blob}}, ^{{tag}} are understood)")
            else:
                n = int(op[1:] or 1)
                oid = self.peel(oid, "commit")
                if n:
                    parents = self.read_commit(oid).parents
                    if n > len(parents):
                        raise GitError(f"'{original}': the commit has no parent number {n}")
                    oid = parents[n - 1]
        if path is not None:
            tree = self.peel(oid, "tree")
            return self._tree_path(tree, path, original)
        return oid

    def _tree_path(self, tree, path, original):
        parts = [p for p in path.split("/") if p]
        cur = tree
        entry_oid = tree
        for i, part in enumerate(parts):
            entries = self.read_tree_entries(cur)
            hit = next((e for e in entries if e.name == part), None)
            if hit is None:
                raise GitError(f"path '{path}' does not exist in '{original.split(':')[0] or 'the index'}'")
            entry_oid = hit.oid
            if i < len(parts) - 1:
                if hit.mode != MODE_TREE:
                    raise GitError(f"path '{path}' does not exist: '{part}' is not a directory")
                cur = hit.oid
        return entry_oid

    # --- the work tree ---------------------------------------------------------------------------------------------------------------------------
    def _lstat(self, rel):
        try:
            return os.lstat(self._full(rel))
        except (FileNotFoundError, NotADirectoryError):
            return None

    def worktree_mode(self, st, entry_mode=None):
        if statmod.S_ISLNK(st.st_mode):
            return MODE_LINK
        if not self.filemode() and entry_mode in (MODE_FILE, MODE_EXEC):
            return entry_mode
        return MODE_EXEC if st.st_mode & 0o111 else MODE_FILE

    def read_worktree(self, rel, st=None):
        """``(mode, data)`` of a work tree file as git would index it; a symlink's data is its target."""
        st = st or os.lstat(self._full(rel))
        if statmod.S_ISLNK(st.st_mode):
            return MODE_LINK, os.fsencode(os.readlink(self._full(rel)))
        with open(self._full(rel), "rb") as f:
            return self.worktree_mode(st), f.read()

    def _entry_unchanged(self, entry, rel, st, index):
        """Does the work tree file still equal the index entry? The stat data answers cheaply; when it differs (or is racy) the content decides."""
        if statmod.S_ISDIR(st.st_mode) and entry.mode == MODE_GITLINK:
            return True
        if statmod.S_ISDIR(st.st_mode) or not (statmod.S_ISREG(st.st_mode) or statmod.S_ISLNK(st.st_mode)):
            return False
        mode = self.worktree_mode(st, entry.mode)
        if mode != entry.mode:
            return False
        if ix.stat_matches(entry, st) and not index.is_racy(entry):
            return True
        _mode, data = self.read_worktree(rel, st)
        return hash_object("blob", data) == entry.oid

    def walk_untracked_candidates(self, index, ignore=True, collapse=False):
        """Files in the work tree that are neither tracked nor ignored, plus untracked nested repositories (reported as ``dir/``). With ``collapse`` (git's default
        ``-unormal``) a directory with no tracked file below it, and at least one untracked non-ignored file, is reported as just ``dir/``."""
        rules = self.ignore_rules() if ignore else None
        tracked = {p for p, _s in index.entries}
        tracked_dirs = set()
        for path in tracked:
            parts = path.split("/")
            for i in range(1, len(parts)):
                tracked_dirs.add("/".join(parts[:i]))

        def visit(rel_dir, out):
            full = self._full(rel_dir)
            try:
                names = sorted(os.listdir(full), key=lambda n: n.encode("utf-8", "surrogateescape"))
            except OSError:
                return
            for name in names:
                rel = rel_dir + "/" + name if rel_dir else name
                if name == ".git":
                    continue
                st = os.lstat(os.path.join(full, name))
                if statmod.S_ISDIR(st.st_mode):
                    if rel in tracked:                                   # a submodule (gitlink) is tracked as a whole
                        continue
                    if os.path.lexists(os.path.join(full, name, ".git")):
                        if not (rules and rules.is_ignored(rel, True)):
                            out.append(rel + "/")
                        continue
                    if rules and rules.is_ignored(rel, True):
                        continue
                    if collapse and rel not in tracked_dirs:
                        inside = []
                        visit(rel, inside)
                        if inside:
                            out.append(rel + "/")
                        continue
                    visit(rel, out)
                elif rel not in tracked:
                    if statmod.S_ISREG(st.st_mode) or statmod.S_ISLNK(st.st_mode):
                        if not (rules and rules.is_ignored(rel, False)):
                            out.append(rel)

        found = []
        visit("", found)
        return sorted(found, key=sort_key)

    # --- status ----------------------------------------------------------------------------------------------------------------------------------
    def status(self, untracked="all"):
        index = self.read_index()
        head = self.refs.head_oid()
        head_files = self.tree_files(self.read_commit(head).tree) if head else {}
        by_path = {}
        for (path, stage), e in index.entries.items():
            by_path.setdefault(path, {})[stage] = e
        entries = []
        deleted, added = {}, {}                          # staged deletions and additions: candidates for rename detection
        for path in sorted(set(by_path) | set(head_files), key=sort_key):
            stages = by_path.get(path, {})
            h = head_files.get(path)
            if any(stages) and set(stages) != {0}:
                code = self._unmerged_code(stages)
                entries.append((code[0], code[1], path))
                continue
            e = stages.get(0)
            x = " "
            if e is None and h is not None:
                x = "D"
            elif e is not None and h is None:
                x = "A"
            elif e is not None and h is not None and (e.oid, e.mode) != (h[1], h[0]):
                x = "T" if (e.mode == MODE_LINK) != (h[0] == MODE_LINK) or (e.mode == MODE_GITLINK) != (h[0] == MODE_GITLINK) else "M"
            y = " "
            if e is not None:
                st = self._lstat(path)
                if st is None or (statmod.S_ISDIR(st.st_mode) and e.mode != MODE_GITLINK):
                    y = "D"
                elif not self._entry_unchanged(e, path, st, index):
                    wt_mode = self.worktree_mode(st, e.mode) if (statmod.S_ISREG(st.st_mode) or statmod.S_ISLNK(st.st_mode)) else None
                    y = "T" if wt_mode is not None and (wt_mode == MODE_LINK) != (e.mode == MODE_LINK) else "M"
            if x == "D":
                deleted[path] = h
            elif x == "A":
                added[path] = e
            if (x, y) != (" ", " "):
                entries.append((x, y, path))
        renames = {}
        if deleted and added:
            changes = [(p, self._blob_side(*h), None) for p, h in deleted.items()] + [(p, None, self._blob_side(e.mode, e.oid)) for p, e in added.items()]
            changes.sort(key=lambda c: sort_key(c[0]))
            for path, old_side, _new in self.detect_renames(changes, rn.DEFAULT_MINIMUM_SCORE):
                if isinstance(old_side, df.RenamedSide):
                    renames[path] = old_side.path
        if renames:
            gone = set(renames.values())
            entries = [("R" if p in renames else x, y, p) for x, y, p in entries if not (x == "D" and p in gone)]
        s = Status(entries=entries, branch=self.refs.current_branch(), head=head, renames=renames)
        if untracked != "no":
            s.untracked = self.walk_untracked_candidates(index, collapse=(untracked == "normal"))
        return s

    @staticmethod
    def _unmerged_code(stages):
        present = frozenset(stages)
        table = {frozenset({1, 2, 3}): "UU", frozenset({2, 3}): "AA", frozenset({1, 2}): "UD", frozenset({1, 3}): "DU", frozenset({2}): "AU", frozenset({3}): "UA", frozenset({1}): "DD"}
        return table.get(present, "UU")

    # --- staging ---------------------------------------------------------------------------------------------------------------------------------
    def _stage_file(self, index, rel, st, write=True):
        mode, data = self.read_worktree(rel, st)
        oid = self.odb.write("blob", data) if write else hash_object("blob", data)
        index.set(ix.IndexEntry(rel, mode, oid, **ix.stat_fields(st)))
        return oid

    def add(self, specs, prefix="", update=False, force=False):
        """Stage files. Returns ``(staged, removed, warnings)``: lists of paths. Raises when a pathspec matches nothing or names an ignored file (unless ``force``)."""
        if not specs:
            raise GitError("nothing specified, nothing added")
        ps = Pathspec(specs, prefix)
        index = self.read_index()                      # (an unmerged path is resolved by adding it: the stages are replaced when its file is staged)
        rules = self.ignore_rules()
        staged, removed, warnings, ignored_named = [], [], [], []
        refreshed_stat = []
        seen = set()
        tracked_paths = {p for p, _s in index.entries}

        def consider(rel, st):
            """Handle one work tree file matched by a pathspec. A pathspec only counts as matched by files that can actually be staged (git reports a glob that matches
            nothing but ignored files as matching nothing)."""
            seen.add(rel)
            if rel in tracked_paths:
                ps.match(rel)
                entry = index.entries.get((rel, 0))
                if entry is not None and self._entry_unchanged(entry, rel, st, index):
                    refreshed = ix.IndexEntry(rel, entry.mode, entry.oid, **ix.stat_fields(st))
                    if (refreshed.ctime, refreshed.mtime, refreshed.ino, refreshed.size) != (entry.ctime, entry.mtime, entry.ino, entry.size):
                        index.set(refreshed)              # same content, new stat data: keep the index fast
                        refreshed_stat.append(rel)
                    return
                self._stage_file(index, rel, st)
                staged.append(rel)
                return
            if update:
                return
            if rules.is_ignored(rel, False) and not force:
                if ps.names_path(rel):
                    ignored_named.append(rel)
                return
            if not (statmod.S_ISREG(st.st_mode) or statmod.S_ISLNK(st.st_mode)):
                warnings.append(f"skipping '{rel}': not a regular file or symlink")
                return
            ps.match(rel)
            self._stage_file(index, rel, st)
            staged.append(rel)

        def visit(rel_dir):
            full = self._full(rel_dir)
            for name in sorted(os.listdir(full)):
                rel = rel_dir + "/" + name if rel_dir else name
                if name == ".git":
                    continue
                st = os.lstat(os.path.join(full, name))
                if statmod.S_ISDIR(st.st_mode):
                    if not ps.could_contain(rel):
                        continue
                    if os.path.lexists(os.path.join(full, name, ".git")):
                        if ps.match(rel + "/x", mark=False):
                            warnings.append(f"skipping embedded git repository '{rel}' (submodules are not supported)")
                            ps.match(rel + "/x")
                        continue
                    if not force and rules.is_ignored(rel, True):
                        if not update and ps.names_path(rel):        # git reports an ignored directory that the command line points into, tracked content or not
                            ignored_named.append(rel)                # (but "add -u" adds no new files, so it has nothing to complain about)
                        if not any(p.startswith(rel + "/") for p in tracked_paths):
                            continue                                  # nothing is tracked below, so nothing below is considered
                    visit(rel)
                elif ps.match(rel, mark=False):
                    consider(rel, st)

        visit("")
        # tracked paths that vanished from the work tree (or turned into something else) are removed, as with ``git add -A``
        for path in sorted(tracked_paths, key=sort_key):
            if path in seen or not ps.match(path):
                continue
            st = self._lstat(path)
            if st is None or statmod.S_ISDIR(st.st_mode):
                entry0 = index.entries.get((path, 0))
                if entry0 is not None and entry0.mode == MODE_GITLINK and st is not None:
                    continue
                index.remove(path)
                removed.append(path)
        missing = ps.unmatched(lambda joined: os.path.lexists(self._full(joined)))
        if missing:
            raise GitError(f"pathspec '{missing[0]}' did not match any files")           # nothing has been written yet, as in git
        if staged or removed or refreshed_stat:
            self.write_index(index)
        if ignored_named:                                   # git adds everything else first and only then reports (and fails on) the ignored names
            raise GitError("the following paths are ignored by one of your .gitignore files: " + ", ".join(ignored_named) + " (use -f if you really want to add them)")
        return staged, removed, warnings

    def rm(self, specs, prefix="", cached=False, force=False, recursive=False):
        ps = Pathspec(specs, prefix)
        index = self.read_index()
        head_files = self.head_files()
        matched = [p for p in index.paths() if ps.match(p)]
        missing = ps.unmatched()
        if missing:
            raise GitError(f"pathspec '{missing[0]}' did not match any files")
        for spec, joined, literal, _r in ps.items:
            if literal and joined and (joined, 0) not in index.entries and not recursive:
                raise GitError(f"not removing '{spec}' recursively without -r")
        problems = []
        for path in matched:
            entry = index.entries.get((path, 0))
            if entry is None or force:
                continue
            h = head_files.get(path)
            st = self._lstat(path)
            staged_differs = (h is None) or (h != (entry.mode, entry.oid))
            file_differs = st is not None and not statmod.S_ISDIR(st.st_mode) and not self._entry_unchanged(entry, path, st, index)
            if cached:
                if staged_differs and file_differs:
                    problems.append(f"'{path}' has staged content different from both the file and the HEAD")
            elif file_differs and staged_differs:
                problems.append(f"'{path}' has staged content different from both the file and the HEAD")
            elif file_differs:
                problems.append(f"'{path}' has local modifications")
            elif staged_differs:
                problems.append(f"'{path}' has changes staged in the index")
        if problems:
            raise GitError("; ".join(problems) + " (use --cached to keep the file, or -f to force removal)")
        for path in matched:
            index.remove(path)
            if not cached:
                try:
                    os.unlink(self._full(path))
                except FileNotFoundError:
                    pass
                self._prune_empty_dirs(os.path.dirname(path))
        self.write_index(index)
        return matched

    def _prune_empty_dirs(self, rel_dir):
        while rel_dir:
            try:
                os.rmdir(self._full(rel_dir))
            except OSError:
                return
            rel_dir = os.path.dirname(rel_dir)

    # --- committing ------------------------------------------------------------------------------------------------------------------------------
    def make_commit(self, tree, parents, message, author=None, committer=None):
        author = author or identity("author", self.config, self.env, now=self.clock())
        committer = committer or identity("committer", self.config, self.env, now=self.clock())
        text = clean_message(message)
        if not text:
            raise GitError("aborting commit due to empty commit message")
        return self.odb.write("commit", Commit(tree, list(parents), author, committer, text).serialize())

    def commit(self, message=None, amend=False, allow_empty=False):
        index = self.read_index()
        if any(stage for _p, stage in index.entries):
            raise GitError("committing is not possible because you have unmerged files")
        tree = ix.index_to_tree(self.odb, index)
        head = self.refs.head_oid()
        parents = [head] if head else []
        author = None
        merge_head = None
        merge_head_path = os.path.join(self.gitdir, "MERGE_HEAD")
        if os.path.exists(merge_head_path):                         # concluding a merge: the second parent and the prepared message
            if amend:
                raise GitError("you are in the middle of a merge: cannot amend")
            with open(merge_head_path) as f:
                merge_head = f.read().strip()
            parents = [head, merge_head]
            if message is None:
                try:
                    with open(os.path.join(self.gitdir, "MERGE_MSG"), encoding="utf-8", errors="surrogateescape") as f:
                        message = f.read()
                except FileNotFoundError:
                    pass
        if merge_head:
            pass
        elif amend:
            if not head:
                raise GitError("you have nothing to amend")
            old = self.read_commit(head)
            parents, author = list(old.parents), old.author
            if message is None:
                message = old.message
            # amending is refused when the result would be an empty commit, i.e. equal to the amended commit's parent (or to the empty tree for a root commit)
            if not allow_empty and tree == (self.read_commit(parents[0]).tree if parents else EMPTY_TREE):
                raise NothingToCommit("amending would make the commit empty (use allow_empty to keep it)")
        elif not allow_empty:
            parent_tree = self.read_commit(head).tree if head else EMPTY_TREE
            if tree == parent_tree:
                raise NothingToCommit("nothing to commit")
        if message is None:
            raise GitError("a commit message is required (-m)")
        oid = self.make_commit(tree, parents, message, author=author)
        kind = "commit (amend)" if amend else "commit (merge)" if merge_head else "commit (initial)" if head is None else "commit"
        self._advance_head(oid, head, "%s: %s" % (kind, clean_message(message).split("\n")[0]))
        if merge_head:
            from .merge import clear_merge_state
            clear_merge_state(self)
        return oid

    def _advance_head(self, oid, old, message=None):
        """Move the branch ``HEAD`` names (or a detached ``HEAD``) to ``oid``, logging ``message`` in the reflogs of both."""
        kind, value = self.refs.head()
        log = self.log_entry(message) if message else None
        if kind == "ref":
            self.refs.update(value, oid, old=old, log=log, also_log=("HEAD",))
        else:
            self.refs.update("HEAD", oid, old=old, log=log)

    # --- history ---------------------------------------------------------------------------------------------------------------------------------
    def reachable(self, tips):
        """Every commit reachable from ``tips`` (ids or revisions already resolved to commits), as a set."""
        seen = set()
        stack = [self.peel(t, "commit") for t in tips]
        while stack:
            oid = stack.pop()
            if oid in seen:
                continue
            seen.add(oid)
            stack.extend(self.read_commit(oid).parents)
        return seen

    def walk(self, include, exclude=(), limit=None):
        """Commits reachable from ``include`` but not from ``exclude``, in git's default order: newest committer date first, ties in discovery order (a commit is
        queued the moment it is first seen, exactly as git's revision walk does)."""
        boring = self.reachable(exclude) if exclude else set()
        heap, seen, counter, emitted = [], set(), 0, 0

        def push(oid):
            nonlocal counter
            if oid in seen or oid in boring:
                return
            seen.add(oid)
            counter += 1
            heapq.heappush(heap, (-self.read_commit(oid).committer.time, counter, oid))

        for oid in include:
            push(self.peel(oid, "commit"))
        while heap:
            _t, _n, oid = heapq.heappop(heap)
            yield oid
            emitted += 1
            if limit is not None and emitted >= limit:
                return
            for parent in self.read_commit(oid).parents:
                push(parent)

    def order_commits(self, commits, mode, parents=None):
        """Reorder a walk result like git's ``--topo-order`` (no commit before all its children, a branch shown to the end before the next one starts) or
        ``--date-order`` (the same guarantee, but the newest available commit first). ``parents`` overrides the parent lists (history simplification)."""
        if parents is None:
            parents = {c: self.read_commit(c).parents for c in commits}
        indegree = {c: 1 for c in commits}
        for c in commits:
            for p in parents[c]:
                if indegree.get(p):
                    indegree[p] += 1
        tips = [c for c in commits if indegree[c] == 1]
        out, counter = [], 0
        if mode == "topo":
            stack = list(reversed(tips))                     # a stack: the most recently released parent is shown next
            take, put = stack.pop, stack.append
        else:
            heap = []

            def put(c):
                nonlocal counter
                counter += 1
                heapq.heappush(heap, (-self.read_commit(c).committer.time, counter, c))

            def take():
                return heapq.heappop(heap)[2]
            for c in tips:
                put(c)
            stack = heap
        while stack:
            c = take()
            for p in parents[c]:
                if not indegree.get(p):
                    continue
                indegree[p] -= 1
                if indegree[p] == 1:
                    put(p)
            indegree[c] = 0
            out.append(c)
        return out

    def format_commit(self, oid, oneline=False, parents=None, from_parent=None):
        """``parents`` overrides the commit's own (history simplification and ``--graph`` rewrite them, and the ``Merge:`` line follows); ``from_parent`` marks the entry
        of ``log -m`` that shows the merge against that parent."""
        c = self.read_commit(oid)
        if oneline:
            return "%s%s %s" % (self.odb.abbrev(oid), " (from %s)" % self.odb.abbrev(from_parent) if from_parent else "", c.subject)
        parents = c.parents if parents is None else parents
        lines = ["commit " + oid + (" (from %s)" % from_parent if from_parent else "")]
        if len(parents) > 1:
            lines.append("Merge: " + " ".join(self.odb.abbrev(p) for p in parents))
        lines.append("Author: %s <%s>" % (c.author.name, c.author.email))
        lines.append("Date:   " + format_date(c.author))
        lines.append("")
        for line in c.message.rstrip("\n").split("\n"):
            lines.append("    " + line)                 # blank lines are indented too: git prints four spaces there
        return "\n".join(lines) + "\n"

    # --- diffs -----------------------------------------------------------------------------------------------------------------------------------
    # A change is ``(path, old Side or None, new Side or None)``; patches, ``--numstat`` and ``--name-status`` are all rendered from the same list.
    def _blob_side(self, mode, oid):
        return df.blob_side(mode, oid, self.odb)

    def changes_snapshots(self, old, new, paths=None):
        """Changes between two ``{path: (mode, oid)}`` snapshots whose blobs are in the object database."""
        return [(path, self._blob_side(*o) if o else None, self._blob_side(*n) if n else None)
                for path, o, n in df.diff_flat(old, new) if paths is None or paths.match(path)]

    def index_files(self, index=None, lenient=False):
        index = index or self.read_index()
        return {e.path: (e.mode, e.oid) for e in (index.zero_entries() if lenient else index.stage_zero())}

    def _worktree_changes(self, index, base, paths, unmerged=False, stage=0):
        """Changes from ``base`` (``{path: Side-source}``: a snapshot of (mode, oid)) to the work tree files of the tracked paths. With ``unmerged`` a path with conflicts
        becomes an ``Unmerged`` entry (what ``git diff`` of the index against the work tree does); otherwise it is just a tracked file."""
        if self.worktree is None:
            raise GitError("this operation must be run in a work tree (this is a bare repository)")
        out = []
        conflicts = index.unmerged()
        tracked = {e.path for e in index.zero_entries()} | set(conflicts)
        for path in sorted(set(base) | tracked, key=sort_key):
            if paths is not None and not paths.match(path):
                continue
            old = base.get(path)
            st = self._lstat(path) if path in tracked else None
            exists = st is not None and not statmod.S_ISDIR(st.st_mode)
            entry = index.entries.get((path, 0))
            if exists and entry is not None and old == (entry.mode, entry.oid) and self._entry_unchanged(entry, path, st, index):
                continue
            new = None
            if exists:
                mode, data = self.read_worktree(path, st)
                new = df.file_side(mode, hash_object("blob", data), data)
            if unmerged and path in conflicts:
                out.append((path, df.Unmerged({n: (e.mode, e.oid) for n, e in conflicts[path].items()}, new, stage=stage), None))
                continue
            old_side = self._blob_side(*old) if old else None
            if old is None and new is None:
                continue
            if old is not None and new is not None and (old[0] == MODE_LINK) != (new.mode == MODE_LINK):
                out.append((path, old_side, None))
                out.append((path, None, new))
            elif old is None or new is None or (old[1], old[0]) != (new.oid, new.mode):
                out.append((path, old_side, new))
        return out

    def changes_worktree(self, paths=None, stage=0):
        """Index versus work tree (``git diff``); paths with conflicts come as ``Unmerged`` entries."""
        index = self.read_index()
        return self._worktree_changes(index, self.index_files(index, lenient=True), paths, unmerged=True, stage=stage)

    def changes_against_commit(self, commit, paths=None):
        """A commit versus the work tree (``git diff <commit>``)."""
        return self._worktree_changes(self.read_index(), self.tree_files(self.read_commit(commit).tree), paths)

    def changes_cached(self, against=None, paths=None):
        """A commit (``HEAD`` unless given) versus the index: what a commit would record; paths with conflicts come as ``Unmerged`` entries."""
        base = self.tree_files(self.read_commit(against).tree) if against is not None else self.head_files()
        index = self.read_index()
        conflicts = index.unmerged()
        changes = self.changes_snapshots({p: v for p, v in base.items() if p not in conflicts}, self.index_files(index, lenient=True), paths)
        for path, stages in conflicts.items():
            if paths is None or paths.match(path):
                changes.append((path, df.Unmerged({n: (e.mode, e.oid) for n, e in stages.items()}, None, cached=True), None))
        return sorted(changes, key=lambda c: sort_key(c[0]))

    def changes_commits(self, a, b, paths=None):
        old = self.tree_files(self.read_commit(a).tree) if a else {}
        new = self.tree_files(self.read_commit(b).tree) if b else {}
        return self.changes_snapshots(old, new, paths)

    def commit_changes(self, oid, paths=None):
        """The changes a commit made against its first parent (against the empty tree for a root commit)."""
        parents = self.read_commit(oid).parents
        return self.changes_commits(parents[0] if parents else None, oid, paths)

    def detect_renames(self, changes, threshold=None):
        """Pair deletions with additions in a change list (``threshold`` is the minimum similarity out of 60000, or None to leave the list alone)."""
        if threshold is None:
            return changes
        return rn.detect_renames(changes, minimum_score=threshold)

    def render_changes(self, changes, context=3, dense=True):
        """The patch text of a change list. Conflicted paths with both an ``ours`` and a ``theirs`` side get a combined diff of the work tree file, and those are written
        first (git prints them as it finds them and the rest when everything is collected); other conflicted paths are announced as unmerged."""
        combined, queued = [], []
        for path, old, new in changes:
            if not isinstance(old, df.Unmerged):
                queued.append(df.render_change(path, old, new, self.odb.abbrev, context))
                continue
            st = old.stages
            want = old.stage or 2
            if not old.cached and not old.stage and 2 in st and 3 in st:
                combined.append(cb.combined_patch(path, self._worktree_side(old.worktree), [self._lazy_side(st[2]), self._lazy_side(st[3])], self.odb.abbrev,
                                                  dense=dense, context=context, force_header=True))
                continue
            queued.append(b"* Unmerged path %s\n" % df.quote_path(path).encode())
            if not old.cached and want in st and old.worktree is not None and (old.worktree.mode, old.worktree.oid) != st[want]:
                queued.append(df.render_change(path, self._blob_side(*st[want]), old.worktree, self.odb.abbrev, context))
            elif not old.cached and want in st and old.worktree is None:
                queued.append(df.render_change(path, self._blob_side(*st[want]), None, self.odb.abbrev, context))
        return b"".join(combined + queued)

    def _worktree_side(self, side):
        """A work tree file as a ``(mode, oid, data)`` triple for a combined diff; git shows its id as all zeros."""
        if side is None:
            return (0, None, b"")
        return (side.mode, NULL_OID, side.data)

    def diff_snapshots(self, old, new, context=3, paths=None, renames=rn.DEFAULT_MINIMUM_SCORE):
        return self.render_changes(self.detect_renames(self.changes_snapshots(old, new, paths), renames), context)

    def diff_worktree(self, paths=None, context=3, renames=rn.DEFAULT_MINIMUM_SCORE):
        return self.render_changes(self.detect_renames(self.changes_worktree(paths), renames), context)

    def diff_against_commit(self, commit, paths=None, context=3, renames=rn.DEFAULT_MINIMUM_SCORE):
        return self.render_changes(self.detect_renames(self.changes_against_commit(commit, paths), renames), context)

    def diff_cached(self, against=None, paths=None, context=3, renames=rn.DEFAULT_MINIMUM_SCORE):
        return self.render_changes(self.detect_renames(self.changes_cached(against, paths), renames), context)

    def diff_commits(self, a, b, paths=None, context=3, renames=rn.DEFAULT_MINIMUM_SCORE):
        return self.render_changes(self.detect_renames(self.changes_commits(a, b, paths), renames), context)

    def combined_diff(self, oid, parents=None, dense=True, context=3, paths=None):
        """The ``diff --cc`` (``dense``) or ``diff --combined`` text of a merge commit: the paths in which the result differs from every parent."""
        c = self.read_commit(oid)
        parents = c.parents if parents is None else parents
        result = self.tree_files(c.tree)
        sides = [self.tree_files(self.read_commit(p).tree) for p in parents]
        out = []
        for path in sorted(result.keys() | set().union(*[s.keys() for s in sides]), key=lambda p: p.encode("utf-8", "surrogateescape")):
            if paths is not None and not paths.match(path, mark=False):
                continue
            r = result.get(path)
            if all(s.get(path) == r for s in sides) or any(s.get(path) == r for s in sides):
                continue                                                  # taken unchanged from some parent: not a combined change
            out.append(cb.combined_patch(path, self._lazy_side(r), [self._lazy_side(s.get(path)) for s in sides], self.odb.abbrev, dense=dense, context=context))
        return b"".join(out)

    def _lazy_side(self, entry):
        if entry is None:
            return (0, None, b"")
        mode, oid = entry
        return (mode, oid, lambda: self.odb.read(oid)[1])

    def show_commit(self, oid, context=3, dense=True, renames=rn.DEFAULT_MINIMUM_SCORE):
        c = self.read_commit(oid)
        text = self.format_commit(oid).encode("utf-8", "surrogateescape")
        if len(c.parents) > 1:
            return text + b"\n" + self.combined_diff(oid, dense=dense, context=context)          # the empty line comes even when no path is left to show
        patch = self.diff_commits(c.parents[0] if c.parents else None, oid, context=context, renames=renames)
        return text + (b"\n" + patch if patch else b"")

    def show_tag(self, oid, context=3, renames=rn.DEFAULT_MINIMUM_SCORE):
        """``git show`` of an annotated tag: the tag's own header and message, then what it points at."""
        tag = Tag.parse(self.odb.read(oid)[1])
        lines = ["tag " + tag.name]
        if tag.tagger:
            lines += ["Tagger: %s <%s>" % (tag.tagger.name, tag.tagger.email), "Date:   " + format_date(tag.tagger)]
        text = "\n".join(lines) + "\n\n" + tag.message.rstrip("\n") + "\n\n"
        target = tag.object
        kind = self.odb.kind_of(target)
        return text.encode("utf-8", "surrogateescape") + (self.show_commit(target, context) if kind == "commit" else self.show_tag(target, context) if kind == "tag" else b"")

    # --- branches and tags -----------------------------------------------------------------------------------------------------------------------
    def branches(self):
        return [(n[len("refs/heads/"):], oid) for n, oid in self.refs.list("refs/heads/")]

    def create_branch(self, name, start=None, force=False):
        if not check_branch_name(name):
            raise GitError(f"'{name}' is not a valid branch name")
        ref = "refs/heads/" + name
        if self.refs.exists(ref) and not force:
            raise GitError(f"a branch named '{name}' already exists")
        oid = self.commit_of(start) if start else self.refs.head_oid()
        if oid is None:
            raise GitError("not a valid object name: 'HEAD' (the current branch has no commits yet)")
        self.refs.update(ref, oid, log=self.log_entry("branch: Created from %s" % (start or self.refs.current_branch() or "HEAD")))
        return oid

    def delete_branch(self, name, force=False):
        ref = "refs/heads/" + name
        oid = self.refs.read(ref)
        if oid is None:
            raise GitError(f"branch '{name}' not found")
        if self.refs.current_branch() == name:
            raise GitError(f"cannot delete branch '{name}' checked out in this work tree")
        if not force:
            head = self.refs.head_oid()
            if head is None or oid not in set(self.walk([head])):
                raise GitError(f"the branch '{name}' is not fully merged (use -D to delete it anyway)")
        self.refs.delete(ref)
        return oid

    def rename_branch(self, old, new):
        if not check_branch_name(new):
            raise GitError(f"'{new}' is not a valid branch name")
        oid = self.refs.read("refs/heads/" + old)
        if oid is None:
            raise GitError(f"branch '{old}' not found")
        if self.refs.exists("refs/heads/" + new):
            raise GitError(f"a branch named '{new}' already exists")
        current = self.refs.current_branch() == old
        message = "Branch: renamed refs/heads/%s to refs/heads/%s" % (old, new)
        self.refs.update("refs/heads/" + new, oid)
        self.refs.move_log("refs/heads/" + old, "refs/heads/" + new)
        if current:
            self.refs.set_symbolic("HEAD", "refs/heads/" + new)
        self.refs.delete("refs/heads/" + old)
        entry = self.log_entry(message)
        if entry:
            self.refs.append_log("refs/heads/" + new, oid, oid, *entry)
            if current:
                self.refs.append_log("HEAD", oid, oid, *entry)
                self.refs.append_log("HEAD", oid, oid, *entry)             # git really writes this one twice (the ref rename and the HEAD update)

    def tags(self):
        return [(n[len("refs/tags/"):], oid) for n, oid in self.refs.list("refs/tags/")]

    def create_tag(self, name, target=None, message=None, force=False):
        ref = "refs/tags/" + name
        if not check_ref_format(ref) or name.startswith("-"):
            raise GitError(f"'{name}' is not a valid tag name")
        if self.refs.exists(ref) and not force:
            raise GitError(f"tag '{name}' already exists")
        oid = self.rev_parse(target) if target else self.refs.head_oid()
        if oid is None:
            raise GitError("failed to resolve 'HEAD' as a valid ref (the current branch has no commits yet)")
        if message is not None:
            text = clean_message(message, strip_comments=True)             # unlike commit -m, tag -m drops lines that start with "#"
            if not text:
                raise GitError("no tag message")
            kind = self.odb.kind_of(oid)
            tagger = identity("committer", self.config, self.env, now=self.clock())
            oid = self.odb.write("tag", Tag(oid, kind, name, tagger, text).serialize())
        self.refs.update(ref, oid)
        return oid

    def delete_tag(self, name):
        ref = "refs/tags/" + name
        oid = self.refs.read(ref)
        if oid is None:
            raise GitError(f"tag '{name}' not found")
        self.refs.delete(ref)
        return oid


class NothingToCommit(GitError):
    pass


def format_date(ident):
    """``Tue Nov 14 22:13:20 2023 +0100``: git's default date, in the time zone recorded in the commit."""
    sign = -1 if ident.tz.startswith("-") else 1
    minutes = sign * (int(ident.tz[1:3]) * 60 + int(ident.tz[3:5]))
    moment = time.gmtime(ident.time + minutes * 60)
    return time.strftime("%a %b ", moment) + "%d" % moment.tm_mday + time.strftime(" %H:%M:%S %Y ", moment) + ident.tz


def _case_insensitive(gitdir):
    """git's own probe: is ``config`` also reachable as ``CONFIG``?"""
    probe = os.path.join(gitdir, "kgit-case-probe")
    try:
        with open(probe, "w"):
            pass
        return os.path.exists(os.path.join(gitdir, "KGIT-CASE-PROBE"))
    except OSError:
        return False
    finally:
        try:
            os.unlink(probe)
        except OSError:
            pass


__all__ = ["Repo", "Status", "Pathspec", "NothingToCommit", "format_date", "sort_key"]
