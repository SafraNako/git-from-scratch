"""Talking to another repository: a directory on this machine, a ``git://`` server or a smart ``http(s)://`` server.

Every transport offers the same four things: ``ls_refs`` (what refs does the other side have), ``fetch`` (give me these objects, I already have those), ``advertise_push``
(the refs a push is compared against) and ``push`` (send these objects and update these refs).
"""
from __future__ import annotations

import base64
import os
import socket
import urllib.error
import urllib.parse
import urllib.request

from . import packio
from . import protocol as pr
from .errors import GitError
from .objects import Tag, is_oid


# --- which objects go into a pack -------------------------------------------------------------------------------------------------------------------
def collect_objects(repo, wants, haves):
    """``[(kind, oid)]`` of every object needed to complete the history of ``wants`` for a receiver that has ``haves``. Tags are followed to what they point at."""
    odb = repo.odb
    have_commits = set()
    for h in haves:
        try:
            if odb.kind_of(h) == "commit":
                have_commits.add(h)
        except GitError:
            continue
    boring = repo.reachable(have_commits) if have_commits else set()
    out, seen, commits = [], set(), []
    stack = list(wants)
    loose = []                                          # refs that point straight at a tree or blob
    while stack:
        oid = stack.pop()
        if oid in seen:
            continue
        kind, data = odb.read(oid)
        if kind == "tag":
            seen.add(oid)
            out.append(("tag", oid))
            stack.append(Tag.parse(data).object)
        elif kind == "commit":
            if oid in boring:
                continue
            seen.add(oid)
            commits.append(oid)
            out.append(("commit", oid))
            stack.extend(repo.read_commit(oid).parents)                 # (a shallow repository's boundary commits have no parents)
        else:
            seen.add(oid)
            loose.append((kind, oid))
    known = set()                                       # trees and blobs the receiver surely has: those of the boundary commits
    boundary = {p for c in commits for p in repo.read_commit(c).parents if p in boring}
    for b in boundary:
        _walk_tree(repo, repo.read_commit(b).tree, known, None)
    emitted = set()
    for c in commits:
        _walk_tree(repo, repo.read_commit(c).tree, known, (emitted, out))
    return out + loose


def _walk_tree(repo, tree, known, sink):
    """Visit a tree and everything below it. Without ``sink`` the ids go into ``known``; with ``sink=(emitted, out)`` the ones not known are appended to ``out``."""
    stack = [tree]
    while stack:
        oid = stack.pop()
        if oid in known or (sink and oid in sink[0]):
            continue
        if sink:
            sink[0].add(oid)
            sink[1].append(("tree", oid))
        else:
            known.add(oid)
        for is_tree, child in _tree_children(repo.odb.read(oid)[1]):
            if is_tree:
                stack.append(child)
            elif child not in known and not (sink and child in sink[0]):
                if sink:
                    sink[0].add(child)
                    sink[1].append(("blob", child))
                else:
                    known.add(child)


def _tree_children(data):
    """``[(is_tree, id)]`` of the entries of a tree object (submodule entries left out): a lean parser, since the walk reads every tree of a history."""
    out = []
    i, n = 0, len(data)
    while i < n:
        sp = data.index(b" ", i)
        nul = data.index(b"\x00", sp)
        mode = data[i:sp]
        i = nul + 21
        if mode == b"40000":
            out.append((True, data[nul + 1:i].hex()))
        elif mode != b"160000":
            out.append((False, data[nul + 1:i].hex()))
    return out


# --- URLs ----------------------------------------------------------------------------------------------------------------------------------------------
def open_transport(url, repo=None, env=None, cwd=None):
    """The transport for a remote ``url``: a path or ``file://`` URL, ``git://`` or ``http(s)://``."""
    if url.startswith("file://"):
        return LocalTransport(urllib.parse.unquote(url[len("file://"):]), env)
    if url.startswith("git://"):
        parts = urllib.parse.urlsplit(url)
        return GitTransport(parts.hostname, parts.port or 9418, urllib.parse.unquote(parts.path) or "/")
    if url.startswith(("http://", "https://")):
        return HttpTransport(url)
    if url.startswith("ssh://") or ("@" in url.split("/")[0] and ":" in url.split("/")[0]):
        raise GitError("ssh remotes are not supported (use a path, git:// or http(s)://)")
    path = url if os.path.isabs(url) else os.path.join(cwd or os.getcwd(), url)
    return LocalTransport(os.path.normpath(path), env)


class Transport:
    url = ""

    def ls_refs(self, prefixes=(), unborn=False):
        raise NotImplementedError

    def fetch(self, wants, haves, depth=None):
        raise NotImplementedError

    def advertise_push(self):
        raise NotImplementedError

    def push(self, commands, objects):
        raise NotImplementedError

    def close(self):
        pass


