"""Remotes: configuration, refspecs, fetch, push, clone, pull and the upstream bookkeeping of branches.

A remote is a ``[remote "name"]`` section (``url`` and ``fetch`` refspecs). A refspec ``+src:dst`` says that the ref ``src`` on one side is copied to ``dst`` on the other
(the ``+`` allows a move that is not a fast-forward); either side may hold one ``*``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import checkout as co
from . import index as ix
from . import packio
from . import protocol as pr
from .errors import GitError
from .objects import is_oid
from .transport import collect_objects, open_transport

ZERO = "0" * 40


# --- refspecs ----------------------------------------------------------------------------------------------------------------------------------------
@dataclass
class Refspec:
    src: str
    dst: object            # None when the spec only names a source
    force: bool = False

    @property
    def is_pattern(self):
        return "*" in self.src

    def __str__(self):
        return ("+" if self.force else "") + self.src + (":" + self.dst if self.dst is not None else "")


def parse_refspec(text):
    force = text.startswith("+")
    if force:
        text = text[1:]
    if ":" in text:
        src, dst = text.split(":", 1)
    else:
        src, dst = text, None
    if (src.count("*") > 1) or (dst is not None and dst.count("*") > 1) or ((src.count("*") == 1) != (dst is not None and dst.count("*") == 1) and dst):
        raise GitError("invalid refspec '%s'" % text)
    return Refspec(src, dst, force)


def match_pattern(pattern, name):
    """The text the ``*`` of ``pattern`` stands for in ``name``, ``""`` for an exact match of a pattern without ``*``, or ``None``."""
    if "*" not in pattern:
        return "" if pattern == name else None
    pre, post = pattern.split("*", 1)
    if name.startswith(pre) and name.endswith(post) and len(name) >= len(pre) + len(post):
        return name[len(pre):len(name) - len(post)]
    return None


# --- configuration -------------------------------------------------------------------------------------------------------------------------------------
def remote_names(repo):
    names = []
    for layer in repo.config.layers:
        for key, _v in layer.entries():
            if key.startswith("remote.") and key.count(".") >= 2:
                name = key[len("remote."):key.rindex(".")]
                if name not in names:
                    names.append(name)
    return names


def remote_url(repo, name):
    return repo.config.get("remote.%s.url" % name)


def remote_fetch_specs(repo, name):
    specs = [v for v in repo.config.get_all("remote.%s.fetch" % name) if v]
    return [parse_refspec(v) for v in specs]


def add_remote(repo, name, url, fetch_specs=None):
    if name in remote_names(repo):
        raise GitError("remote %s already exists." % name)
    if not name or "/" in name.strip("/") and False:
        raise GitError("'%s' is not a valid remote name" % name)
    cfg = repo.config.local
    cfg.set("remote.%s.url" % name, url)
    for spec in fetch_specs or ["+refs/heads/*:refs/remotes/%s/*" % name]:
        cfg.add("remote.%s.fetch" % name, spec)
    cfg.save()


def remove_remote(repo, name):
    if name not in remote_names(repo):
        raise GitError("No such remote: '%s'" % name)
    for ref, _oid in repo.refs.list("refs/remotes/%s/" % name):
        repo.refs.delete(ref)
    cfg = repo.config.local
    cfg.remove_section("remote", name)
    for branch_key in [k for k, _v in cfg.entries() if k.startswith("branch.") and k.endswith(".remote")]:
        if cfg.get_all(branch_key) == [name]:
            branch = branch_key[len("branch."):-len(".remote")]
            cfg.unset("branch.%s.remote" % branch)
            cfg.unset("branch.%s.merge" % branch)
    cfg.save()


def rename_remote(repo, old, new):
    if old not in remote_names(repo):
        raise GitError("No such remote: '%s'" % old)
    if new in remote_names(repo):
        raise GitError("remote %s already exists." % new)
    cfg = repo.config.local
    cfg.rename_section("remote", old, new)
    specs = remote_fetch_specs(repo, new)
    cfg.unset("remote.%s.fetch" % new)
    for spec in specs:
        text = str(spec).replace("refs/remotes/%s/" % old, "refs/remotes/%s/" % new)
        cfg.add("remote.%s.fetch" % new, text)
    for key in [k for k, _v in cfg.entries() if k.startswith("branch.") and k.endswith(".remote")]:
        if cfg.get_all(key) == [old]:
            cfg.set(key, new)
    cfg.save()
    for ref, oid in repo.refs.list("refs/remotes/%s/" % old):
        newref = "refs/remotes/%s/%s" % (new, ref[len("refs/remotes/%s/" % old):])
        repo.refs.update(newref, oid)
        repo.refs.move_log(ref, newref) if hasattr(repo.refs, "move_log") else None
        repo.refs.delete(ref)


def resolve_url(repo, url):
    """A relative local path in a remote's URL is taken from the top of the work tree."""
    if "://" in url or os.path.isabs(url):
        return url
    base = repo.worktree or repo.gitdir
    return os.path.normpath(os.path.join(base, url))


