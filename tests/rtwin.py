"""Remote operations in lock step: two copies of one upstream, a clone of each (real git's and ours), the same changes fed to both, and a comparison after every step."""
import io
import os
import re
import shutil
import subprocess
import threading

import gitoracle as go
from kendi_git import cli
from twin import Twin


def commit_file(repo, name, content, message, when):
    go.write(repo, name, content)
    go.git(repo, "add", "-A")
    go.git(repo, "commit", "-qm", message, extra={"GIT_AUTHOR_DATE": "%d +0000" % when, "GIT_COMMITTER_DATE": "%d +0000" % when})


class RTwin:
    def __init__(self, tmp_path, transport="local"):
        self.root = str(tmp_path)
        self.transport = transport
        self.step = 0
        self.seed = os.path.join(self.root, "seed")
        os.makedirs(self.seed)
        go.git(self.seed, "init", "-q", "-b", "main")
        commit_file(self.seed, "a.txt", "one\ntwo\nthree\n", "first", 1700000000)
        commit_file(self.seed, "d/b.txt", "bee\n", "second", 1700000010)
        go.git(self.seed, "tag", "-a", "v1", "-m", "version 1", extra={"GIT_COMMITTER_DATE": "1700000011 +0000"})
        go.git(self.seed, "branch", "dev")
        self.up = {}
        for who in ("g", "k"):
            self.up[who] = os.path.join(self.root, "up_%s.git" % who)
            go.git(self.root, "clone", "-q", "--bare", self.seed, self.up[who])
            go.git(self.up[who], "config", "http.receivepack", "true")
        self.feeder = os.path.join(self.root, "feeder")
        go.git(self.root, "clone", "-q", self.seed, self.feeder)
        go.git(self.feeder, "branch", "dev", "origin/dev")
        for who in ("g", "k"):
            go.git(self.feeder, "remote", "add", "feed_" + who, self.up[who])
        self.servers = []
        self.url = {who: self._serve(who) for who in ("g", "k")}
        self.dir = {"g": os.path.join(self.root, "client_g"), "k": os.path.join(self.root, "client_k")}

    # --- servers -------------------------------------------------------------------------------------------------------------------------------
    def _serve(self, who):
        if self.transport == "local":
            return self.up[who]
        if self.transport == "file":
            return "file://" + self.up[who]
        if self.transport == "git":
            from servers import GitDaemon
            if not hasattr(self, "_daemon"):
                self._daemon = GitDaemon(self.root)
                self.servers.append(self._daemon)
            return self._daemon.url("up_%s.git" % who)
        if self.transport == "http":
            from servers import HttpBackend
            if not hasattr(self, "_http"):
                self._http = HttpBackend(self.root)
                self.servers.append(self._http)
            return self._http.url("up_%s.git" % who)
        raise ValueError(self.transport)

    def close(self):
        for s in self.servers:
            s.close()

    # --- running tools ---------------------------------------------------------------------------------------------------------------------------
    def env(self):
        stamp = "%d +0000" % (1700001000 + 100 * self.step)
        return {**go.env(), "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}

    def git(self, who, *args, check=False, cwd=None):
        p = subprocess.run([go.GIT, *args], cwd=cwd or self.dir[who], capture_output=True, env=self.env())
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")

    def kgit(self, *args, cwd=None):
        out, err = io.BytesIO(), io.BytesIO()
        code = cli.main(list(args), stdout=out, stderr=err, cwd=cwd or self.dir["k"], env=self.env())
        return code, out.getvalue().decode("utf-8", "replace"), err.getvalue().decode("utf-8", "replace")

    def both(self, *args):
        """Run the same command with real git (in its client) and with kgit (in ours); returns both results."""
        self.step += 1
        g = self.git("g", *args)
        k = self.kgit(*args)
        return g, k

    def clone(self, *options):
        self.step += 1
        g = self.git("g", "clone", "-q", *options, self.url["g"], self.dir["g"], cwd=self.root)
        k = self.kgit("clone", *options, self.url["k"], self.dir["k"], cwd=self.root)
        return g, k

    def feed(self, fn):
        """Make changes in the feeder repository and publish them to both upstreams."""
        self.step += 1
        fn(self.feeder)
        for who in ("g", "k"):
            go.git(self.feeder, "push", "-q", "--mirror", "feed_" + who)

    # --- comparison ------------------------------------------------------------------------------------------------------------------------------
    def norm(self, text, who):
        up = self.up[who]
        text = text.replace(up, "UP").replace(up[:-4], "UP").replace("up_%s.git" % who, "UP").replace("up_%s" % who, "UP").replace(self.dir[who], "DIR")
        return re.sub(r"127\.0\.0\.1:\d+", "HOST", text)

    def snapshot(self, who, d=None, bare=False):
        d = d or self.dir[who]
        out = {}
        out["refs"] = go.git(d, "for-each-ref", "--format=%(refname) %(objectname) %(objecttype) %(symref)").decode().splitlines()
        out["head"] = go.git(d, "symbolic-ref", "-q", "HEAD", check=False).stdout.decode().strip()
        gitdir = d if bare else os.path.join(d, ".git")
        logs = {}
        base = os.path.join(gitdir, "logs")
        for cur, _dirs, files in os.walk(base):
            for f in files:
                p = os.path.join(cur, f)
                logs[os.path.relpath(p, base)] = self.norm(open(p, encoding="utf-8", errors="replace").read(), who)
        out["logs"] = logs
        cfg = go.git(d, "config", "--local", "--list", check=False).stdout.decode()
        out["config"] = sorted(self.norm(l, who) for l in cfg.splitlines() if l.split("=")[0].startswith(("remote.", "branch.")))
        fh = os.path.join(gitdir, "FETCH_HEAD")
        out["fetch_head"] = self.norm(open(fh).read(), who) if os.path.exists(fh) else None
        if not bare:
            out["status"] = go.git(d, "status", "--porcelain", "-b", "-uall").decode()
            out["tree"] = {k: v for k, v in Twin.tree_snapshot(d).items() if v[0] != "dir"}
            out["index"] = Twin.stage_listing(d)
        return out

    @staticmethod
    def _scrub(obj, needle):
        if isinstance(obj, str):
            return obj.replace(needle, "M" * 40).replace(needle[:7], "M" * 7)
        if isinstance(obj, list):
            return [RTwin._scrub(x, needle) for x in obj]
        if isinstance(obj, dict):
            return {k: RTwin._scrub(v, needle) for k, v in obj.items()}
        return obj

    def merge_commit(self, who):
        """The id of HEAD in a client (the id of a merge commit differs because its message names the upstream, which has a different path in each twin)."""
        return go.gits(self.dir[who], "rev-parse", "HEAD").strip()

    def assert_same(self, label="", logs=True, upstream=False, clients=True, mask_head=False):
        if clients:
            g, k = self.snapshot("g"), self.snapshot("k")
            if mask_head:
                g, k = self._scrub(g, self.merge_commit("g")), self._scrub(k, self.merge_commit("k"))
            for key in g:
                if key == "logs" and not logs:
                    continue
                assert g[key] == k[key], "%s differs %s:\n git: %r\n ours: %r" % (key, label, g[key], k[key])
            fsck = go.git(self.dir["k"], "fsck", "--strict", check=False)
            assert fsck.returncode == 0 and not fsck.stdout.strip() and not fsck.stderr.decode().strip().replace("Checking object directories", ""), "fsck of our clone %s: %r %r" % (label, fsck.stdout, fsck.stderr)
        if upstream:
            g, k = self.snapshot("g", self.up["g"], bare=True), self.snapshot("k", self.up["k"], bare=True)
            assert g["refs"] == k["refs"], "upstream refs differ %s:\n git: %r\n ours: %r" % (label, g["refs"], k["refs"])
            fsck = go.git(self.up["k"], "fsck", "--strict", check=False)
            assert fsck.returncode == 0, "fsck of the upstream after our push %s: %r" % (label, fsck.stderr)