# --- a repository on this machine ------------------------------------------------------------------------------------------------------------------
class LocalTransport(Transport):
    def __init__(self, path, env=None):
        from .repo import Repo
        self.path = path
        try:
            self.repo = Repo.discover(path, env=env)
        except GitError:
            raise GitError("'%s' does not appear to be a git repository" % path)
        if self.repo.worktree is not None and os.path.realpath(self.repo.worktree) != os.path.realpath(path):
            raise GitError("'%s' does not appear to be a git repository" % path)

    def _all_refs(self, unborn=False):
        repo = self.repo
        out = []
        head = repo.refs.head_oid()
        raw = repo.refs.read_raw("HEAD")
        target = raw[1] if raw and raw[0] == "sym" else None
        if head:
            out.append(("HEAD", head, target, None))
        elif unborn and target:
            out.append(("HEAD", None, target, None))
        for name, oid in repo.refs.list("refs/"):
            peeled = None
            try:
                if repo.odb.kind_of(oid) == "tag":
                    peeled = repo.peel(oid)
            except GitError:
                pass
            out.append((name, oid, None, peeled))
        return out

    def ls_refs(self, prefixes=(), unborn=False):
        refs = self._all_refs(unborn)
        if prefixes:
            refs = [r for r in refs if any(r[0].startswith(p) or r[0] == p.rstrip("/") for p in prefixes) or r[0] == "HEAD"]
        return refs

    def fetch(self, wants, haves, depth=None):
        return packio.iter_pack(collect_objects(self.repo, wants, haves), self.repo.odb.read, self.repo.odb.raw_delta)

    def advertise_push(self):
        return {name: oid for name, oid, _t, _p in self._all_refs() if name != "HEAD"}, {"report-status", "delete-refs"}

    def push(self, commands, pack):
        repo = self.repo
        if pack is not None:
            packio.receive_pack(pack, repo.odb, os.path.join(repo.gitdir, "objects"))
        results = {}
        current = repo.refs.current_branch() if not repo.bare else None
        deny = repo.config.get("receive.denyCurrentBranch", "refuse")
        for old, new, ref in commands:
            try:
                if not ref.startswith("refs/") or ref.endswith("/") or ".." in ref:
                    results[ref] = "funny refname"
                    continue
                have = repo.refs.read(ref)
                if (have or pr.ZERO) != old:
                    results[ref] = "failed to update ref"
                    continue
                if current and ref == "refs/heads/" + current and deny not in ("ignore", "warn", "updateInstead"):
                    results[ref] = "branch is currently checked out"
                    continue
                if new == pr.ZERO:
                    if have:
                        repo.refs.delete(ref, old=old)
                else:
                    if not repo.odb.exists(new):
                        results[ref] = "missing necessary objects"
                        continue
                    repo.refs.update(ref, new, old=have, log=repo.log_entry("push"))
                results[ref] = None
            except GitError as exc:
                results[ref] = str(exc)
        return "ok", results


# --- git:// ------------------------------------------------------------------------------------------------------------------------------------
class _Conn:
    def __init__(self, sock):
        self.sock = sock
        self.reader = pr.PktReader(self.recv)

    def recv(self, n):
        return self.sock.recv(n)

    def send(self, data):
        self.sock.sendall(data)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class GitTransport(Transport):
    def __init__(self, host, port, path):
        self.host, self.port, self.path = host, port, path
        self.url = "git://%s%s%s" % (host, "" if port == 9418 else ":%d" % port, path)
        self._v2 = None

    def _connect(self, service, v2):
        try:
            sock = socket.create_connection((self.host, self.port), timeout=300)
        except OSError as exc:
            raise GitError("unable to connect to %s:%d: %s" % (self.host, self.port, exc))
        request = "%s %s\0host=%s\0" % (service, self.path, self.host)
        if v2:
            request += "\0version=2\0"
        conn = _Conn(sock)
        conn.send(pr.pkt_line(request))
        return conn

    def _v2_conn(self):
        if self._v2 is None:
            conn = self._connect("git-upload-pack", True)
            conn.caps = pr.parse_v2_capabilities(conn.reader)
            self._v2 = conn
        return self._v2

    @staticmethod
    def _caps_of(conn):
        return conn.caps.get("ls-refs") or ""

    def ls_refs(self, prefixes=(), unborn=False):
        conn = self._v2_conn()
        conn.send(pr.ls_refs_request(prefixes, unborn=unborn and "unborn" in (self._caps_of(conn) or "")))
        return pr.parse_ls_refs(conn.reader)

    def fetch(self, wants, haves, depth=None):
        conn = self._v2_conn()
        conn.send(pr.fetch_request(wants, haves, depth))
        return list(pr.read_fetch_response(conn.reader))

    def advertise_push(self):
        self._push_conn = self._connect("git-receive-pack", False)
        refs, caps = pr.parse_v0_advertisement(self._push_conn.reader)
        return dict(refs), caps

    def push(self, commands, pack):
        conn = self._push_conn
        caps = ["report-status", "side-band-64k", pr.AGENT]
        conn.send(pr.receive_pack_request(commands, None, caps))
        for chunk in pack or ():
            conn.send(chunk)
        return pr.parse_report(conn.reader, True)

    def close(self):
        for c in (self._v2, getattr(self, "_push_conn", None)):
            if c:
                c.close()