def anonymize(url):
    """A URL without the user name and password that may be in front of the host."""
    if "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    host, slash, path = rest.partition("/")
    if "@" in host:
        host = host.rsplit("@", 1)[1]
    return scheme + "://" + host + slash + path


def tidy_url(url):
    """How git writes a URL in ``From ...`` lines and in FETCH_HEAD: no credentials, no trailing slashes and no ``.git`` suffix."""
    url = anonymize(url).rstrip("/")
    return url[:-4] if url.endswith(".git") and len(url) > 4 else url


# --- upstream tracking -----------------------------------------------------------------------------------------------------------------------------------
def upstream_of(repo, branch):
    """``(remote, merge ref, remote-tracking ref)`` that ``branch`` follows, or ``None``."""
    remote = repo.config.get("branch.%s.remote" % branch)
    merge = repo.config.get("branch.%s.merge" % branch)
    if not remote or not merge:
        return None
    if remote == ".":
        return remote, merge, merge
    tracking = None
    for spec in remote_fetch_specs(repo, remote):
        if spec.dst is None:
            continue
        mid = match_pattern(spec.src, merge)
        if mid is not None:
            tracking = spec.dst.replace("*", mid) if "*" in spec.dst else spec.dst
            break
    return remote, merge, tracking


def set_upstream(repo, branch, remote, merge):
    cfg = repo.config.local
    cfg.set("branch.%s.remote" % branch, remote)
    cfg.set("branch.%s.merge" % branch, merge)
    cfg.save()


def ahead_behind(repo, local, upstream):
    """``(ahead, behind)``: commits only the local tip has, commits only the upstream tip has."""
    a = repo.reachable([local])
    b = repo.reachable([upstream])
    return len(a - b), len(b - a)


# --- fetch ----------------------------------------------------------------------------------------------------------------------------------------------------
@dataclass
class FetchResult:
    lines: list = field(default_factory=list)       # what git prints on stderr
    updated: list = field(default_factory=list)     # (local ref, old, new)
    rejected: list = field(default_factory=list)
    fetch_head: list = field(default_factory=list)
    pruned: list = field(default_factory=list)


def _what_kind(ref):
    if ref.startswith("refs/heads/"):
        return "branch", ref[len("refs/heads/"):]
    if ref.startswith("refs/tags/"):
        return "tag", ref[len("refs/tags/"):]
    if ref.startswith("refs/remotes/"):
        return "remote-tracking branch", ref[len("refs/remotes/"):]
    return None, ref


def _short(ref):
    for p in ("refs/heads/", "refs/tags/", "refs/remotes/", "refs/"):
        if ref.startswith(p):
            return ref[len(p):]
    return ref


def _resolve_source(remote_refs, name):
    """The remote ref a source name stands for: exact, then the usual abbreviations (``refs/tags/x``, ``refs/heads/x``, ...)."""
    for candidate in (name, "refs/" + name, "refs/tags/" + name, "refs/heads/" + name, "refs/remotes/" + name, "refs/remotes/%s/HEAD" % name):
        if candidate in remote_refs:
            return candidate
    return None


