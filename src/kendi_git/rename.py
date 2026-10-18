"""Rename detection: a port of git's diffcore-rename.c and diffcore-delta.c.

Deleted files are matched with created files. Identical contents are paired first (preferring the same base name), then paths whose base name is unique on both sides,
and finally every remaining pair is scored by how much of the new file's material came from the old one: both files are cut into spans that end at a newline (or after
64 bytes) and the spans are compared by hash.
"""
from __future__ import annotations

from .diff import RenamedSide, Side, is_binary

MAX_SCORE = 60000
DEFAULT_MINIMUM_SCORE = 30000
HASHBASE = 107927
NUM_CANDIDATE_PER_DST = 4
DEFAULT_RENAME_LIMIT = 1000
EMPTY_BLOB = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"


def span_counts(data):
    """The spans of ``data`` as ``{hash: bytes}``: a span ends at a newline or after 64 bytes, and a CR in front of an LF is ignored for text."""
    text = not is_binary(data)
    counts = {}
    a1 = a2 = 0
    n = 0
    size = len(data)
    for i, c in enumerate(data):
        if text and c == 13 and i + 1 < size and data[i + 1] == 10:
            continue
        old1 = a1
        a1 = ((a1 << 7) & 0xFFFFFFFF) ^ (a2 >> 25)
        a2 = ((a2 << 7) & 0xFFFFFFFF) ^ (old1 >> 25)
        a1 = (a1 + c) & 0xFFFFFFFF
        n += 1
        if n < 64 and c != 10:
            continue
        h = (a1 + a2 * 0x61) & 0xFFFFFFFF
        h %= HASHBASE
        counts[h] = counts.get(h, 0) + n
        n = 0
        a1 = a2 = 0
    if n > 0:
        h = ((a1 + a2 * 0x61) & 0xFFFFFFFF) % HASHBASE
        counts[h] = counts.get(h, 0) + n
    return counts


def basename_same(a, b):
    """Do the two paths end in the same file name?"""
    return a.rsplit("/", 1)[-1] == b.rsplit("/", 1)[-1]


class _File:
    """A side of a candidate pair with its content loaded on demand."""

    def __init__(self, path, side):
        self.path = path
        self.side = side
        self.mode = side.mode
        self.oid = side.oid
        self._data = None
        self._spans = None
        self.rename_used = 0

    @property
    def data(self):
        if self._data is None:
            self._data = self.side.bytes()
        return self._data

    @property
    def spans(self):
        if self._spans is None:
            self._spans = span_counts(self.data)
        return self._spans

    @property
    def regular(self):
        return self.mode in (0o100644, 0o100755)


def estimate_similarity(src, dst, minimum_score):
    if not src.regular or not dst.regular:
        return 0
    sdata, ddata = src.data, dst.data
    max_size = max(len(sdata), len(ddata))
    base_size = min(len(sdata), len(ddata))
    delta_size = max_size - base_size
    if max_size * (MAX_SCORE - minimum_score) < delta_size * MAX_SCORE:
        return 0
    if not len(ddata):
        return 0
    copied = 0
    other = dst.spans
    for h, n in src.spans.items():
        m = other.get(h)
        if m:
            copied += n if n < m else m
    return copied * MAX_SCORE // max_size


