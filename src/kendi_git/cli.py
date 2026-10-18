"""The ``kgit`` command line.

Command names and options follow git's (so muscle memory and scripts carry over), prose output is in Turkish, machine formats (``status --porcelain``, ``ls-files
--stage``, ``log --oneline``, patches) are git's own byte for byte. ``main`` takes the streams, the environment and the clock as arguments so that tests drive the real
code paths without a subprocess.
"""
from __future__ import annotations

import argparse
import io
import itertools
import os
import sys

from . import __version__
from . import checkout as co
from . import diff as df
from . import fsck as fsck_mod
from . import history as hist
from . import index as ix
from . import remote as rm
from . import rename as rn
from . import merge as mg
from .config import ConfigFile
from .errors import GitError
from .graph import Graph
from .objects import MODE_GITLINK, MODE_TREE, hash_object, parse_tree
from .repo import NothingToCommit, Pathspec, Repo, relative_to


class Out:
    """Writes text or bytes to a text stream (through its ``buffer`` when it has one) or to a binary stream such as ``io.BytesIO``."""

    def __init__(self, stream):
        self.text = stream
        self.stream = getattr(stream, "buffer", stream)
        self.binary = hasattr(stream, "buffer") or isinstance(self.stream, (io.RawIOBase, io.BufferedIOBase))

    def write(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8", "surrogateescape")
        if self.binary:
            if self.text is not self.stream:
                self.text.flush()
            self.stream.write(data)
            self.stream.flush()
        else:
            self.stream.write(data.decode("utf-8", "surrogateescape"))

    def line(self, text=""):
        self.write(text + "\n")


# --- argument parsing ----------------------------------------------------------------------------------------------------------------------------
class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


class UsageError(Exception):
    pass


def build_parser():
    p = _Parser(prog="kgit", description="A Git client written from scratch (Turkish output, git-compatible repositories).")
    p.add_argument("-C", dest="chdir", metavar="DIR", help="run as if started in DIR")
    p.add_argument("--version", action="version", version="kgit " + __version__)
    sub = p.add_subparsers(dest="command", parser_class=_Parser)

    def cmd(name, **kw):
        return sub.add_parser(name, **kw)

    c = cmd("init", help="create an empty repository")
    c.add_argument("directory", nargs="?", default=".")
    c.add_argument("-b", "--initial-branch", default="main")
    c.add_argument("--bare", action="store_true")

    c = cmd("hash-object", help="compute (and optionally store) an object id")
    c.add_argument("-w", action="store_true", dest="write")
    c.add_argument("-t", default="blob", dest="kind")
    c.add_argument("--stdin", action="store_true")
    c.add_argument("files", nargs="*")

    c = cmd("cat-file", help="show an object")
    g = c.add_mutually_exclusive_group(required=True)
    g.add_argument("-t", action="store_true", dest="show_type")
    g.add_argument("-s", action="store_true", dest="show_size")
    g.add_argument("-p", action="store_true", dest="pretty")
    g.add_argument("-e", action="store_true", dest="exists")
    g.add_argument("--type", dest="kind", help="print the raw content of an object of this type")
    c.add_argument("object")

    c = cmd("ls-tree", help="list the contents of a tree")
    c.add_argument("-r", action="store_true", dest="recursive")
    c.add_argument("-z", action="store_true")
    c.add_argument("--name-only", action="store_true")
    c.add_argument("--full-tree", action="store_true", help="list from the repository root even from a subdirectory")
    c.add_argument("tree")

    c = cmd("ls-files", help="list the files of the index")
    c.add_argument("-s", "--stage", action="store_true")
    c.add_argument("-z", action="store_true")
    c.add_argument("-o", "--others", action="store_true")
    c.add_argument("--exclude-standard", action="store_true")
    c.add_argument("-u", "--unmerged", action="store_true")
    c.add_argument("--full-name", action="store_true", help="print paths relative to the repository root even from a subdirectory")

    cmd("write-tree", help="write the index as tree objects")

    c = cmd("rev-parse", help="resolve revisions to object ids")
    c.add_argument("--short", action="store_true")
    c.add_argument("--short-length", type=int, default=None, help=argparse.SUPPRESS)       # what "--short=N" becomes (see main)
    c.add_argument("--verify", action="store_true")
    c.add_argument("--show-toplevel", action="store_true")
    c.add_argument("--git-dir", action="store_true")
    c.add_argument("specs", nargs="*")

    c = cmd("add", help="stage files")
    c.add_argument("-u", "--update", action="store_true")
    c.add_argument("-A", "--all", action="store_true")
    c.add_argument("-f", "--force", action="store_true")
    c.add_argument("-v", "--verbose", action="store_true")
    c.add_argument("paths", nargs="*")

    c = cmd("mv", help="move or rename a file or directory")
    c.add_argument("-f", "--force", action="store_true")
    c.add_argument("paths", nargs="+")

    c = cmd("rm", help="remove files from the index and the work tree")
    c.add_argument("--cached", action="store_true")
    c.add_argument("-f", "--force", action="store_true")
    c.add_argument("-r", action="store_true", dest="recursive")
    c.add_argument("paths", nargs="*")

    c = cmd("status", help="show the state of the work tree")
    c.add_argument("-s", "--short", action="store_true")
    c.add_argument("--porcelain", action="store_true")
    c.add_argument("-z", action="store_true")
    c.add_argument("-b", "--branch", action="store_true", help="with --short, show the branch line")
    c.add_argument("--no-renames", action="store_true")
    c.add_argument("-u", "--untracked-files", nargs="?", const="all", default="normal", choices=["no", "normal", "all"], dest="untracked")

    c = cmd("commit", help="record the staged changes")
    c.add_argument("-m", "--message", action="append", dest="messages")
    c.add_argument("-a", "--all", action="store_true")
    c.add_argument("--amend", action="store_true")
    c.add_argument("--no-edit", action="store_true")
    c.add_argument("--allow-empty", action="store_true")

    c = cmd("log", help="show the history")
    _rename_args(c)
    c.add_argument("--oneline", action="store_true")
    c.add_argument("--stat", action="store_true")
    c.add_argument("--graph", action="store_true")
    c.add_argument("--topo-order", action="store_true")
    c.add_argument("--date-order", action="store_true")
    c.add_argument("--full-history", action="store_true")
    c.add_argument("--first-parent", action="store_true")
    c.add_argument("--follow", action="store_true")
    c.add_argument("-p", "--patch", action="store_true")
    c.add_argument("-c", dest="combined", action="store_true")
    c.add_argument("--cc", action="store_true")
    c.add_argument("-m", action="store_true")
    c.add_argument("-U", dest="context", type=int, default=3)
    c.add_argument("-n", "--max-count", type=int)
    c.add_argument("revs", nargs="*")

    c = cmd("show", help="show a commit, tag, tree or blob")
    _rename_args(c)
    c.add_argument("-U", dest="context", type=int, default=3)
    c.add_argument("--stat", action="store_true")
    c.add_argument("-c", dest="combined", action="store_true")
    c.add_argument("--cc", action="store_true")
    c.add_argument("object", nargs="?", default="HEAD")

    c = cmd("diff", help="show changes")
    _rename_args(c)
    c.add_argument("--cached", "--staged", action="store_true", dest="cached")
    c.add_argument("-U", dest="context", type=int, default=3)
    c.add_argument("--numstat", action="store_true")
    c.add_argument("--stat", action="store_true")
    c.add_argument("--shortstat", action="store_true")
    c.add_argument("--summary", action="store_true")
    c.add_argument("--name-only", action="store_true")
    c.add_argument("--name-status", action="store_true")
    c.add_argument("-c", dest="combined", action="store_true")
    c.add_argument("--cc", action="store_true")
    c.add_argument("--base", action="store_true")
    c.add_argument("--ours", action="store_true")
    c.add_argument("--theirs", action="store_true")
    c.add_argument("args", nargs="*")

    c = cmd("clone", help="clone a repository into a new directory")
    c.add_argument("-q", "--quiet", action="store_true")
    c.add_argument("--bare", action="store_true")
    c.add_argument("-b", "--branch")
    c.add_argument("-o", "--origin", default="origin")
    c.add_argument("-n", "--no-checkout", action="store_true")
    c.add_argument("url")
    c.add_argument("directory", nargs="?")

    c = cmd("fetch", help="download objects and refs from another repository")
    c.add_argument("-q", "--quiet", action="store_true")
    c.add_argument("--prune", "-p", action="store_true")
    c.add_argument("--tags", "-t", action="store_true")
    c.add_argument("--no-tags", action="store_true")
    c.add_argument("--force", "-f", action="store_true")
    c.add_argument("--all", action="store_true")
    c.add_argument("args", nargs="*")

    c = cmd("pull", help="fetch and merge")
    c.add_argument("-q", "--quiet", action="store_true")
    c.add_argument("--no-rebase", action="store_true")
    c.add_argument("--rebase", action="store_true")
    c.add_argument("--ff-only", action="store_true")
    c.add_argument("--no-ff", action="store_true")
    c.add_argument("--prune", "-p", action="store_true")
    c.add_argument("--tags", "-t", action="store_true")
    c.add_argument("args", nargs="*")

    c = cmd("push", help="send objects and refs to another repository")
    c.add_argument("-q", "--quiet", action="store_true")
    c.add_argument("-u", "--set-upstream", action="store_true")
    c.add_argument("-f", "--force", action="store_true")
    c.add_argument("-d", "--delete", action="store_true")
    c.add_argument("--all", action="store_true")
    c.add_argument("--tags", action="store_true")
    c.add_argument("-n", "--dry-run", action="store_true")
    c.add_argument("args", nargs="*")

    c = cmd("remote", help="manage the repositories you follow")
    c.add_argument("-v", "--verbose", action="store_true")
    c.add_argument("action", nargs="?")
    c.add_argument("args", nargs="*")

    c = cmd("ls-remote", help="list the refs of another repository")
    c.add_argument("--heads", action="store_true")
    c.add_argument("--tags", action="store_true")
    c.add_argument("url")
    c.add_argument("patterns", nargs="*")

    c = cmd("branch", help="list, create, rename or delete branches")
    c.add_argument("-d", "--delete", action="store_true")
    c.add_argument("-D", action="store_true", dest="force_delete")
    c.add_argument("-m", "--move", action="store_true")
    c.add_argument("-f", "--force", action="store_true")
    c.add_argument("-v", "--verbose", action="count", default=0)
    c.add_argument("-r", "--remotes", action="store_true")
    c.add_argument("-a", "--all", action="store_true")
    c.add_argument("-u", "--set-upstream-to", dest="set_upstream_to")
    c.add_argument("--unset-upstream", action="store_true")
    c.add_argument("names", nargs="*")

    c = cmd("checkout", help="switch branches or restore files")
    c.add_argument("-b", dest="create")
    c.add_argument("--detach", action="store_true")
    c.add_argument("--ours", action="store_true")
    c.add_argument("--theirs", action="store_true")
    c.add_argument("args", nargs="*")

    c = cmd("reset", help="move the current branch (and optionally the index and work tree)")
    g = c.add_mutually_exclusive_group()
    g.add_argument("--soft", action="store_true")
    g.add_argument("--mixed", action="store_true")
    g.add_argument("--hard", action="store_true")
    c.add_argument("target", nargs="?", default="HEAD")

    c = cmd("tag", help="list, create or delete tags")
    c.add_argument("-a", action="store_true", dest="annotate")
    c.add_argument("-m", dest="message")
    c.add_argument("-d", action="store_true", dest="delete")
    c.add_argument("-f", action="store_true", dest="force")
    c.add_argument("names", nargs="*")

    c = cmd("merge", help="merge a branch into the current one")
    c.add_argument("--no-ff", action="store_true")
    c.add_argument("--ff-only", action="store_true")
    c.add_argument("--abort", action="store_true")
    c.add_argument("--continue", action="store_true", dest="continue_")
    c.add_argument("-m", dest="message")
    c.add_argument("target", nargs="?")

    c = cmd("reflog", help="show the reference log (where a branch or HEAD has been)")
    c.add_argument("-n", "--max-count", type=int)
    c.add_argument("ref", nargs="?", default="HEAD")

    c = cmd("fsck", help="verify the object database")
    c.add_argument("--no-strict", action="store_true")
    c.add_argument("--dangling", action="store_true")

    c = cmd("config", help="read or write configuration")
    c.add_argument("--list", "-l", action="store_true", dest="list_all")
    c.add_argument("--get", action="store_true")
    c.add_argument("--global", action="store_true", dest="use_global")
    c.add_argument("args", nargs="*")

    c = cmd("dogrulama", help="run the self-verification battery against real git")
    c.add_argument("--quick", action="store_true")
    return p


# --- helpers -------------------------------------------------------------------------------------------------------------------------------------
WORKTREE_COMMANDS = {"add", "rm", "status", "commit", "checkout", "reset", "merge", "mv"}
STATUS_WORDS = {"A": "yeni dosya", "M": "değiştirilmiş", "D": "silinmiş", "T": "tür değişmiş", "U": "çakışmalı", "R": "yeniden adlandırılmış"}


def _tree_lines(repo, oid, recursive=False, names_only=False, prefix="", quote=True):
    """``git ls-tree`` lines; names with unusual bytes are C-quoted unless ``quote`` is off (``-z``)."""
    q = df.quote_path if quote else (lambda p: p)
    lines = []
    for e in parse_tree(repo.odb.read(oid)[1]):
        kind = "tree" if e.mode == MODE_TREE else "commit" if e.mode == MODE_GITLINK else "blob"
        if recursive and e.mode == MODE_TREE:
            lines += _tree_lines(repo, e.oid, True, names_only, prefix + e.name + "/", quote)
        else:
            name = q(prefix + e.name)
            lines.append(name if names_only else "%06o %s %s\t%s" % (e.mode, kind, e.oid, name))
    return lines


def print_long_status(repo, status, out, prefix=""):
    branch = status.branch
    out.line("Dal: %s" % branch if branch else "HEAD ayrık (detached): %s" % (status.head or "")[:7])
    if status.upstream:
        up, ahead, behind, gone = status.upstream
        if gone:
            out.line("Dalınız '%s' tabanlı, ancak uzak dal artık yok." % up)
        elif ahead and behind:
            out.line("Dalınız ve '%s' ayrışmış (diverged); sırasıyla %d ve %d farklı commit var." % (up, ahead, behind))
            out.line("  ('kgit pull' ile uzak dalı kendi dalına birleştir)")
        elif ahead:
            out.line("Dalınız '%s' dalının %d commit önünde." % (up, ahead))
            out.line("  ('kgit push' ile yerel commit'lerini yayımla)")
        elif behind:
            out.line("Dalınız '%s' dalının %d commit gerisinde; ileri sarılabilir (fast-forward)." % (up, behind))
            out.line("  ('kgit pull' ile yerel dalı güncelle)")
        else:
            out.line("Dalınız '%s' ile güncel." % up)
    if status.head is None:
        out.line("")
        out.line("Henüz commit yok")
    staged = [(x, p) for x, y, p in status.entries if x not in " U" and not (x == "A" and y == "U")]
    unstaged = [(y, p) for x, y, p in status.entries if y not in " " and x != "U"]
    unmerged = [p for x, y, p in status.entries if "U" in (x, y) or (x, y) in (("A", "A"), ("D", "D"))]
    if unmerged:
        out.line("")
        out.line("Çakışmalı (birleştirilmemiş) yollar:")
        for p in unmerged:
            out.line("\t" + relative_to(p, prefix))
    if staged:
        out.line("")
        out.line("Commit'e hazırlanan değişiklikler:")
        for x, p in staged:
            out.line("\t%-14s %s" % (STATUS_WORDS.get(x, x) + ":", (relative_to(status.renames[p], prefix) + " -> " if p in status.renames else "") + relative_to(p, prefix)))
    if unstaged:
        out.line("")
        out.line("Hazırlanmamış değişiklikler:")
        for y, p in unstaged:
            out.line("\t%-14s %s" % (STATUS_WORDS.get(y, y) + ":", relative_to(p, prefix)))
    if status.untracked:
        out.line("")
        out.line("Takip edilmeyen dosyalar:")
        for p in status.untracked:
            out.line("\t" + relative_to(p, prefix))
    if status.clean:
        out.line("")
        out.line("Çalışma ağacı temiz: commit edilecek bir şey yok." if status.head else "Commit edilecek bir şey yok (dosyaları 'kgit add' ile hazırla).")


def _split_revs_and_paths(repo, args, after, dashdash):
    """``[<rev> [<rev>]] [--] [<path>...]``: with ``--`` everything before it is a revision and everything after a path; without it, tokens that resolve as revisions
    come first and the rest are paths (git's rule)."""
    if dashdash:
        return args, after
    revs = []
    for i, token in enumerate(args):
        try:
            repo.rev_parse(token)
            revs.append(token)
        except GitError:
            return revs, args[i:]
        if len(revs) == 2:
            return revs, args[i + 1:]
    return revs, []


def _rename_args(c):
    c.add_argument("-M", "--find-renames", dest="find_renames", nargs="?", const="", default=None)
    c.add_argument("--no-renames", action="store_true")


def _rename_threshold(ns):
    """The minimum similarity (out of 60000) for rename detection, or None when it is off."""
    if ns.no_renames:
        return None
    if ns.find_renames:
        return rn.parse_score(ns.find_renames)
    return rn.DEFAULT_MINIMUM_SCORE


def _print_merge(repo, result, out, err):
    if result.kind == "conflict":
        for kind, path, text in result.notes:
            out.line(("Otomatik birleştirme: " + path) if kind == "auto" else ("ÇAKIŞMA: " + text))
        err.line("Otomatik birleştirme başarısız; çakışmaları düzelt, 'kgit add' ile hazırla ve 'kgit commit' ile sonuçlandır (vazgeçmek için 'kgit merge --abort').")
        return 1
    if result.kind == "up-to-date":
        out.line("Zaten güncel.")
    elif result.kind == "fast-forward":
        out.line("İleri sarma (fast-forward): %s..%s" % (repo.odb.abbrev(result.old) if result.old else "(boş)", repo.odb.abbrev(result.oid)))
    else:
        out.line("Birleştirme commit'i oluşturuldu: %s" % repo.odb.abbrev(result.oid))
    return 0


def _set_upstream_to(repo, branch, target, out):
    """``branch -u <upstream>``: the upstream is a remote-tracking branch (``origin/main``) or a local branch."""
    for cand in ("refs/remotes/" + target, "refs/heads/" + target, target):
        if cand.startswith("refs/") and repo.refs.exists(cand):
            break
    else:
        raise GitError("the requested upstream branch '%s' does not exist" % target)
    if cand.startswith("refs/heads/"):
        rm.set_upstream(repo, branch, ".", cand)
        out.line("branch '%s' set up to track '%s'." % (branch, cand[len("refs/heads/"):]))
        return
    rest = cand[len("refs/remotes/"):]
    for remote in rm.remote_names(repo):
        if rest.startswith(remote + "/"):
            for spec in rm.remote_fetch_specs(repo, remote):
                mid = rm.match_pattern(spec.dst, cand) if spec.dst else None
                if mid is not None:
                    rm.set_upstream(repo, branch, remote, spec.src.replace("*", mid))
                    out.line("branch '%s' set up to track '%s'." % (branch, rest))
                    return
    raise GitError("the requested upstream branch '%s' does not exist" % target)


def _list_branches(repo, ns, out):
    current = repo.refs.current_branch()
    rows = []
    if not ns.remotes:
        rows += [("", n, oid) for n, oid in repo.branches()]
    if ns.remotes or ns.all:
        for ref, oid in repo.refs.list("refs/remotes/"):
            raw = repo.refs.read_raw(ref)
            name = ref[len("refs/remotes/"):]
            if raw and raw[0] == "sym":
                rows.append(("remotes/" if ns.all else "", name + " -> " + raw[1][len("refs/remotes/"):], oid))
            else:
                rows.append(("remotes/" if ns.all else "", name, oid))
    width = max([len(p + n) for p, n, _o in rows if " -> " not in n] + [0])
    for prefix, name, oid in rows:
        mark = "* " if (not prefix and name == current) else "  "
        label = prefix + name
        if not ns.verbose or " -> " in name:
            out.line(mark + label)
            continue
        c = repo.read_commit(oid)
        extra = ""
        if not prefix:
            state = repo.upstream_state(name, oid)
            if state:
                up, ahead, behind, gone = state
                detail = "gone" if gone else ", ".join(([("ahead %d" % ahead)] if ahead else []) + ([("behind %d" % behind)] if behind else []))
                if ns.verbose > 1:
                    extra = "[%s%s] " % (up, ": " + detail if detail else "")
                elif detail:
                    extra = "[%s] " % detail                      # a single -v shows the counts without naming the upstream
        out.line("%s%s %s %s%s" % (mark, label.ljust(width), repo.odb.abbrev(oid), extra, c.subject))


def _remote_command(ns, repo, out):
    action = ns.action
    if action is None:
        for name in rm.remote_names(repo):
            if ns.verbose:
                url = rm.remote_url(repo, name)
                out.line("%s\t%s (fetch)" % (name, url))
                out.line("%s\t%s (push)" % (name, url))
            else:
                out.line(name)
        return 0
    args = ns.args
    if action == "add":
        if len(args) != 2:
            raise GitError("usage: kgit remote add <name> <url>")
        rm.add_remote(repo, args[0], args[1])
    elif action in ("remove", "rm"):
        if len(args) != 1:
            raise GitError("usage: kgit remote remove <name>")
        rm.remove_remote(repo, args[0])
    elif action == "rename":
        if len(args) != 2:
            raise GitError("usage: kgit remote rename <old> <new>")
        rm.rename_remote(repo, args[0], args[1])
    elif action == "get-url":
        if len(args) != 1 or args[0] not in rm.remote_names(repo):
            raise GitError("No such remote '%s'" % (args[0] if args else ""))
        out.line(rm.remote_url(repo, args[0]))
    elif action == "set-url":
        if len(args) != 2 or args[0] not in rm.remote_names(repo):
            raise GitError("No such remote '%s'" % (args[0] if args else ""))
        repo.config.local.set("remote.%s.url" % args[0], args[1])
        repo.config.local.save()
    else:
        raise GitError("unknown remote command '%s' (add, remove, rename, get-url, set-url)" % action)
    return 0


def _pull(ns, repo, out, err):
    if ns.rebase:
        raise GitError("pull --rebase is not supported (there is no rebase); use the default merge")
    cur = repo.refs.current_branch()
    res = rm.fetch(repo, ns.remote, ns.refspecs, prune=ns.prune, tags=True if ns.tags else None, action=" ".join(["pull"] + list(ns.argv_tail)))
    for line in res.lines:
        if not ns.quiet:
            err.line(line)
    heads = [h for h in res.fetch_head if h[1]]
    if not heads:
        if cur is None:
            raise GitError("You are not currently on a branch.")
        if not ns.refspecs and not rm.upstream_of(repo, cur):
            raise GitError("There is no tracking information for the current branch.")
        raise GitError("Your configuration specifies to merge with a ref that is not fetched from the remote.")
    if len(heads) > 1:
        raise GitError("pulling several branches at once (octopus merge) is not supported")
    oid, _for_merge, desc = heads[0]
    ff = "never" if ns.no_ff else "only" if ns.ff_only else "allow"
    action = " ".join(["pull"] + list(ns.argv_tail))
    result = mg.merge(repo, oid, message=_fetch_head_message(desc, repo), ff=ff, reflog_action=action)
    return _print_merge(repo, result, out, err)


def _fetch_head_message(desc, repo):
    text = "Merge " + desc
    branch = repo.refs.current_branch()
    if branch and branch not in ("main", "master"):
        text += " into " + branch
    return text


def _log_revisions(repo, tokens, dashdash):
    """The revision arguments of ``log`` (``a..b``, ``^x``, plain revisions) as ``(commit, excluded, bottom)`` in order, and, without a ``--``, the paths that follow the
    first token that is not a revision."""
    pending, i = [], 0
    while i < len(tokens):
        spec = tokens[i]
        try:
            if ".." in spec and "..." not in spec:
                a, b = spec.split("..", 1)
                pending += [(repo.commit_of(a or "HEAD"), True, True), (repo.commit_of(b or "HEAD"), False, False)]
            elif spec.startswith("^"):
                pending.append((repo.commit_of(spec[1:]), True, True))
            else:
                pending.append((repo.commit_of(spec), False, False))
        except GitError:
            if dashdash:
                raise
            break
        i += 1
    return pending, tokens[i:]


def _log_follow(ns, repo, pending, path, out):
    """``log --follow <file>``: every commit is looked at (no history simplification); one is shown when its change against its parent touches the file, and a commit
    that creates the file as a rename or copy of another one moves the search to the old name for the commits behind it."""
    walk = hist.Walk(repo, pending, None, first_parent=ns.first_parent, topo=("date" if ns.date_order else "topo") if (ns.topo_order or ns.date_order) else None)
    commits = walk.run()
    threshold = _rename_threshold(ns)
    first, shown, current = True, 0, path
    for oid in commits:
        parents = repo.read_commit(oid).parents
        if len(parents) > 1:
            continue
        parent = parents[0] if parents else None
        changes = repo.changes_commits(parent, oid, Pathspec([current]))
        switch = None
        if len(changes) == 1 and changes[0][1] is None and changes[0][0] == current and threshold is not None and parent is not None:
            new_side = changes[0][2]
            if new_side.mode in (0o100644, 0o100755):
                parent_files, current_files = repo.tree_files(repo.read_commit(parent).tree), repo.tree_files(repo.read_commit(oid).tree)
                found = rn.find_follow_source(parent_files, current_files, current, new_side, lambda m, o: df.blob_side(m, o, repo.odb), threshold)
                if found:
                    src, score = found
                    old_mode, old_oid = parent_files[src]
                    still_there = src in current_files
                    changes = [(current, df.RenamedSide(old_mode, old_oid, df.blob_side(old_mode, old_oid, repo.odb).data, src, score, still_there), new_side)]
                    switch = src
        if not changes:
            continue
        if ns.max_count is not None and shown >= ns.max_count:
            break
        shown += 1
        extra = []
        if ns.stat:
            extra.append(df.format_stat(changes).encode("utf-8", "surrogateescape"))
        if ns.patch or ns.combined or ns.cc:
            extra.append(repo.render_changes(changes, ns.context))
        extra = [e for e in extra if e]
        body = (b"\n".join(extra)).decode("utf-8", "surrogateescape").rstrip("\n").split("\n") if extra else []
        lines = ([] if ns.oneline else ["---" if len(extra) == 2 else ""]) + body if body else []
        if ns.oneline:
            out.line(repo.format_commit(oid, oneline=True))
        else:
            if not first:
                out.line("")
            out.write(repo.format_commit(oid))
        for line in lines:
            out.line(line)
        first = False
        if switch:
            current = switch
    return 0


def _cmd_log(ns, repo, out, cwd):
    pending, rest = _log_revisions(repo, ns.revs, ns.dashdash)
    paths = ns.after + rest
    if not pending:
        pending = [(repo.commit_of("HEAD"), False, False)]
    if all(u for _o, u, _b in pending):
        raise GitError("no revision to start from")
    ps = Pathspec(paths, repo.rel_cwd(cwd)) if paths else None
    if ns.follow:
        if len(paths) != 1 or ps is None or not ps.items[0][2] or not ps.items[0][1]:
            raise GitError("--follow requires exactly one plain file path")
        if ns.graph:
            raise GitError("--follow cannot be combined with --graph")
        return _log_follow(ns, repo, pending, ps.items[0][1], out)
    ordered = ns.graph or ns.topo_order or ns.date_order                  # these orders need the whole list first; --graph implies --topo-order
    walk = hist.Walk(repo, pending, ps, full_history=ns.full_history, first_parent=ns.first_parent,
                     topo=("date" if ns.date_order else "topo") if ordered else None, rewrite=ns.graph)
    commits = walk.run()
    if ns.max_count is not None:
        commits = itertools.islice(commits, ns.max_count)
    graph = Graph() if ns.graph else None
    first = True
    for oid in commits:
        dparents = walk.shown_parents(oid)
        for from_parent, extra, combined in _log_diffs(repo, ns, oid, dparents, ps):
            body = (b"\n".join(extra)).decode("utf-8", "surrogateescape").rstrip("\n").split("\n") if any(extra) else []
            if combined:
                lines = [""] + body                                       # a combined diff is always preceded by an empty line, even when it has nothing to show
            else:
                lines = ([] if ns.oneline else ["---" if len(extra) == 2 else ""]) + body if body else []
            if graph is not None:
                _log_graph_entry(repo, graph, oid, walk.graph_parents(oid), ns, out, first, lines, dparents, from_parent)
            else:
                if ns.oneline:
                    out.line(repo.format_commit(oid, oneline=True, from_parent=from_parent))
                else:
                    if not first:
                        out.line("")
                    out.write(repo.format_commit(oid, parents=dparents, from_parent=from_parent))
                for line in lines:
                    out.line(line)
            first = False
    return 0


def _log_diffs(repo, ns, oid, dparents, ps):
    """The log entries of one commit as ``(from parent or None, [diff text, ...], combined)``: normally one, with ``-m`` one for every parent of a merge."""
    merge = len(dparents) > 1
    if not (ns.stat or ns.patch or ns.combined or ns.cc):
        return [(None, [], False)]
    if merge and (ns.combined or ns.cc):
        if ns.stat:
            raise GitError("--stat together with -c/--cc is not supported for merge commits")
        return [(None, [repo.combined_diff(oid, dparents, dense=not ns.combined or ns.cc, context=ns.context, paths=ps)], True)]
    if merge and not ns.first_parent and not ns.m:
        return [(None, [], False)]
    out = []
    for parent in (dparents if merge and ns.m else dparents[:1] or [None]):
        changes = repo.detect_renames(repo.changes_commits(parent, oid, ps), _rename_threshold(ns))
        extra = []
        if ns.stat:
            extra.append(df.format_stat(changes).encode("utf-8", "surrogateescape"))
        if ns.patch or ns.combined or ns.cc:
            extra.append(repo.render_changes(changes, ns.context))
        out.append((parent if merge and ns.m else None, [e for e in extra if e], False))
    return out


def _log_graph_entry(repo, graph, oid, parents, ns, out, first, tail=(), shown=None, from_parent=None):
    """One commit of ``log --graph``: the separator row, the rows before the commit row, the commit row with its text, the rows of the other text lines, and any graph
    rows that are still owed."""
    graph.update(oid, parents)
    if not first and not ns.oneline:
        out.line(graph.padding_line())                                   # the blank line between entries also carries the graph
    while True:
        row, is_commit_row = graph.next_line()
        if is_commit_row:
            break
        out.line(row)
    text = repo.format_commit(oid, oneline=ns.oneline, parents=shown, from_parent=from_parent).rstrip("\n").split("\n")
    out.line(row + text[0])
    for line in text[1:]:
        out.line(graph.padding_line() + line)
    for rest in graph.remainder():                                       # the merge and collapsing rows come before the diff output
        out.line(rest)
    for line in tail:
        out.line(graph.padding_line() + line)


def run(ns, repo_factory, out, err, env, cwd, now, stdin=None):
    cmd = ns.command
    if cmd == "init":
        target = os.path.abspath(os.path.join(cwd, ns.directory))
        gitdir = target if ns.bare else os.path.join(target, ".git")
        existed = os.path.isdir(os.path.join(gitdir, "objects"))
        os.makedirs(target, exist_ok=True)
        Repo.init(target, branch=ns.initial_branch, env=env, now=now, bare=ns.bare)
        out.line(("Mevcut Git deposu yeniden başlatıldı: " if existed else "Boş Git deposu oluşturuldu: ") + gitdir + "/")
        return 0
    if cmd == "dogrulama":
        from . import checks
        return checks.run(out, quick=ns.quick)
    if cmd == "clone":
        out_dir = ns.directory or rm.default_dir_name(ns.url) + (".git" if ns.bare else "")
        if not ns.quiet:
            err.line("Cloning into %s'%s'..." % ("bare repository " if ns.bare else "", out_dir))
        repo, notes = rm.clone(ns.url, ns.directory, bare=ns.bare, branch=ns.branch, origin=ns.origin, checkout=not ns.no_checkout, env=env, now=now, cwd=cwd)
        for note in notes:
            err.line(note)
        return 0
    if cmd == "ls-remote":
        from .transport import open_transport
        t = open_transport(ns.url, None, env, cwd=cwd)
        try:
            for name, oid, _target, peeled in t.ls_refs(None):
                if ns.heads and not name.startswith("refs/heads/"):
                    continue
                if ns.tags and not name.startswith("refs/tags/"):
                    continue
                if ns.patterns and not any(name == p or name.endswith("/" + p) or rm.match_pattern(p, name) is not None for p in ns.patterns):
                    continue
                out.line("%s\t%s" % (oid, name))
                if peeled:
                    out.line("%s\t%s^{}" % (peeled, name))
        finally:
            t.close()
        return 0
    repo = repo_factory()
    if repo.worktree is None and cmd in WORKTREE_COMMANDS:
        raise GitError("this operation must be run in a work tree (this is a bare repository)")
    if cmd == "hash-object":
        files = list(ns.files)
        if ns.stdin:
            data_list = [(stdin or sys.stdin.buffer).read()]
        else:
            data_list = []
            for f in files:
                with open(os.path.join(cwd, f), "rb") as fh:
                    data_list.append(fh.read())
        for data in data_list:
            oid = repo.odb.write(ns.kind, data) if ns.write else hash_object(ns.kind, data)
            out.line(oid)
        return 0
    if cmd == "cat-file":
        oid = repo.rev_parse(ns.object)
        if ns.exists:
            return 0 if repo.odb.exists(oid) else 1
        kind, data = repo.odb.read(oid)
        if ns.show_type:
            out.line(kind)
        elif ns.show_size:
            out.line(str(len(data)))
        elif ns.kind:
            if ns.kind != kind:
                raise GitError(f"object {oid} is a {kind}, not a {ns.kind}")
            out.write(data)
        elif kind == "tree":
            for line in _tree_lines(repo, oid):
                out.line(line)
        else:
            out.write(data)
        return 0
    if cmd == "ls-tree":
        oid = repo.peel(repo.rev_parse(ns.tree), "tree")
        prefix = repo.rel_cwd(cwd)
        if prefix and not ns.full_tree:                           # like git, list the directory we are in
            oid = repo.rev_parse(oid + ":" + prefix)
        sep = "\0" if ns.z else "\n"
        for line in _tree_lines(repo, oid, ns.recursive, ns.name_only, quote=not ns.z):
            out.write(line + sep)
        return 0
    if cmd == "ls-files":
        index = repo.read_index()
        sep = "\0" if ns.z else "\n"
        prefix = repo.rel_cwd(cwd)
        here = (lambda p: p.startswith(prefix + "/")) if prefix else (lambda p: True)
        shown = (lambda p: p) if (ns.full_name or not prefix) else (lambda p: p[len(prefix) + 1:])
        if ns.others:
            for p in repo.walk_untracked_candidates(index, ignore=ns.exclude_standard):
                if here(p):
                    out.write((shown(p) if ns.z else df.quote_path(shown(p))) + sep)
            return 0
        for e in sorted(index.entries.values(), key=ix.IndexEntry.key):
            if not here(e.path) or (ns.unmerged and e.stage == 0):
                continue
            path = shown(e.path) if ns.z else df.quote_path(shown(e.path))
            out.write(("%06o %s %d\t%s" % (e.mode, e.oid, e.stage, path) if (ns.stage or ns.unmerged) else path) + sep)
        return 0
    if cmd == "write-tree":
        out.line(ix.index_to_tree(repo.odb, repo.read_index()))
        return 0
    if cmd == "rev-parse":
        if ns.show_toplevel:
            out.line(repo.worktree)
        if ns.git_dir:
            out.line(repo.gitdir)
        for spec in ns.specs:
            oid = repo.rev_parse(spec)
            out.line(repo.odb.abbrev(oid, ns.short_length) if (ns.short or ns.short_length) else oid)
        return 0
    if cmd == "add":
        paths, prefix = ns.paths + ns.after, repo.rel_cwd(cwd)
        if not paths and (ns.update or ns.all):                    # "git add -u" and "git add -A" without a path act on the whole work tree
            paths, prefix = ["."], ""
        if not paths:
            raise GitError("nothing specified, nothing added")
        staged, removed, warnings = repo.add(paths, prefix=prefix, update=ns.update, force=ns.force)
        for w in warnings:
            err.line("uyarı: " + w)
        if ns.verbose:
            for p in staged:
                out.line("add '%s'" % p)
            for p in removed:
                out.line("remove '%s'" % p)
        return 0
    if cmd == "mv":
        paths = ns.paths + ns.after
        if len(paths) < 2:
            raise GitError("usage: kgit mv <source>... <destination>")
        repo.mv(paths[:-1], paths[-1], prefix=repo.rel_cwd(cwd), force=ns.force)
        return 0
    if cmd == "rm":
        paths = ns.paths + ns.after
        if not paths:
            raise GitError("no pathspec given: which files should I remove?")
        for p in repo.rm(paths, prefix=repo.rel_cwd(cwd), cached=ns.cached, force=ns.force, recursive=ns.recursive):
            out.line("rm '%s'" % p)
        return 0
    if cmd == "status":
        status = repo.status(untracked=ns.untracked, renames=not ns.no_renames)
        if ns.porcelain or ns.short:
            if ns.z:
                for x, y, p in status.entries:
                    out.write("%s%s %s\0%s" % (x, y, p, status.renames[p] + "\0" if p in status.renames else ""))
                for p in status.untracked:
                    out.write("?? %s\0" % p)
            else:
                if ns.branch:
                    head = "No commits yet on %s" % status.branch if status.head is None and status.branch else status.branch or "HEAD (no branch)"
                    if status.upstream:
                        up, ahead, behind, gone = status.upstream
                        detail = "gone" if gone else ", ".join(([("ahead %d" % ahead)] if ahead else []) + ([("behind %d" % behind)] if behind else []))
                        head += "...%s%s" % (up, " [%s]" % detail if detail else "")
                    out.line("## " + head)
                for line in status.porcelain_lines(prefix="" if ns.porcelain else repo.rel_cwd(cwd)):
                    out.line(line)
        else:
            print_long_status(repo, status, out, repo.rel_cwd(cwd))
        return 0
    if cmd == "commit":
        if not ns.messages and not ns.amend and not (mg.merge_in_progress(repo) and getattr(ns, "no_edit", False)):
            raise GitError("a commit message is required: use -m \"message\" (no editor is started)" + (", or --no-edit to take the prepared merge message" if mg.merge_in_progress(repo) else ""))
        message = "\n\n".join(ns.messages) if ns.messages else None
        if ns.all:
            repo.add(["."], prefix="", update=True)
        before = repo.refs.head_oid()
        try:
            oid = repo.commit(message, amend=ns.amend, allow_empty=ns.allow_empty)
        except NothingToCommit:
            print_long_status(repo, repo.status(), out, repo.rel_cwd(cwd))
            err.line("kgit: commit edilecek değişiklik yok")
            return 1
        c = repo.read_commit(oid)
        branch = repo.refs.current_branch() or "HEAD (detached)"
        out.line("[%s%s %s] %s" % (branch, " (root-commit)" if before is None and not ns.amend else "", repo.odb.abbrev(oid), c.subject))
        return 0
    if cmd == "log":
        return _cmd_log(ns, repo, out, cwd)
    if cmd == "show":
        oid = repo.rev_parse(ns.object)
        kind, data = repo.odb.read(oid)
        if kind == "commit" and ns.stat:
            out.write(repo.format_commit(oid))
            stat = df.format_stat(repo.detect_renames(repo.commit_changes(oid), _rename_threshold(ns))) if len(repo.read_commit(oid).parents) < 2 else ""
            out.write(("\n" + stat) if stat else "")
        elif kind == "commit":
            out.write(repo.show_commit(oid, ns.context, dense=not ns.combined or ns.cc, renames=_rename_threshold(ns)))
        elif kind == "tag":
            out.write(repo.show_tag(oid, ns.context, renames=_rename_threshold(ns)))
        elif kind == "tree":
            out.line("tree %s\n" % ns.object)
            for line in _tree_lines(repo, oid, names_only=True):
                out.line(line)
        else:
            out.write(data)
        return 0
    if cmd == "diff":
        revs, paths = _split_revs_and_paths(repo, ns.args, ns.after, ns.dashdash)
        if len(revs) > 2:
            raise GitError("too many revisions: diff takes at most two")
        ps = Pathspec(paths, repo.rel_cwd(cwd)) if paths else None
        if len(revs) == 2:
            changes = repo.changes_commits(repo.commit_of(revs[0]), repo.commit_of(revs[1]), ps)
        elif len(revs) == 1:
            commit = repo.commit_of(revs[0])
            changes = repo.changes_cached(commit, ps) if ns.cached else repo.changes_against_commit(commit, ps)
        else:
            stage = 1 if ns.base else 2 if ns.ours else 3 if ns.theirs else 0
            changes = repo.changes_cached(None, ps) if ns.cached else repo.changes_worktree(ps, stage)
        changes = repo.detect_renames(changes, _rename_threshold(ns))
        unmerged = any(isinstance(old, df.Unmerged) for _p, old, _n in changes)
        if unmerged and (ns.stat or ns.shortstat or ns.summary or ns.numstat):
            raise GitError("--stat, --numstat and --summary are not supported while there are unmerged paths")
        if unmerged and (ns.name_only or ns.name_status):
            for path, old, new in changes:
                quoted = df.quote_path(path)
                if isinstance(old, df.Unmerged):
                    out.line("U\t" + quoted if ns.name_status else quoted)
                    work = (old.worktree.mode, old.worktree.oid) if old.worktree is not None else None
                    want = old.stage or 2
                    if old.cached:
                        status = None
                    elif not old.stage and 2 in old.stages and 3 in old.stages:
                        status = "D" if work is None else "M" if work not in (old.stages[2], old.stages[3]) else None
                    elif want in old.stages and work != old.stages[want]:
                        status = "D" if work is None else "M"
                    else:
                        status = None
                    if status:
                        out.line(status + "\t" + quoted if ns.name_status else quoted)
                elif ns.name_status:
                    out.line("%s\t%s" % ("A" if old is None else "D" if new is None else "M", quoted))
                else:
                    out.line(quoted)
        elif ns.stat or ns.shortstat or ns.summary:
            out.write((df.format_stat(changes) if ns.stat else df.format_shortstat(changes) if ns.shortstat else "") + (df.format_summary(changes) if ns.summary else ""))
        elif ns.numstat or ns.name_only or ns.name_status:
            for path, old, new in df.merge_typechanges(changes):
                quoted = df.quote_path(path)
                if ns.name_only:
                    out.line(quoted)
                elif ns.name_status:
                    if isinstance(old, df.RenamedSide):
                        out.line("R%03d\t%s\t%s" % (old.similarity, df.quote_path(old.path), quoted))
                    else:
                        out.line("%s\t%s" % ("A" if old is None else "D" if new is None else "T" if (old.mode == 0o120000) != (new.mode == 0o120000) else "M", quoted))
                else:
                    counted = df.counts(old, new)
                    out.line("%s\t%s\t%s" % (counted[0] if counted else "-", counted[1] if counted else "-", df.rename_label(old.path, path) if isinstance(old, df.RenamedSide) else quoted))
        else:
            out.write(repo.render_changes(changes, ns.context, dense=not ns.combined or ns.cc))
        return 0
    if cmd == "remote":
        return _remote_command(ns, repo, out)
    if cmd == "fetch":
        names = rm.remote_names(repo) if ns.all else [ns.remote]
        for name in names:
            res = rm.fetch(repo, name, ns.refspecs, prune=ns.prune, tags=True if ns.tags else False if ns.no_tags else None, force=ns.force,
                           reflog_args=list(ns.argv_tail))
            for line in res.lines:
                if not ns.quiet:
                    err.line(line)
        return 0
    if cmd == "pull":
        return _pull(ns, repo, out, err)
    if cmd == "push":
        res = rm.push(repo, ns.remote, ns.refspecs, force=ns.force, delete=ns.delete, set_upstream_flag=ns.set_upstream, all_branches=ns.all, tags=ns.tags, dry_run=ns.dry_run)
        for line in res.errors:
            err.line("error: " + line)
        for line in res.lines:
            if not ns.quiet:
                err.line(line)
        if not res.lines and res.up_to_date and not ns.quiet:
            err.line("Everything up-to-date")
        if not res.ok:
            err.line("error: failed to push some refs to '%s'" % res.url)
            for line in res.hints:
                err.line("hint: " + line)
            return 1
        return 0
    if cmd == "branch":
        names = ns.names
        if ns.delete or ns.force_delete:
            for n in names:
                oid = repo.delete_branch(n, force=ns.force_delete)
                out.line("Dal silindi: %s (%s)" % (n, repo.odb.abbrev(oid)))
        elif ns.move:
            if len(names) == 1:
                names = [repo.refs.current_branch() or "", names[0]]
            repo.rename_branch(names[0], names[1])
        elif ns.set_upstream_to or ns.unset_upstream:
            branch = names[0] if names else repo.refs.current_branch()
            if branch is None or repo.refs.read("refs/heads/" + branch) is None:
                raise GitError("branch '%s' does not exist" % (branch or "HEAD"))
            if ns.unset_upstream:
                if not rm.upstream_of(repo, branch):
                    raise GitError("branch '%s' has no upstream information" % branch)
                repo.config.local.unset("branch.%s.remote" % branch)
                repo.config.local.unset("branch.%s.merge" % branch)
                repo.config.local.save()
            else:
                _set_upstream_to(repo, branch, ns.set_upstream_to, out)
        elif names:
            repo.create_branch(names[0], names[1] if len(names) > 1 else None, force=ns.force)
        else:
            _list_branches(repo, ns, out)
        for note in repo.notes:
            out.line(note)
        return 0
    if cmd == "checkout":
        prefix = repo.rel_cwd(cwd)
        if ns.ours or ns.theirs:
            paths = ns.after + ns.args if not ns.dashdash else ns.after
            for p in co.restore_stage(repo, paths, prefix, 2 if ns.ours else 3):
                out.line("geri yüklendi: " + p)
            return 0
        if ns.dashdash:
            if len(ns.args) > 1 or not ns.after:
                raise GitError("usage: kgit checkout [<revision>] -- <path>...")
            for p in co.restore(repo, ns.after, prefix, ns.args[0] if ns.args else None):
                out.line("geri yüklendi: " + p)
            return 0
        target = ns.args[0] if ns.args else None
        if ns.create and len(ns.args) > 1:
            raise GitError("too many arguments")
        result = co.checkout(repo, target, create=ns.create, detach=ns.detach)
        out.line({"already": "Zaten '%s' dalındasın." % result.name, "branch": "'%s' dalına geçildi." % result.name,
                  "new-branch": "Yeni '%s' dalı oluşturuldu ve ona geçildi." % result.name,
                  "detached": "HEAD ayrık (detached): %s %s" % (repo.odb.abbrev(result.oid), result.subject)}[result.kind])
        for note in repo.notes:
            out.line(note)
        return 0
    if cmd == "reset":
        mode = "soft" if ns.soft else "hard" if ns.hard else "mixed"
        oid, subject = co.reset(repo, ns.target, mode)
        out.line("HEAD artık şurada: %s %s" % (repo.odb.abbrev(oid), subject))
        return 0
    if cmd == "tag":
        names = ns.names
        if ns.delete:
            for n in names:
                repo.delete_tag(n)
                out.line("Etiket silindi: " + n)
        elif names:
            if ns.annotate and not ns.message:
                raise GitError("an annotated tag needs a message: use -m \"message\" (no editor is started)")
            repo.create_tag(names[0], names[1] if len(names) > 1 else None, ns.message if (ns.annotate or ns.message) else None, force=ns.force)
        else:
            for n, _oid in repo.tags():
                out.line(n)
        return 0
    if cmd == "merge":
        ff = "never" if ns.no_ff else "only" if ns.ff_only else "allow"
        if ns.abort:
            mg.abort(repo)
            out.line("Birleştirme iptal edildi; HEAD'in durumu geri yüklendi.")
            return 0
        if ns.continue_:
            return run(argparse.Namespace(command="commit", messages=None, all=False, amend=False, allow_empty=False, no_edit=True), repo_factory, out, err, env, cwd, now)
        if not ns.target:
            raise GitError("usage: kgit merge [--no-ff|--ff-only] <revision>  |  kgit merge --abort  |  kgit merge --continue")
        result = mg.merge(repo, ns.target, message=ns.message, ff=ff)
        return _print_merge(repo, result, out, err)
    if cmd == "reflog":
        ref = ns.ref
        if ref != "HEAD" and not ref.startswith("refs/"):
            ref = next((c for c in ("refs/heads/" + ref, "refs/remotes/" + ref, "refs/tags/" + ref) if os.path.exists(repo.refs._log_path(c))), "refs/heads/" + ref)
        entries = repo.refs.read_log(ref)
        for n, (_old, new, _ident, _when, _tz, message) in enumerate(reversed(entries)):
            if ns.max_count is not None and n >= ns.max_count:
                break
            out.line("%s %s@{%d}: %s" % (repo.odb.abbrev(new), ns.ref, n, message))
        return 0
    if cmd == "fsck":
        report = fsck_mod.fsck(repo, strict=not ns.no_strict)
        for line in report.errors:
            err.line(line)
        if ns.dangling:
            for kind, oid in report.dangling:
                out.line("dangling %s %s" % (kind, oid))
        out.line("%d nesne denetlendi; %s" % (report.objects, "sorun yok." if report.ok else "%d sorun bulundu." % len(report.errors)))
        return 0 if report.ok else 1
    if cmd == "config":
        return _config(ns, repo_factory, out, env, cwd)
    raise GitError(f"unknown command {cmd!r}")


def _config(ns, repo_factory, out, env, cwd):
    if ns.use_global:
        path = env.get("GIT_CONFIG_GLOBAL") or os.path.join(env.get("HOME") or os.path.expanduser("~"), ".gitconfig")
        cfg = ConfigFile(path)
        layers = [cfg]
    else:
        repo = repo_factory()
        layers = None
        cfg = repo.config.local
    if ns.list_all:
        source = layers or repo.config.layers
        for layer in source:
            for key, value in layer.entries():
                out.line(key if value is None else "%s=%s" % (key, value))
        return 0
    if not ns.args:
        raise GitError("usage: kgit config [--global] [--get] <key> [<value>]  |  kgit config --list")
    key = ns.args[0]
    if len(ns.args) == 1 or ns.get:
        values = cfg.get_all(key) if ns.use_global else repo.config.get_all(key)
        if not values:
            return 1
        out.line("" if values[-1] is None else values[-1])
        return 0
    cfg.set(key, ns.args[1])
    cfg.save()
    return 0


def main(argv=None, stdout=None, stderr=None, cwd=None, env=None, now=None, stdin=None):
    argv = ["--short-length=" + a[len("--short="):] if a.startswith("--short=") else a for a in (sys.argv[1:] if argv is None else argv)]
    out, err = Out(stdout or sys.stdout), Out(stderr or sys.stderr)
    env = dict(os.environ if env is None else env)
    cwd = os.path.abspath(cwd or os.getcwd())
    try:
        split = argv.index("--") if "--" in argv else None            # "--" separates revisions from paths; argparse would swallow it, so it is handled here
        for cmd_word in ("fetch", "pull", "push"):                    # their options may come after the remote and the refspecs: move them in front
            if cmd_word in argv[:(split if split is not None else len(argv))]:
                at = argv.index(cmd_word)
                rest = argv[at + 1:(split if split is not None else len(argv))]
                flags = [a for a in rest if a.startswith("-") and len(a) > 1]
                words = [a for a in rest if not (a.startswith("-") and len(a) > 1)]
                tail = argv[split:] if split is not None else []
                argv = argv[:at + 1] + flags + words + tail
                split = argv.index("--") if "--" in argv else None
                break
        ns = build_parser().parse_args(argv if split is None else argv[:split])
        ns.dashdash, ns.after = split is not None, ([] if split is None else argv[split + 1:])
        if ns.command in ("fetch", "pull", "push"):
            ns.remote, ns.refspecs = (ns.args[0] if ns.args else None), ns.args[1:]
        head = argv if split is None else argv[:split]
        ns.argv_tail = head[head.index(ns.command) + 1:] if ns.command in head else []       # what followed the command word (git writes it into reflog messages)
        if ns.chdir:
            cwd = os.path.abspath(os.path.join(cwd, ns.chdir))
        if ns.command is None:
            build_parser().print_help(stderr or sys.stderr)
            return 2
        factory = lambda: Repo.discover(cwd, env=env, now=now)  # noqa: E731
        return run(ns, factory, out, err, env, cwd, now, stdin)
    except UsageError as exc:
        err.line("kgit: kullanım hatası: %s" % exc)
        return 2
    except GitError as exc:
        err.line("kgit: hata: %s" % exc)
        return 1
    except BrokenPipeError:
        return 1
    except (OSError, KeyboardInterrupt) as exc:
        err.line("kgit: hata: %s" % exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["main", "build_parser"]