def fetch(repo, remote=None, refspecs=(), prune=False, tags=None, force=False, depth=None, reflog_args=None, cloning=False, specs=None, url=None, action=None):
    """Fetch from a configured remote or a URL. ``tags``: ``None`` follows tags that point into the fetched history, ``True`` fetches all, ``False`` none."""
    if remote is None:
        branch = repo.refs.current_branch()
        remote = (repo.config.get("branch.%s.remote" % branch) if branch else None) or "origin"
        if remote == ".":
            raise GitError("fetching from '.' is not supported")
    named = remote in remote_names(repo)
    if url is None:
        url = resolve_url(repo, remote_url(repo, remote)) if named else remote
    if named and not url:
        raise GitError("remote '%s' has no url" % remote)
    if not named and "/" not in remote and "://" not in remote and not os.path.exists(remote):
        raise GitError("'%s' does not appear to be a git repository (and there is no remote of that name)" % remote)
    cli_specs = [parse_refspec(s) for s in refspecs]
    if specs is None:
        specs = cli_specs or (remote_fetch_specs(repo, remote) if named else [])
    if not specs:
        specs = [Refspec("HEAD", None)]
    result = FetchResult()
    t = open_transport(url, repo, repo.env, cwd=repo.worktree or repo.gitdir)
    try:
        prefixes = {"HEAD", "refs/tags/"}
        simple = False
        for sp in specs:
            if sp.is_pattern:
                prefixes.add(sp.src[:sp.src.index("*")])
            elif sp.src.startswith("refs/"):
                prefixes.add(sp.src)
            else:
                simple = True
        listing = t.ls_refs(None if simple else sorted(prefixes))
        remote_refs = {name: oid for name, oid, _t, _p in listing}
        peeled = {name: p for name, _oid, _t, p in listing if p}
        wanted = []        # (remote ref, oid, local ref or None, force, for_merge)
        upstream_merge = None
        cur = repo.refs.current_branch()
        if cur and not cli_specs and repo.config.get("branch.%s.remote" % cur) == remote:
            upstream_merge = repo.config.get("branch.%s.merge" % cur)
        for sp in specs:
            if sp.is_pattern:
                for name in sorted(remote_refs, key=lambda n: n.encode()):
                    mid = match_pattern(sp.src, name)
                    if mid is None or name == "HEAD":
                        continue
                    local = sp.dst.replace("*", mid) if sp.dst else None
                    wanted.append((name, remote_refs[name], local, sp.force or force, bool(cli_specs) or name == upstream_merge))
            else:
                name = "HEAD" if sp.src == "HEAD" else _resolve_source(remote_refs, sp.src)
                if name is None:
                    raise GitError("couldn't find remote ref %s" % sp.src)
                local = sp.dst
                if local is not None and not local.startswith("refs/"):
                    local = ("refs/tags/" if name.startswith("refs/tags/") else "refs/heads/") + local
                wanted.append((name, remote_refs[name], local, sp.force or force, bool(cli_specs) or name == upstream_merge))
        if cli_specs and named:
            # a ref fetched by name also moves its configured remote-tracking branch (not listed in FETCH_HEAD)
            taken = {w[2] for w in wanted if w[2]}
            for name, oid, _local, _f, _m in list(wanted):
                for cfg_spec in remote_fetch_specs(repo, remote):
                    mid = match_pattern(cfg_spec.src, name)
                    if mid is None or cfg_spec.dst is None:
                        continue
                    local = cfg_spec.dst.replace("*", mid) if "*" in cfg_spec.dst else cfg_spec.dst
                    if local not in taken:
                        taken.add(local)
                        wanted.append((name, oid, local, cfg_spec.force or force, None))
        if tags is True and not any(sp.src.startswith("refs/tags/") for sp in specs):
            for name in sorted(remote_refs, key=lambda n: n.encode()):
                if name.startswith("refs/tags/"):
                    wanted.append((name, remote_refs[name], name, False, False))
        _transfer(repo, t, wanted, result, depth)
        if tags is None and (not cli_specs or any(sp.dst for sp in cli_specs)):
            have_names = {w[0] for w in wanted}
            follow = []
            for name in sorted(remote_refs, key=lambda n: n.encode()):
                if not name.startswith("refs/tags/") or name in have_names:
                    continue
                target = peeled.get(name) or remote_refs[name]
                if repo.refs.read(name) is None and repo.odb.exists(target):
                    follow.append((name, remote_refs[name], name, False, False))
            if follow:
                _transfer(repo, t, follow, result, depth)
                wanted += follow
        if prune:
            _prune(repo, specs, remote_refs, result)
        action = action or "fetch" + "".join(" " + a for a in (reflog_args or []))
        _apply(repo, wanted, remote_refs, result, url, action, cloning)
        if not cloning:
            _write_fetch_head(repo, result, url)
    finally:
        t.close()
    return result