def detect_renames(changes, minimum_score=DEFAULT_MINIMUM_SCORE, rename_limit=DEFAULT_RENAME_LIMIT, relevant=None, rename_empty=True):
    """Pair up deletions and additions in a change list (``(path, old, new)``) and return the list with the pairs replaced by ``(new path, RenamedSide, new side)`` entries
    placed where the addition was. ``relevant`` (a set of paths) limits which deleted files may be used as sources, and ``rename_empty=False`` keeps empty files out of
    it, as merging does."""
    both = {}
    for path, old, new in changes:
        both.setdefault(path, []).append((old, new))
    typechanged = {p for p, v in both.items() if len(v) > 1}                      # a file turning into a link and back is no rename candidate
    srcs, dsts = [], []
    for path, old, new in changes:
        if path in typechanged or not (old is None or isinstance(old, Side)) or not (new is None or isinstance(new, Side)):
            continue                                                  # (a conflicted path is neither a deletion nor an addition)
        if old is None and new is not None:
            if rename_empty or new.oid != EMPTY_BLOB:
                dsts.append({"path": path, "file": _File(path, new), "pair": None})
        elif new is None and old is not None:
            if (relevant is None or path in relevant) and (rename_empty or old.oid != EMPTY_BLOB):
                srcs.append({"path": path, "file": _File(path, old)})
    if not srcs or not dsts:
        return list(changes)
    dst_by_path = {d["path"]: d for d in dsts}

    def record(dst, src, score):
        src["file"].rename_used += 1
        dst["pair"] = (src, score)

    # --- identical contents ------------------------------------------------------------------------------------------------------------------
    table = {}
    for s in srcs:
        table.setdefault(s["file"].oid, []).append(s)
    for d in dsts:
        best, best_score = None, -1
        tried = 100
        for s in table.get(d["file"].oid, ()):
            if (not s["file"].regular or not d["file"].regular) and s["file"].mode != d["file"].mode:
                continue
            if s["file"].rename_used:
                continue
            score = 1 + (1 if basename_same(s["path"], d["path"]) else 0)
            if score > best_score:
                best, best_score = s, score
                if score == 2:
                    break
            tried -= 1
            if not tried:
                break
        if best is not None:
            record(d, best, MAX_SCORE)
    if minimum_score >= MAX_SCORE:
        return _assemble(changes, typechanged, dst_by_path)
    if all(d["pair"] is not None for d in dsts):
        return _assemble(changes, typechanged, dst_by_path)
    srcs = [s for s in srcs if not s["file"].rename_used]
    if not srcs:
        return _assemble(changes, typechanged, dst_by_path)

    # --- unique base names ------------------------------------------------------------------------------------------------------------------
    def base(path):
        return path.rsplit("/", 1)[-1]

    src_names, dst_names = {}, {}
    for i, s in enumerate(srcs):
        src_names[base(s["path"])] = -1 if base(s["path"]) in src_names else i
    for i, d in enumerate(dsts):
        if d["pair"] is not None:
            continue
        dst_names[base(d["path"])] = -1 if base(d["path"]) in dst_names else i
    for i, s in enumerate(srcs):
        if s["file"].rename_used:
            continue
        name = base(s["path"])
        if name in dst_names:
            if src_names[name] == -1:
                continue
            j = dst_names[name]
            if j == -1:
                continue
            d = dsts[j]
            score = estimate_similarity(s["file"], d["file"], minimum_score)
            if score < minimum_score:
                continue
            record(d, s, score)

    # --- too many candidates? ----------------------------------------------------------------------------------------------------------------
    left_dsts = [d for d in dsts if d["pair"] is None]
    left_srcs = [s for s in srcs if not s["file"].rename_used]
    if not left_dsts or not left_srcs:
        return _assemble(changes, typechanged, dst_by_path)
    limit = rename_limit if rename_limit > 0 else 32767
    if not ((len(left_dsts) <= limit or len(left_srcs) <= limit) and len(left_dsts) * len(left_srcs) <= limit * limit):
        return _assemble(changes, typechanged, dst_by_path)

    # --- the score matrix --------------------------------------------------------------------------------------------------------------------
    cands = []
    for di, d in enumerate(dsts):
        if d["pair"] is not None:
            continue
        slots = [None] * NUM_CANDIDATE_PER_DST
        for si, s in enumerate(srcs):
            score = estimate_similarity(s["file"], d["file"], minimum_score)
            name_score = 1 if basename_same(s["path"], d["path"]) else 0
            _record_if_better(slots, (score, name_score, di, si))
        cands.extend(slots)
    # a stable sort: best score first, then the base name match; empty slots sink to the end
    cands.sort(key=lambda c: (1, 0, 0) if c is None else (0, -c[0], -c[1]))
    for c in cands:
        if c is None or c[0] < minimum_score:
            break
        score, _ns, di, si = c
        d = dsts[di]
        if d["pair"] is not None or srcs[si]["file"].rename_used:
            continue
        record(d, srcs[si], score)
    return _assemble(changes, typechanged, dst_by_path)


