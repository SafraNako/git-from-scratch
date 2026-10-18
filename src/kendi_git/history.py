"""The revision walk behind ``log``: a port of the parts of git's revision.c that decide *which commits* are shown and in which order.

That includes ``limit_list`` (ranges such as ``a..b``: the "uninteresting" flag spreads while the walk goes, so what ``log a..b -- path`` hides depends on how far it
had spread when a merge was looked at), ``process_parents``, history simplification for ``log -- <path>`` (``try_to_simplify_commit``: a commit is *TREESAME* when it
changed none of the given paths, and a merge is only followed down the parent it is TREESAME to), ``get_commit_action`` and the parent rewriting that ``--graph`` needs.
"""
from __future__ import annotations

import heapq

UNINTERESTING, BOTTOM, SEEN, ADDED, TREESAME = 1, 2, 4, 8, 16
SLOP = 5


class Walk:
    def __init__(self, repo, pending, pathspec=None, full_history=False, first_parent=False, topo=None, rewrite=False):
        """``pending``: ``(commit id, uninteresting, bottom)`` in command-line order (``a..b`` gives ``a`` excluded, as a bottom, then ``b``)."""
        self.repo = repo
        self.ps = pathspec
        self.full_history = full_history
        self.first_parent = first_parent
        self.topo = topo                              # None, "topo" or "date"
        self.rewrite = rewrite                        # --graph: parents are rewritten and merges kept for the topology
        self.flags = {}
        self.plist = {}                               # commit -> its current parent list (None until the commit is "parsed")
        self.treesame_parts = {}                      # merge -> per-parent TREESAME flags (--full-history with a limited walk)
        self.heap = []
        self.seq = 0
        self.pending = list(pending)
        self._tree_cache = {}
        self.limited = any(u for _o, u, _b in self.pending) or topo is not None
        self.treesame_tracking = self.limited and pathspec is not None
        self.start = []

    # --- commits ---------------------------------------------------------------------------------------------------------------------------------
    def flag(self, oid):
        return self.flags.get(oid, 0)

    def date(self, oid):
        return self.repo.read_commit(oid).committer.time

    def parse(self, oid):
        if oid not in self.plist:
            self.plist[oid] = list(self.repo.read_commit(oid).parents)
        return self.plist[oid]

    def parents_of(self, oid):
        """``commit->parents``: empty while the commit has not been parsed."""
        return self.plist.get(oid) or []

    def relevant(self, oid):
        return (self.flag(oid) & (UNINTERESTING | BOTTOM)) != UNINTERESTING

    def insert_by_date(self, oid):
        self.seq += 1
        heapq.heappush(self.heap, (-self.date(oid), self.seq, oid))

    # --- uninteresting commits -----------------------------------------------------------------------------------------------------------------
    def mark_parents_uninteresting(self, oid):
        stack = list(reversed(self.parents_of(oid)))
        while stack:
            commit = stack.pop()
            while commit is not None:
                if self.flag(commit) & UNINTERESTING:
                    break
                self.flags[commit] = self.flag(commit) | UNINTERESTING
                parents = self.parents_of(commit)
                if not parents:
                    break
                stack.extend(reversed(parents[1:]))
                commit = parents[0]

    # --- tree comparison ---------------------------------------------------------------------------------------------------------------------
    def _entries(self, tree):
        if tree is None:
            return {}
        got = self._tree_cache.get(tree)
        if got is None:
            got = {e.name: e for e in self.repo.read_tree_entries(tree)}
            if len(self._tree_cache) > 4096:
                self._tree_cache.clear()
            self._tree_cache[tree] = got
        return got

    def differs(self, a, b, base=""):
        """Do the trees ``a`` and ``b`` (ids, or None for none) differ at any path the pathspec selects?"""
        if a == b:
            return False
        ea, eb = self._entries(a), self._entries(b)
        for name in set(ea) | set(eb):
            x, y = ea.get(name), eb.get(name)
            if x is not None and y is not None and x.mode == y.mode and x.oid == y.oid:
                continue
            path = base + name
            xdir = x is not None and x.mode == 0o40000
            ydir = y is not None and y.mode == 0o40000
            if (x is not None and not xdir) or (y is not None and not ydir):          # a file, link or submodule on either side
                if self.ps.match(path, mark=False):
                    return True
            if (xdir or ydir) and self.ps.could_contain(path):
                if self.differs(x.oid if xdir else None, y.oid if ydir else None, path + "/"):
                    return True
        return False

    # --- history simplification ---------------------------------------------------------------------------------------------------------------
    def simplify(self, oid):
        """``try_to_simplify_commit``: sets TREESAME and may cut a merge's parent list down to the one parent it is TREESAME to."""
        if self.ps is None:
            return
        tree = self.repo.read_commit(oid).tree
        parents = self.plist[oid]
        if not parents:
            if not self.differs(None, tree):
                self.flags[oid] = self.flag(oid) | TREESAME
            return
        relevant_parents = 0
        relevant_change = irrelevant_change = False
        ts = None
        for n, p in enumerate(parents):
            if self.relevant(p):
                relevant_parents += 1
            if n == 1:
                if self.first_parent:
                    break
                if not self.simplify_history and self.treesame_tracking and not self.flag(oid) & UNINTERESTING:
                    ts = self.treesame_parts[oid] = [False] * len(parents)
                    if not (irrelevant_change or relevant_change):
                        ts[0] = True
            self.parse(p)
            if not self.differs(self.repo.read_commit(p).tree, tree):
                if not self.simplify_history or not self.relevant(p):
                    if ts is not None:
                        ts[n] = True
                    continue
                self.plist[oid] = [p]
                self.flags[oid] = self.flag(oid) | TREESAME
                return
            if self.relevant(p):
                relevant_change = True
            else:
                irrelevant_change = True
        if (not relevant_change) if relevant_parents else (not irrelevant_change):
            self.flags[oid] = self.flag(oid) | TREESAME

    @property
    def simplify_history(self):
        return not self.full_history

    def update_treesame(self, oid):
        ts = self.treesame_parts.get(oid)
        if ts is None:
            return
        relevant_parents = 0
        relevant_changes = irrelevant_changes = False
        for n, p in enumerate(self.plist[oid]):
            if self.relevant(p):
                relevant_parents += 1
                if not ts[n]:
                    relevant_changes = True
            elif not ts[n]:
                irrelevant_changes = True
        if relevant_changes if relevant_parents else irrelevant_changes:
            self.flags[oid] = self.flag(oid) & ~TREESAME
        else:
            self.flags[oid] = self.flag(oid) | TREESAME

    # --- walking ----------------------------------------------------------------------------------------------------------------------------
    def process_parents(self, oid):
        if self.flag(oid) & ADDED:
            return
        self.flags[oid] = self.flag(oid) | ADDED
        parents = self.parse(oid)
        if self.flag(oid) & UNINTERESTING:
            for p in list(parents):
                self.flags[p] = self.flag(p) | UNINTERESTING
                self.parse(p)
                if self.plist[p]:
                    self.mark_parents_uninteresting(p)
                if self.flag(p) & SEEN:
                    continue
                self.flags[p] = self.flag(p) | SEEN
                self.insert_by_date(p)
            return
        self.simplify(oid)
        for p in list(self.plist[oid]):
            self.parse(p)
            if not self.flag(p) & SEEN:
                self.flags[p] = self.flag(p) | SEEN
                self.insert_by_date(p)
            if self.first_parent:
                break

    def prepare(self):
        for oid, uninteresting, bottom in self.pending:                      # the flags are put on the objects while the arguments are read
            self.flags[oid] = self.flag(oid) | (UNINTERESTING if uninteresting else 0) | (BOTTOM if bottom else 0)
        for oid, _u, _b in self.pending:                                      # handle_commit
            self.parse(oid)
            if self.flag(oid) & UNINTERESTING:
                self.mark_parents_uninteresting(oid)
            if not self.flag(oid) & SEEN:
                self.flags[oid] = self.flag(oid) | SEEN
                self.start.append(oid)
        for oid in sorted(self.start, key=lambda c: -self.date(c)):             # a stable sort by date
            self.insert_by_date(oid)

    def everybody_uninteresting(self):
        return all(self.flag(e[2]) & UNINTERESTING for e in self.heap)

    def limit_list(self):
        slop = SLOP
        date = float("inf")
        out = []
        while self.heap:
            oid = heapq.heappop(self.heap)[2]
            self.process_parents(oid)
            if self.flag(oid) & UNINTERESTING:
                self.mark_parents_uninteresting(oid)
                if not self.heap:
                    slop = 0
                elif date <= -self.heap[0][0]:
                    slop = SLOP
                elif not self.everybody_uninteresting():
                    slop = SLOP
                else:
                    slop -= 1
                if slop:
                    continue
                break
            date = self.date(oid)
            out.append(oid)
        if self.full_history and self.ps is not None:
            for oid in out:
                if not self.flag(oid) & (UNINTERESTING | TREESAME):
                    self.update_treesame(oid)
        return out

    # --- what is shown -----------------------------------------------------------------------------------------------------------------------
    def action_shows(self, oid):
        """``get_commit_action`` (without the options this program has no use for)."""
        f = self.flag(oid)
        if f & UNINTERESTING:
            return False
        if self.ps is not None and f & TREESAME:
            if not self.rewrite:
                return False
            return sum(1 for p in self.parents_of(oid) if self.relevant(p)) >= 2
        return True

    def _one_relevant_parent(self, parents):
        if not parents:
            return None
        if self.first_parent or len(parents) == 1:
            return parents[0]
        found = None
        for p in parents:
            if self.relevant(p):
                if found is not None:
                    return None
                found = p
        return found

    def rewritten_parents(self, oid):
        """``rewrite_parents``: parents that are not shown are replaced by their nearest shown ancestor; duplicates and ones that lead nowhere are dropped."""
        out = []
        for p in self.parents_of(oid):
            while True:
                if self.flag(p) & UNINTERESTING or not self.flag(p) & TREESAME:
                    break
                parents = self.parents_of(p)
                if not parents:
                    p = None
                    break
                nxt = self._one_relevant_parent(parents)
                if nxt is None:
                    break
                p = nxt
            if p is not None and p not in out:
                out.append(p)
        return out

    def run(self):
        """The commits to show, in order (a generator when the walk can be lazy, otherwise a list)."""
        self.prepare()
        if not self.limited:
            return self._lazy()
        commits = self.limit_list()
        if self.topo:
            commits = self.repo.order_commits(commits, self.topo, {c: self.parents_of(c) for c in commits})
        return [c for c in commits if self.action_shows(c)]

    def _lazy(self):
        while self.heap:
            oid = heapq.heappop(self.heap)[2]
            self.process_parents(oid)
            if self.action_shows(oid):
                yield oid

    def graph_parents(self, oid):
        """The parents ``--graph`` draws lines to: the (rewritten) ones that are shown too, or just the first one with ``--first-parent``."""
        parents = self.rewritten_parents(oid) if self.ps is not None else self.parents_of(oid)
        if self.first_parent:
            return parents[:1] if parents and self.action_shows(parents[0]) else []
        return [p for p in parents if self.action_shows(p)]

    def shown_parents(self, oid):
        """The parents of a shown commit as ``log`` sees them (after simplification and, for ``--graph``, rewriting): what it is compared with and what ``Merge:`` lists."""
        return self.rewritten_parents(oid) if self.rewrite and self.ps is not None else self.parents_of(oid)