def _transfer(repo, t, wanted, result, depth):
    needed = []
    for _name, oid, _local, _f, _m in wanted:
        if oid not in needed and not repo.odb.exists(oid):
            needed.append(oid)
    if not needed:
        return
    haves = _haves(repo)
    chunks = t.fetch(needed, haves, depth)
    packio.receive_pack(chunks, repo.odb, os.path.join(repo.gitdir, "objects"))
    missing = [o for o in needed if not repo.odb.exists(o)]
    if missing:
        raise GitError("the server did not send the object %s" % missing[0])


def _haves(repo, limit=512):
    """Commits the other side is told about: our ref tips and a stretch of history behind them."""
    tips = []
    for prefix in ("refs/heads/", "refs/remotes/", "refs/tags/"):
        for _name, oid in repo.refs.list(prefix):
            try:
                tips.append(repo.peel(oid, "commit"))
            except GitError:
                continue
    head = repo.refs.head_oid()
    if head:
        tips.append(head)
    out = []
    for oid in repo.walk(list(dict.fromkeys(tips)), limit=limit) if tips else []:
        out.append(oid)
    return out


def _is_ancestor(repo, old, new):
    return old in repo.reachable([new])


def _apply(repo, wanted, remote_refs, result, url, action, cloning=False):
    """Move the local refs: report each change like git does and fill FETCH_HEAD."""
    shown = tidy_url(url)
    lines = []
    merge_lines, other_lines = [], []
    entries = []
    for name, oid, local, force, for_merge in [w for w in wanted if w[4]] + [w for w in wanted if not w[4]]:          # the branch to merge comes first (None counts as no)
        kind, short = _what_kind(name)
        desc = "%s '%s' of %s" % (kind, short, shown) if kind else (shown if name == "HEAD" else "'%s' of %s" % (name, shown))
        row = (oid, for_merge, desc)
        if for_merge is not None:
            (merge_lines if for_merge else other_lines).append(row)
        if local is None:
            continue
        old = repo.refs.read(local)
        if old == oid:
            continue
        entries.append((name, oid, local, force, old))
    result.fetch_head = merge_lines + other_lines
    summaries = [("-", "[deleted]", "(none)", _short(ref), "") for ref in result.pruned]
    for name, oid, local, force, old in entries:
        is_tag = local.startswith("refs/tags/")
        what = _short(name) if not is_tag else _short(name)
        dest = _short(local)
        if old is None:
            label = "[new tag]" if is_tag else "[new branch]" if name.startswith("refs/heads/") else "[new ref]"
            flag = "*"
            msg = "storing tag" if is_tag else "storing head" if name.startswith("refs/heads/") else "storing ref"
            summary = label
        elif is_tag:
            if not force:
                result.rejected.append(local)
                summaries.append(("!", "[rejected]", what, dest, "(would clobber existing tag)"))
                continue
            flag, msg, summary = "t", "updating tag", "[tag update]"
        elif _is_ancestor(repo, old, oid):
            flag, msg = " ", "fast-forward"
            summary = "%s..%s" % (repo.odb.abbrev(old), repo.odb.abbrev(oid))
        elif force:
            flag, msg = "+", "forced-update"
            summary = "%s...%s" % (repo.odb.abbrev(old), repo.odb.abbrev(oid))
        else:
            result.rejected.append(local)
            summaries.append(("!", "[rejected]", what, dest, "(non-fast-forward)"))
            continue
        entry = None if cloning else repo.log_entry("%s: %s" % (action, msg))
        repo.refs.update(local, oid, old=old, log=entry)
        result.updated.append((local, old, oid))
        summaries.append((flag, summary, what, dest, "(forced update)" if flag == "+" else ""))
    if summaries:
        lines.append("From " + shown)
        width = 2 * len(repo.odb.abbrev(summaries[0][2] if False else "0" * 40)) + 3
        refcol = max([10] + [len(s[2]) for s in summaries])
        for flag, summary, src, dst, note in summaries:
            lines.append(" %s %s %s -> %s%s" % (flag, summary.ljust(width), src.ljust(refcol), dst, "  " + note if note else ""))
    if not cloning:
        result.lines.extend(lines)