def _sorts_after(a, b):
    """git's ``score_compare(a, b) > 0``: ``a`` is the worse candidate (an empty slot is worse than anything)."""
    if a is None:
        return b is not None
    if b is None:
        return False
    if a[0] == b[0]:
        return a[1] < b[1]
    return a[0] < b[0]


def _record_if_better(slots, candidate):
    worst = 0
    for i in range(1, len(slots)):
        if _sorts_after(slots[i], slots[worst]):
            worst = i
    if _sorts_after(slots[worst], candidate):
        slots[worst] = candidate


def _assemble(changes, typechanged, dst_by_path):
    used = {d["pair"][0]["path"] for d in dst_by_path.values() if d["pair"]}
    out = []
    for path, old, new in changes:
        if path in typechanged:
            out.append((path, old, new))
            continue
        if old is None and new is not None:
            d = dst_by_path.get(path)
            if d is not None and d["pair"] is not None:
                src, score = d["pair"]
                side = src["file"].side
                out.append((path, RenamedSide(side.mode, side.oid, side.data, src["path"], score), new))
                continue
        elif new is None and old is not None and path in used:
            continue
        out.append((path, old, new))
    return out


def parse_score(text):
    """git's ``parse_rename_score``: ``50%``, ``0.5`` and ``5`` (the digits after an implied decimal point) all mean half."""
    num, scale, dot = 0, 1, False
    for ch in text:
        if not dot and ch == ".":
            scale, dot = 1, True
        elif ch == "%":
            scale = scale * 100 if dot else 100
            break
        elif ch.isdigit():
            if scale < 100000:
                scale *= 10
                num = num * 10 + int(ch)
        else:
            break
    return MAX_SCORE if num >= scale else MAX_SCORE * num // scale


def find_follow_source(parent_files, current_files, path, new_side, odb_side, minimum_score=DEFAULT_MINIMUM_SCORE):
    """For ``log --follow``: the file of the parent commit that the new file at ``path`` came from, as a rename or as a copy. Identical content is looked for in every
    file of the parent; otherwise files the commit deleted or changed are compared first and only if none of them is similar enough the untouched ones (git's
    ``--find-copies-harder`` does it in this order). Returns ``(source path, score)`` or ``None``. ``odb_side(mode, oid)`` makes a ``Side`` that can read the content."""
    dst = _File(path, new_side)
    candidates = sorted(parent_files.items(), key=lambda kv: kv[0].encode("utf-8", "surrogateescape"))
    best, best_score = None, -1
    for p, (mode, oid) in candidates:                                   # identical content first, preferring the same base name
        if oid != new_side.oid or ((mode not in (0o100644, 0o100755) or not dst.regular) and mode != new_side.mode):
            continue
        score = 1 + (1 if basename_same(p, path) else 0)
        if score > best_score:
            best, best_score = p, score
            if score == 2:
                break
    if best is not None:
        return best, MAX_SCORE
    for untouched in (False, True):
        best, best_key = None, None
        for p, (mode, oid) in candidates:
            if mode not in (0o100644, 0o100755) or (current_files.get(p) == (mode, oid)) != untouched:
                continue
            score = estimate_similarity(_File(p, odb_side(mode, oid)), dst, minimum_score)
            key = (score, 1 if basename_same(p, path) else 0)
            if best_key is None or key > best_key:
                best, best_key = p, key
        if best is not None and best_key[0] >= minimum_score:
            return best, best_key[0]
    return None