# --- smart HTTP --------------------------------------------------------------------------------------------------------------------------------------
class HttpTransport(Transport):
    def __init__(self, url):
        parts = urllib.parse.urlsplit(url)
        self.auth = None
        netloc = parts.netloc
        if parts.username is not None:
            self.auth = "Basic " + base64.b64encode(("%s:%s" % (urllib.parse.unquote(parts.username), urllib.parse.unquote(parts.password or ""))).encode()).decode()
            netloc = parts.hostname + (":%d" % parts.port if parts.port else "")
        self.base = urllib.parse.urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", ""))
        self.url = self.base
        self._caps = None

    def _request(self, path, body=None, service=None, v2=False, query=""):
        headers = {"User-Agent": "kgit/0.1", "Accept": "*/*", "Pragma": "no-cache"}
        if self.auth:
            headers["Authorization"] = self.auth
        if v2:
            headers["Git-Protocol"] = "version=2"
        if body is not None:
            headers["Content-Type"] = "application/x-%s-request" % service
            headers["Accept"] = "application/x-%s-result" % service
        req = urllib.request.Request(self.base + path + query, data=body, headers=headers, method="POST" if body is not None else "GET")
        try:
            return urllib.request.urlopen(req, timeout=300)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise GitError("authentication failed for '%s' (HTTP %d); put the credentials in the URL (https://user:token@host/path)" % (self.base, exc.code))
            if exc.code == 404:
                raise GitError("repository '%s' not found" % self.base)
            raise GitError("the server answered HTTP %d for %s" % (exc.code, self.base + path))
        except urllib.error.URLError as exc:
            raise GitError("unable to access '%s': %s" % (self.base, exc.reason))

    def _discover(self, service, v2):
        resp = self._request("/info/refs", service=None, v2=v2, query="?service=" + service)
        ctype = resp.headers.get("Content-Type", "")
        if ctype != "application/x-%s-advertisement" % service:
            raise GitError("'%s' does not look like a smart HTTP git server (got %s)" % (self.base, ctype or "no content type"))
        reader = pr.PktReader(resp.read)
        first = reader.packet()
        if isinstance(first, bytes) and first.startswith(b"# service="):
            reader.until_flush()                       # protocol 0/1 announces the service first; version 2 starts with its greeting
        elif first is None:
            raise GitError("the server sent an empty answer")
        else:
            reader.unread(first)
        return reader

    def _v2_caps(self):
        if self._caps is None:
            reader = self._discover("git-upload-pack", True)
            self._caps = pr.parse_v2_capabilities(reader)
        return self._caps

    def ls_refs(self, prefixes=(), unborn=False):
        caps = self._v2_caps()
        resp = self._request("/git-upload-pack", pr.ls_refs_request(prefixes, unborn=unborn and "unborn" in (caps.get("ls-refs") or "")), "git-upload-pack", v2=True)
        return pr.parse_ls_refs(pr.PktReader(resp.read))

    def fetch(self, wants, haves, depth=None):
        self._v2_caps()
        resp = self._request("/git-upload-pack", pr.fetch_request(wants, haves, depth), "git-upload-pack", v2=True)
        return list(pr.read_fetch_response(pr.PktReader(resp.read)))

    def advertise_push(self):
        reader = self._discover("git-receive-pack", False)
        refs, caps = pr.parse_v0_advertisement(reader)
        return dict(refs), caps

    def push(self, commands, pack):
        caps = ["report-status", "side-band-64k", pr.AGENT]
        resp = self._request("/git-receive-pack", pr.receive_pack_request(commands, b"".join(pack) if pack is not None else None, caps), "git-receive-pack")
        return pr.parse_report(pr.PktReader(resp.read), True)


__all__ = ["open_transport", "collect_objects", "Transport", "LocalTransport", "GitTransport", "HttpTransport", "is_oid"]