def _write_fetch_head(repo, result, url):
    if not result.fetch_head and not os.path.exists(os.path.join(repo.gitdir, "FETCH_HEAD")):
        return
    text = "".join("%s\t%s\t%s\n" % (oid, "" if for_merge else "not-for-merge", desc) for oid, for_merge, desc in result.fetch_head)
    with open(os.path.join(repo.gitdir, "FETCH_HEAD"), "w", encoding="utf-8", errors="surrogateescape") as f:
        f.write(text)


def _prune(repo, specs, remote_refs, result):
    """Delete the remote-tracking refs whose branch is gone from the remote (symbolic ones such as ``origin/HEAD`` are left alone)."""
    for sp in specs:
        if not sp.is_pattern or sp.dst is None:
            continue
        for local, _oid in repo.refs.list(sp.dst[:sp.dst.index("*")]):
            raw = repo.refs.read_raw(local)
            if raw and raw[0] == "sym":
                continue
            mid = match_pattern(sp.dst, local)
            if mid is None:
                continue
            if sp.src.replace("*", mid) not in remote_refs:
                repo.refs.delete(local)
                result.pruned.append(local)


# --- push -------------------------------------------------------------------------------------------------------------------------------------------------------
@dataclass
class PushResult:
    lines: list = field(default_factory=list)
    ok: bool = True
    up_to_date: bool = False
    url: str = ""
    hints: list = field(default_factory=list)
    errors: list = field(default_factory=list)


def _expand_push_spec(repo, spec, current):
    """``(local source or None for a delete, destination ref, force)`` for one push refspec."""
    sp = parse_refspec(spec)
    src, dst = sp.src, sp.dst
    if dst is None:
        dst = src
    if src == "":
        if not dst.startswith("refs/"):
            dst = "refs/heads/" + dst
        return None, dst, True
    if src == "HEAD":
        if current is None:
            raise GitError("You are not currently on a branch; name the destination: HEAD:<branch>")
        src = "refs/heads/" + current
        if sp.dst is None:
            dst = src
    elif not src.startswith("refs/"):
        for cand in ("refs/heads/" + src, "refs/tags/" + src, "refs/remotes/" + src):
            if repo.refs.read(cand):
                src_ref = cand
                break
        else:
            src_ref = None
        if src_ref is None:
            if is_oid(src) or len(src) >= 4:
                try:
                    repo.rev_parse(src)
                    src_ref = src
                except GitError:
                    raise GitError("src refspec %s does not match any" % src)
            else:
                raise GitError("src refspec %s does not match any" % src)
        if sp.dst is None:
            dst = src_ref if src_ref.startswith("refs/") else src
        src = src_ref
    if not dst.startswith("refs/"):
        base = "refs/tags/" if src.startswith("refs/tags/") else "refs/heads/"
        dst = base + dst
    return src, dst, sp.force


def push(repo, remote=None, refspecs=(), force=False, delete=False, set_upstream_flag=False, all_branches=False, tags=False, dry_run=False):
    cur = repo.refs.current_branch()
    explicit_remote = remote is not None
    if remote is None:
        remote = (repo.config.get("branch.%s.remote" % cur) if cur else None) or "origin"
    named = remote in remote_names(repo)
    url = resolve_url(repo, remote_url(repo, remote)) if named else remote
    if named and not remote_url(repo, remote):
        raise GitError("remote '%s' has no url" % remote)
    if not named and "/" not in remote and "://" not in remote and not os.path.exists(remote):
        raise GitError("'%s' does not appear to be a git repository" % remote)
    specs = list(refspecs)
    if all_branches:
        specs += [name for name, _o in repo.refs.list("refs/heads/")]
    if tags:
        specs += [name for name, _o in repo.refs.list("refs/tags/")]
    if delete:
        specs = [":" + s if ":" not in s else s for s in specs]
    if not specs:
        if cur is None:
            raise GitError("You are not currently on a branch.")
        up = upstream_of(repo, cur)
        if up and up[0] == remote:
            if up[1] != "refs/heads/" + cur and repo.config.get("push.default", "simple") == "simple":
                raise GitError("The upstream branch of your current branch does not match the name of your current branch.")
            specs = ["refs/heads/%s:%s" % (cur, up[1])]
        elif up is None and not explicit_remote:
            raise GitError("The current branch %s has no upstream branch.\nTo push the current branch and set the remote as upstream, use\n\n    kgit push --set-upstream %s %s" % (cur, remote, cur))
        else:
            specs = ["refs/heads/%s:refs/heads/%s" % (cur, cur)]
    result = PushResult()
    t = open_transport(url, repo, repo.env, cwd=repo.worktree or repo.gitdir)
    try:
        remote_refs, caps = t.advertise_push()
        commands, rows, local_updates = [], [], []
        seen = set()
        wants = []
        for spec in specs:
            src, dst, spec_force = _expand_push_spec(repo, spec, cur)
            if dst in seen:
                continue
            seen.add(dst)
            old = remote_refs.get(dst)
            short_src = "(delete)" if src is None else _short(src)
            short_dst = _short(dst)
            if src is None:
                if old is None:
                    result.errors.append("unable to delete '%s': remote ref does not exist" % short_dst)
                    result.ok = False
                    continue
                commands.append((old, ZERO, dst))
                rows.append(("-", "[deleted]", None, short_dst, ""))
                continue
            new = repo.refs.read(src) if src.startswith("refs/") else repo.rev_parse(src)
            if new is None:
                raise GitError("src refspec %s does not match any" % src)
            kind = "tag" if dst.startswith("refs/tags/") else "branch" if dst.startswith("refs/heads/") else "ref"
            if old == new:
                rows.append(("=", "[up to date]", short_src, short_dst, ""))
                local_updates.append((dst, new))
                continue
            if old is None:
                commands.append((ZERO, new, dst))
                rows.append(("*", "[new %s]" % kind, short_src, short_dst, ""))
                wants.append(new)
                continue
            forced = force or spec_force
            if not repo.odb.exists(old):
                if not forced:
                    rows.append(("!", "[rejected]", short_src, short_dst, "(fetch first)"))
                    result.ok = False
                    continue
                ff = False
            else:
                ff = _is_ancestor(repo, old, new) if repo.odb.kind_of(old) == "commit" and dst.startswith("refs/heads/") else False
                if dst.startswith("refs/tags/") and not forced:
                    rows.append(("!", "[rejected]", short_src, short_dst, "(already exists)"))
                    result.ok = False
                    continue
            if not ff and not forced:
                rows.append(("!", "[rejected]", short_src, short_dst, "(non-fast-forward)"))
                result.ok = False
                continue
            commands.append((old, new, dst))
            wants.append(new)
            rows.append(("+" if not ff else " ", ("%s...%s" if not ff else "%s..%s") % (repo.odb.abbrev(old) if repo.odb.exists(old) else old[:7], repo.odb.abbrev(new)), short_src, short_dst,
                         "(forced update)" if not ff else ""))
        if commands and not dry_run:
            have = [o for o in remote_refs.values() if repo.odb.exists(o)]
            to_send = [c for c in commands if c[1] != ZERO]
            # an update (not just deletions) always comes with a pack, even an empty one
            pack = packio.iter_pack(collect_objects(repo, [c[1] for c in to_send], have), repo.odb.read, repo.odb.raw_delta) if to_send else None
            unpack, results = t.push(commands, pack)
            if unpack not in (None, "ok"):
                raise GitError("remote unpack failed: %s" % unpack)
            for old, new, ref in commands:
                reason = results.get(ref)
                if ref in results and reason is not None:
                    result.ok = False
                    for i, row in enumerate(rows):
                        if _short(ref) == row[3]:
                            rows[i] = ("!", "[remote rejected]", row[2], row[3], "(%s)" % reason)
                else:
                    local_updates.append((ref, new))
        elif commands:
            result.ok = result.ok
        shown = [r for r in rows if r[0] != "="]                           # up-to-date refs are only listed in verbose mode
        if shown:
            width = 2 * len(repo.odb.abbrev("0" * 40)) + 3
            result.lines.append("To " + anonymize(url))
            for flag, summary, src, dst, note in shown:
                if src is None:                                               # a deletion names only the ref
                    result.lines.append(" %s %s %s" % (flag, summary.ljust(width), dst))
                else:
                    result.lines.append(" %s %s %s -> %s%s" % (flag, summary.ljust(width), src, dst, " " + note if note else ""))
        result.up_to_date = not shown and result.ok
        result.url = anonymize(url)
        notes = {r[4] for r in shown if r[0] == "!"}
        if "(fetch first)" in notes:
            result.hints = ["Updates were rejected because the remote contains work that you do", "not have locally. This is usually caused by another repository pushing",
                            "to the same ref. You may want to first integrate the remote changes", "(e.g., 'kgit pull ...') before pushing again.",
                            "See the 'Note about fast-forwards' in 'git push --help' for details."]
        elif "(non-fast-forward)" in notes:
            result.hints = ["Updates were rejected because the tip of your current branch is behind", "its remote counterpart. Integrate the remote changes (e.g.",
                            "'kgit pull ...') before pushing again.", "See the 'Note about fast-forwards' in 'git push --help' for details."]
        elif "(already exists)" in notes:
            result.hints = ["Updates were rejected because the tag already exists in the remote."]
        if named and not dry_run:
            _update_tracking_after_push(repo, remote, commands, local_updates)
    finally:
        t.close()
    if set_upstream_flag and result.ok and cur:
        for spec in specs:
            src, dst, _f = _expand_push_spec(repo, spec, cur)
            if src and src.startswith("refs/heads/") and dst.startswith("refs/heads/"):
                set_upstream(repo, src[len("refs/heads/"):], remote, dst)
                result.lines.append("branch '%s' set up to track '%s/%s'." % (src[len("refs/heads/"):], remote, dst[len("refs/heads/"):]))
    return result


def _update_tracking_after_push(repo, remote, commands, local_updates):
    specs = remote_fetch_specs(repo, remote)
    for ref, new in local_updates:
        for sp in specs:
            if sp.dst is None:
                continue
            mid = match_pattern(sp.src, ref)
            if mid is None:
                continue
            local = sp.dst.replace("*", mid) if "*" in sp.dst else sp.dst
            old = repo.refs.read(local)
            if old != new:
                repo.refs.update(local, new, old=old, log=repo.log_entry("update by push"))
    for old, new, ref in commands:
        if new != ZERO:
            continue
        for sp in specs:
            if sp.dst is None:
                continue
            mid = match_pattern(sp.src, ref)
            if mid is not None:
                local = sp.dst.replace("*", mid) if "*" in sp.dst else sp.dst
                if repo.refs.read(local):
                    repo.refs.delete(local)


# --- clone ---------------------------------------------------------------------------------------------------------------------------------------------------
def default_dir_name(url):
    name = tidy_url(url).rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    return name or "repo"


def clone(url, directory=None, bare=False, branch=None, origin="origin", checkout=True, env=None, now=None, cwd=None):
    from .repo import Repo
    cwd = cwd or os.getcwd()
    if "://" not in url and not os.path.isabs(url):
        url_abs = os.path.normpath(os.path.join(cwd, url))
        if os.path.exists(url_abs):
            url = url_abs
    directory = directory or default_dir_name(url) + (".git" if bare else "")
    target = os.path.abspath(os.path.join(cwd, directory))
    if os.path.exists(target) and os.listdir(target):
        raise GitError("destination path '%s' already exists and is not an empty directory." % directory)
    created = not os.path.exists(target)
    notes = []
    try:
        os.makedirs(target, exist_ok=True)
        probe = open_transport(url, None, env or {}, cwd=cwd)
        try:
            listing = probe.ls_refs(["HEAD", "refs/heads/", "refs/tags/"], unborn=True)
        finally:
            probe.close()
        remote_refs = {name: oid for name, oid, _t, _p in listing if oid}
        head_target = next((tg for name, _o, tg, _p in listing if name == "HEAD"), None)
        unborn_target = head_target if not remote_refs.get("HEAD") else None
        if head_target is None and remote_refs.get("HEAD"):
            head_target = next((n for n, o in remote_refs.items() if n.startswith("refs/heads/") and o == remote_refs["HEAD"]), None)
        remote_head = head_target                     # origin/HEAD follows the server's HEAD even when another branch is asked for
        if branch:
            if "refs/heads/" + branch not in remote_refs:
                raise GitError("Remote branch %s not found in upstream %s" % (branch, origin))
            head_target = "refs/heads/" + branch
        repo = Repo.init(target, branch="main", env=env, now=now, bare=bare)
        cfg = repo.config.local
        cfg.set("remote.%s.url" % origin, url)
        if bare:
            specs = [Refspec("refs/heads/*", "refs/heads/*", True)]
        else:
            cfg.add("remote.%s.fetch" % origin, "+refs/heads/*:refs/remotes/%s/*" % origin)
            specs = remote_fetch_specs(Repo(repo.gitdir, repo.worktree, env=env), origin) if False else [Refspec("refs/heads/*", "refs/remotes/%s/*" % origin, True)]
        cfg.save()
        repo = Repo(repo.gitdir, repo.worktree, env=env, now=now)
        fetch(repo, origin, tags=True, cloning=True, specs=specs, url=url)
        if not remote_refs:
            notes.append("warning: You appear to have cloned an empty repository.")
            if unborn_target and unborn_target.startswith("refs/heads/"):
                repo.refs.set_symbolic("HEAD", unborn_target)             # the branch the other side's HEAD is waiting to become
            return repo, notes
        entry = repo.log_entry("clone: from " + url)
        if head_target and head_target.startswith("refs/heads/"):
            name = head_target[len("refs/heads/"):]
            tip = remote_refs[head_target]
            if not bare:
                if remote_head and remote_head.startswith("refs/heads/"):
                    rname = remote_head[len("refs/heads/"):]
                    _set_symref(repo, "refs/remotes/%s/HEAD" % origin, "refs/remotes/%s/%s" % (origin, rname), remote_refs[remote_head], entry)
                repo.refs.update(head_target, tip, old=None, log=entry)
                set_upstream(repo, name, origin, head_target)
            repo.refs.set_symbolic("HEAD", head_target)
            if bare:
                pass
            elif entry:
                repo.refs.append_log("HEAD", ZERO, tip, *entry)
            if not bare and checkout:
                co.apply_switch(repo, {}, co.snapshot_of_commit(repo, tip))
        elif remote_refs.get("HEAD") and not branch:                    # the other side's HEAD is detached: so is ours
            tip = remote_refs["HEAD"]
            repo.refs._write_locked("HEAD", tip)
            if entry:
                repo.refs.append_log("HEAD", ZERO, tip, *entry)
            if not bare and checkout:
                co.apply_switch(repo, {}, co.snapshot_of_commit(repo, tip))
        return repo, notes
    except BaseException:
        if created and os.path.isdir(target):
            import shutil
            shutil.rmtree(target, ignore_errors=True)
        raise


def _set_symref(repo, name, target, tip, entry):
    path = os.path.join(repo.gitdir, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("ref: %s\n" % target)
    if entry:
        repo.refs.append_log(name, ZERO, tip, *entry)


__all__ = ["fetch", "push", "clone", "Refspec", "parse_refspec", "pr", "ix"]
