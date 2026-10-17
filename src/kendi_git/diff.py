"""Line diffs: Myers' O(ND) algorithm in linear space, and git's unified patch format.

``myers`` finds a *shortest* edit script (the number of removed plus added lines is minimal) by divide and conquer on the middle snake. Where several shortest scripts
exist, git's xdiff may pick another one than this code does (it post-processes the result to slide changes and prefers indentation boundaries); the patches are
equally valid and ``git apply`` takes them, but they are not always byte-identical to git's. For inputs whose lines are all distinct the script is unique and the output
matches git's byte for byte, which is what the tests check.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .objects import MODE_FILE, MODE_GITLINK, MODE_LINK, NULL_OID, hash_object


def split_lines(data):
    """Lines of ``data`` with their line ends kept, so that a missing final newline is a difference like any other."""
    parts = data.split(b"\n")
    out = [p + b"\n" for p in parts[:-1]]
    if parts[-1]:
        out.append(parts[-1])
    return out


# --- the diff algorithm: a port of git's xdiff (xdiffi.c, xprepare.c) -------------------------------------------------------------------------------
# Myers' O(ND) algorithm with the same divide-and-conquer split, heuristics and pre-processing as git, so that equal inputs give the *same* edit script, not just an
# equally short one: common ends are trimmed, lines absent from the other file (or too frequent in it) are marked changed before the search, and an expensive search
# settles for a good-enough path once its cost passes a threshold.
XDL_MAX_COST_MIN, XDL_HEUR_MIN_COST, XDL_SNAKE_CNT, XDL_K_HEUR = 256, 256, 20, 4
XDL_MAX_EQLIMIT, XDL_SIMSCAN_WINDOW, XDL_KPDIS_RUN = 1024, 100, 4
LINE_MAX = 1 << 62


def _bogosqrt(n):
    i = 1
    while n > 0:
        n >>= 2
        i <<= 1
    return i


def _split(ha1, off1, lim1, ha2, off2, lim2, kvdf, kvdb, base, need_min, mxcost):
    """xdl_split: find the point where the forward and the backward search meet. ``kvdf``/``kvdb`` are indexed by diagonal + ``base``. Returns ``(i1, i2, min_lo, min_hi)``."""
    dmin, dmax = off1 - lim2, lim1 - off2
    fmid, bmid = off1 - off2, lim1 - lim2
    odd = (fmid - bmid) & 1
    fmin = fmax = fmid
    bmin = bmax = bmid
    kvdf[base + fmid] = off1
    kvdb[base + bmid] = lim1
    ec = 1
    while True:
        got_snake = False
        if fmin > dmin:
            fmin -= 1
            kvdf[base + fmin - 1] = -1
        else:
            fmin += 1
        if fmax < dmax:
            fmax += 1
            kvdf[base + fmax + 1] = -1
        else:
            fmax -= 1
        d = fmax
        while d >= fmin:
            i1 = kvdf[base + d - 1] + 1 if kvdf[base + d - 1] >= kvdf[base + d + 1] else kvdf[base + d + 1]
            prev1 = i1
            i2 = i1 - d
            while i1 < lim1 and i2 < lim2 and ha1[i1] == ha2[i2]:
                i1 += 1
                i2 += 1
            if i1 - prev1 > XDL_SNAKE_CNT:
                got_snake = True
            kvdf[base + d] = i1
            if odd and bmin <= d <= bmax and kvdb[base + d] <= i1:
                return i1, i2, True, True
            d -= 2
        if bmin > dmin:
            bmin -= 1
            kvdb[base + bmin - 1] = LINE_MAX
        else:
            bmin += 1
        if bmax < dmax:
            bmax += 1
            kvdb[base + bmax + 1] = LINE_MAX
        else:
            bmax -= 1
        d = bmax
        while d >= bmin:
            i1 = kvdb[base + d - 1] if kvdb[base + d - 1] < kvdb[base + d + 1] else kvdb[base + d + 1] - 1
            prev1 = i1
            i2 = i1 - d
            while i1 > off1 and i2 > off2 and ha1[i1 - 1] == ha2[i2 - 1]:
                i1 -= 1
                i2 -= 1
            if prev1 - i1 > XDL_SNAKE_CNT:
                got_snake = True
            kvdb[base + d] = i1
            if not odd and fmin <= d <= fmax and i1 <= kvdf[base + d]:
                return i1, i2, True, True
            d -= 2
        if need_min:
            ec += 1
            continue
        if got_snake and ec > XDL_HEUR_MIN_COST:                          # the cost is high and there was a good snake: look for an "interesting" diagonal
            best = 0
            d = fmax
            while d >= fmin:
                dd = d - fmid if d > fmid else fmid - d
                i1 = kvdf[base + d]
                i2 = i1 - d
                v = (i1 - off1) + (i2 - off2) - dd
                if v > XDL_K_HEUR * ec and v > best and off1 + XDL_SNAKE_CNT <= i1 < lim1 and off2 + XDL_SNAKE_CNT <= i2 < lim2:
                    for k in range(1, XDL_SNAKE_CNT + 1):
                        if ha1[i1 - k] != ha2[i2 - k]:
                            break
                        if k == XDL_SNAKE_CNT:
                            best, found = v, (i1, i2)
                d -= 2
            if best > 0:
                return found[0], found[1], True, False
            best = 0
            d = bmax
            while d >= bmin:
                dd = d - bmid if d > bmid else bmid - d
                i1 = kvdb[base + d]
                i2 = i1 - d
                v = (lim1 - i1) + (lim2 - i2) - dd
                if v > XDL_K_HEUR * ec and v > best and off1 < i1 <= lim1 - XDL_SNAKE_CNT and off2 < i2 <= lim2 - XDL_SNAKE_CNT:
                    for k in range(XDL_SNAKE_CNT):
                        if ha1[i1 + k] != ha2[i2 + k]:
                            break
                        if k == XDL_SNAKE_CNT - 1:
                            best, found = v, (i1, i2)
                d -= 2
            if best > 0:
                return found[0], found[1], False, True
        if ec >= mxcost:                                                  # enough is enough: take the furthest reaching path
            fbest = fbest1 = -1
            d = fmax
            while d >= fmin:
                i1 = min(kvdf[base + d], lim1)
                i2 = i1 - d
                if lim2 < i2:
                    i1, i2 = lim2 + d, lim2
                if fbest < i1 + i2:
                    fbest, fbest1 = i1 + i2, i1
                d -= 2
            bbest = bbest1 = LINE_MAX
            d = bmax
            while d >= bmin:
                i1 = max(off1, kvdb[base + d])
                i2 = i1 - d
                if i2 < off2:
                    i1, i2 = off2 + d, off2
                if i1 + i2 < bbest:
                    bbest, bbest1 = i1 + i2, i1
                d -= 2
            if (lim1 + lim2) - bbest < fbest - (off1 + off2):
                return fbest1, fbest - fbest1, True, False
            return bbest1, bbest - bbest1, False, True
        ec += 1


def _recs_cmp(ha1, rindex1, rchg1, ha2, rindex2, rchg2, mxcost):
    """xdl_recs_cmp without recursion: mark in ``rchg1``/``rchg2`` the lines that differ."""
    n1, n2 = len(ha1), len(ha2)
    base = n2 + 1
    size = n1 + n2 + 6
    kvdf, kvdb = [0] * (size + n2 + 2), [0] * (size + n2 + 2)
    stack = [(0, n1, 0, n2, False)]
    while stack:
        off1, lim1, off2, lim2, need_min = stack.pop()
        while off1 < lim1 and off2 < lim2 and ha1[off1] == ha2[off2]:
            off1 += 1
            off2 += 1
        while off1 < lim1 and off2 < lim2 and ha1[lim1 - 1] == ha2[lim2 - 1]:
            lim1 -= 1
            lim2 -= 1
        if off1 == lim1:
            for k in range(off2, lim2):
                rchg2[rindex2[k]] = True
        elif off2 == lim2:
            for k in range(off1, lim1):
                rchg1[rindex1[k]] = True
        else:
            i1, i2, min_lo, min_hi = _split(ha1, off1, lim1, ha2, off2, lim2, kvdf, kvdb, base, need_min, mxcost)
            stack.append((i1, lim1, i2, lim2, min_hi))
            stack.append((off1, i1, off2, i2, min_lo))


def _clean_mmatch(dis, i, s, e):
    """xdl_clean_mmatch: should a line with many matches in the other file be discarded because it sits inside a run of lines that match nothing?"""
    if i - s > XDL_SIMSCAN_WINDOW:
        s = i - XDL_SIMSCAN_WINDOW
    if e - i > XDL_SIMSCAN_WINDOW:
        e = i + XDL_SIMSCAN_WINDOW
    rdis0, rpdis0, r = 0, 1, 1
    while i - r >= s:
        if not dis[i - r]:
            rdis0 += 1
        elif dis[i - r] == 2:
            rpdis0 += 1
        else:
            break
        r += 1
    if rdis0 == 0:
        return False
    rdis1, rpdis1, r = 0, 1, 1
    while i + r <= e:
        if not dis[i + r]:
            rdis1 += 1
        elif dis[i + r] == 2:
            rpdis1 += 1
        else:
            break
        r += 1
    if rdis1 == 0:
        return False
    rdis1 += rdis0
    rpdis1 += rpdis0
    return rpdis1 * XDL_KPDIS_RUN < (rpdis1 + rdis1)


def _myers_raw(a, b):
    """xdl_do_diff without the final compaction: ``(removed, added)`` flags per line of ``a`` and ``b``."""
    classes = {}
    ha1 = [classes.setdefault(line, len(classes)) for line in a]
    ha2 = [classes.setdefault(line, len(classes)) for line in b]
    count1, count2 = {}, {}
    for h in ha1:
        count1[h] = count1.get(h, 0) + 1
    for h in ha2:
        count2[h] = count2.get(h, 0) + 1
    n1, n2 = len(a), len(b)
    rchg1, rchg2 = [False] * n1, [False] * n2
    lim = min(n1, n2)                                                    # xdl_trim_ends
    dstart = 0
    while dstart < lim and ha1[dstart] == ha2[dstart]:
        dstart += 1
    tail = 0
    while tail < lim - dstart and ha1[n1 - 1 - tail] == ha2[n2 - 1 - tail]:
        tail += 1
    dend1, dend2 = n1 - tail - 1, n2 - tail - 1
    mlim = min(_bogosqrt(n1), XDL_MAX_EQLIMIT)                           # xdl_cleanup_records
    dis1, dis2 = {}, {}
    for i in range(dstart, dend1 + 1):
        nm = count2.get(ha1[i], 0)
        dis1[i] = 0 if nm == 0 else 2 if nm >= mlim else 1
    for i in range(dstart, dend2 + 1):
        nm = count1.get(ha2[i], 0)
        dis2[i] = 0 if nm == 0 else 2 if nm >= mlim else 1
    kept1, kept2 = [], []
    for i in range(dstart, dend1 + 1):
        if dis1[i] == 1 or (dis1[i] == 2 and not _clean_mmatch(dis1, i, dstart, dend1)):
            kept1.append(i)
        else:
            rchg1[i] = True
    for i in range(dstart, dend2 + 1):
        if dis2[i] == 1 or (dis2[i] == 2 and not _clean_mmatch(dis2, i, dstart, dend2)):
            kept2.append(i)
        else:
            rchg2[i] = True
    mxcost = max(_bogosqrt(len(kept1) + len(kept2) + 3), XDL_MAX_COST_MIN)
    _recs_cmp([ha1[i] for i in kept1], kept1, rchg1, [ha2[i] for i in kept2], kept2, rchg2, mxcost)
    return rchg1, rchg2


def myers(a, b, indent_heuristic=False):
    """``(removed, added)``: one boolean per line of ``a`` / ``b`` telling whether the line is part of a shortest edit script. Like git's xdiff the raw script is then
    *compacted*: every group of changed lines is slid over equal neighbouring lines to merge with other groups and to line up with changes in the other file, and with
    ``indent_heuristic`` (what ``git diff`` does by default) ties are broken by where the split looks best given blank lines and indentation."""
    removed, added = _myers_raw(a, b)
    _compact(a, removed, added, indent_heuristic)
    _compact(b, added, removed, indent_heuristic)
    return removed, added


# --- xdiff's change compaction, a port of xdl_change_compact and the indent heuristic of git 2.14+ -----------------------------------------------------
MAX_INDENT, MAX_BLANKS = 200, 20
START_OF_FILE_PENALTY, END_OF_FILE_PENALTY = 1, 21
TOTAL_BLANK_WEIGHT, POST_BLANK_WEIGHT = -30, 6
RELATIVE_INDENT_PENALTY, RELATIVE_INDENT_WITH_BLANK_PENALTY = -4, 10
RELATIVE_OUTDENT_PENALTY, RELATIVE_OUTDENT_WITH_BLANK_PENALTY = 24, 17
RELATIVE_DEDENT_PENALTY, RELATIVE_DEDENT_WITH_BLANK_PENALTY = 23, 17
INDENT_WEIGHT, INDENT_HEURISTIC_MAX_SLIDING = 60, 100


def _indent_of(line):
    """Indentation of a line (tabs to multiples of 8), or -1 for a blank line."""
    ret = 0
    for c in line:
        if c not in b" \t\n\r":
            return ret
        if c == 0x20:
            ret += 1
        elif c == 0x09:
            ret += 8 - ret % 8
        if ret >= MAX_INDENT:
            return MAX_INDENT
    return -1


def _measure_split(lines, split):
    n = len(lines)
    end_of_file = split >= n
    indent = -1 if end_of_file else _indent_of(lines[split])
    pre_blank, pre_indent = 0, -1
    for i in range(split - 1, -1, -1):
        pre_indent = _indent_of(lines[i])
        if pre_indent != -1:
            break
        pre_blank += 1
        if pre_blank == MAX_BLANKS:
            pre_indent = 0
            break
    post_blank, post_indent = 0, -1
    for i in range(split + 1, n):
        post_indent = _indent_of(lines[i])
        if post_indent != -1:
            break
        post_blank += 1
        if post_blank == MAX_BLANKS:
            post_indent = 0
            break
    return end_of_file, indent, pre_blank, pre_indent, post_blank, post_indent


def _score_add_split(m, score):
    end_of_file, indent, pre_blank, pre_indent, post_blank, post_indent = m
    penalty = 0
    if pre_indent == -1 and pre_blank == 0:
        penalty += START_OF_FILE_PENALTY
    if end_of_file:
        penalty += END_OF_FILE_PENALTY
    post = 1 + post_blank if indent == -1 else 0
    total_blank = pre_blank + post
    penalty += TOTAL_BLANK_WEIGHT * total_blank + POST_BLANK_WEIGHT * post
    here = indent if indent != -1 else post_indent
    any_blanks = total_blank != 0
    score[0] += here
    if here == -1 or pre_indent == -1:
        pass
    elif here > pre_indent:
        penalty += RELATIVE_INDENT_WITH_BLANK_PENALTY if any_blanks else RELATIVE_INDENT_PENALTY
    elif here == pre_indent:
        pass
    elif post_indent != -1 and post_indent > here:
        penalty += RELATIVE_OUTDENT_WITH_BLANK_PENALTY if any_blanks else RELATIVE_OUTDENT_PENALTY
    else:
        penalty += RELATIVE_DEDENT_WITH_BLANK_PENALTY if any_blanks else RELATIVE_DEDENT_PENALTY
    score[1] += penalty


def _score_cmp(s1, s2):
    cmp_indents = (s1[0] > s2[0]) - (s1[0] < s2[0])
    return INDENT_WEIGHT * cmp_indents + (s1[1] - s2[1])


def _compact(lines, rc, orc, indent_heuristic):
    """Slide the groups of changed lines of one file (``rc`` its change flags, ``orc`` the other file's) to canonical places; ``lines`` are this file's records."""
    n, on = len(rc), len(orc)

    def ch(flags, i, size):
        return 0 <= i < size and flags[i]

    def group_init(flags, size):
        end = 0
        while ch(flags, end, size):
            end += 1
        return [0, end]

    def group_next(flags, size, g):
        if g[1] == size:
            return False
        g[0] = g[1] + 1
        g[1] = g[0]
        while ch(flags, g[1], size):
            g[1] += 1
        return True

    def group_previous(flags, size, g):
        if g[0] == 0:
            return False
        g[1] = g[0] - 1
        g[0] = g[1]
        while ch(flags, g[0] - 1, size):
            g[0] -= 1
        return True

    def slide_down(g):
        if g[1] < n and lines[g[0]] == lines[g[1]]:
            rc[g[0]] = False
            rc[g[1]] = True
            g[0] += 1
            g[1] += 1
            while ch(rc, g[1], n):
                g[1] += 1
            return True
        return False

    def slide_up(g):
        if g[0] > 0 and lines[g[0] - 1] == lines[g[1] - 1]:
            g[0] -= 1
            rc[g[0]] = True
            g[1] -= 1
            rc[g[1]] = False
            while ch(rc, g[0] - 1, n):
                g[0] -= 1
            return True
        return False

    g, go = group_init(rc, n), group_init(orc, on)
    while True:
        if g[1] != g[0]:                                   # an empty group (no change at this gap) needs nothing
            while True:
                groupsize = g[1] - g[0]
                end_matching_other = -1
                while slide_up(g):
                    assert group_previous(orc, on, go), "group sync broken sliding up"
                earliest_end = g[1]
                if go[1] > go[0]:
                    end_matching_other = g[1]
                while slide_down(g):
                    assert group_next(orc, on, go), "group sync broken sliding down"
                    if go[1] > go[0]:
                        end_matching_other = g[1]
                if groupsize == g[1] - g[0]:
                    break
            if g[1] == earliest_end:
                pass                                       # no shifting was possible
            elif end_matching_other != -1:                 # line the group up with the last group of the other file it can align with
                while go[1] == go[0]:
                    assert slide_up(g) and group_previous(orc, on, go), "match disappeared"
            elif indent_heuristic:
                shift = max(earliest_end, g[1] - groupsize - 1, g[1] - INDENT_HEURISTIC_MAX_SLIDING)
                best_shift, best_score = -1, None
                while shift <= g[1]:
                    score = [0, 0]
                    _score_add_split(_measure_split(lines, shift), score)
                    _score_add_split(_measure_split(lines, shift - groupsize), score)
                    if best_shift == -1 or _score_cmp(score, best_score) <= 0:
                        best_score, best_shift = score, shift
                    shift += 1
                while g[1] > best_shift:
                    assert slide_up(g) and group_previous(orc, on, go), "best shift unreached"
        if not group_next(rc, n, g):
            break
        assert group_next(orc, on, go), "group sync broken moving to the next group"


@dataclass
class Hunk:
    a_start: int          # 0-based index of the first old line in the hunk
    a_count: int
    b_start: int
    b_count: int
    lines: list           # (tag, text): tag is " ", "-" or "+"


def hunks(a, b, context=3):
    """Group the edit script into hunks with ``context`` unchanged lines around changes; changes closer than twice that share a hunk."""
    removed, added = myers(a, b, indent_heuristic=True)
    ops = []                                     # one entry per output line: (tag, a_index, b_index)
    i = j = 0
    while i < len(a) or j < len(b):
        if i < len(a) and removed[i]:
            ops.append(("-", i, j))
            i += 1
        elif j < len(b) and added[j]:
            ops.append(("+", i, j))
            j += 1
        else:
            ops.append((" ", i, j))
            i += 1
            j += 1
    # within a run of changes git prints all removals before the additions
    fixed = []
    k = 0
    while k < len(ops):
        if ops[k][0] == " ":
            fixed.append(ops[k])
            k += 1
            continue
        run = []
        while k < len(ops) and ops[k][0] != " ":
            run.append(ops[k])
            k += 1
        fixed += [o for o in run if o[0] == "-"] + [o for o in run if o[0] == "+"]
    ops = fixed
    change_at = [n for n, o in enumerate(ops) if o[0] != " "]
    out = []
    if not change_at:
        return out
    start = prev = change_at[0]
    groups = []
    for n in change_at[1:]:
        if n - prev - 1 > 2 * context:
            groups.append((start, prev))
            start = n
        prev = n
    groups.append((start, prev))
    for first, last in groups:
        lo, hi = max(0, first - context), min(len(ops), last + context + 1)
        chunk = ops[lo:hi]
        lines = [(tag, a[ai] if tag != "+" else b[bj]) for tag, ai, bj in chunk]
        a_count = sum(1 for t, _ in lines if t != "+")
        b_count = sum(1 for t, _ in lines if t != "-")
        a_start = chunk[0][1]
        b_start = chunk[0][2]
        out.append(Hunk(a_start, a_count, b_start, b_count, lines))
    return out


_FUNC = re.compile(rb"^[A-Za-z_$]")


def _func_context(a, before):
    """The text git appends to a hunk header: the nearest line above the hunk that starts like a function or section heading."""
    for idx in range(before - 1, -1, -1):
        line = a[idx]
        if _FUNC.match(line):
            return b" " + line.rstrip(b"\r\n")[:80].rstrip()
    return b""


def _range(start, count):
    if count == 0:
        return b"%d,0" % start               # an empty range is named by the line before it
    return b"%d" % (start + 1) if count == 1 else b"%d,%d" % (start + 1, count)


def format_hunks(a, b, context=3):
    out = []
    for h in hunks(a, b, context):
        header = b"@@ -%s +%s @@%s\n" % (_range(h.a_start, h.a_count), _range(h.b_start, h.b_count), _func_context(a, h.a_start))
        out.append(header)
        for tag, text in h.lines:
            out.append(tag.encode() + text)
            if not text.endswith(b"\n"):
                out.append(b"\n\\ No newline at end of file\n")
    return b"".join(out)


# --- git's patch format ---------------------------------------------------------------------------------------------------------------------------
@dataclass
class Side:
    mode: int
    oid: str
    data: object            # bytes, or a zero-argument callable returning bytes (read lazily, only when a content diff is needed)

    def bytes(self):
        if self.mode == MODE_GITLINK:
            return b"Subproject commit %s\n" % self.oid.encode()
        return self.data() if callable(self.data) else self.data


@dataclass
class Change:
    path: str
    old: object            # Side or None
    new: object


def quote_path(path, space=False):
    """git's C-style quoting of unusual path names (``core.quotePath`` on); the short/porcelain status formats also quote names that contain a space (``space=True``)."""
    raw = path.encode("utf-8", "surrogateescape") if isinstance(path, str) else path
    if all(0x20 <= c < 0x7f and c not in (0x22, 0x5c) and not (space and c == 0x20) for c in raw):
        return raw.decode("ascii")
    esc = {7: "\\a", 8: "\\b", 9: "\\t", 10: "\\n", 11: "\\v", 12: "\\f", 13: "\\r", 34: '\\"', 92: "\\\\"}
    return '"' + "".join(esc.get(c) or (chr(c) if 0x20 <= c < 0x7f else "\\%03o" % c) for c in raw) + '"'


def is_binary(data):
    return b"\x00" in data[:8000]


def diff_flat(old, new):
    """``[(path, old, new)]`` between two ``{path: (mode, oid)}`` snapshots, sorted by path, ``old``/``new`` being ``(mode, oid)`` or ``None``; a file turning into a
    symlink (or the reverse) is reported as a removal plus an addition, as git does."""
    changes = []
    for path in sorted(set(old) | set(new), key=lambda p: p.encode("utf-8", "surrogateescape")):
        o, n = old.get(path), new.get(path)
        if o == n:
            continue
        if o and n and ((o[0] == MODE_LINK) != (n[0] == MODE_LINK) or (o[0] == MODE_GITLINK) != (n[0] == MODE_GITLINK)):
            changes.append((path, o, None))
            changes.append((path, None, n))
        else:
            changes.append((path, o, n))
    return changes


def render_change(path, old, new, abbrev, context=3, a_prefix="a/", b_prefix="b/"):
    """The patch text of one change. ``old`` and ``new`` are ``Side`` objects or ``None``; ``abbrev`` shortens an object id for the ``index`` line."""
    qa, qb = quote_path(a_prefix + path), quote_path(b_prefix + path)
    head = [b"diff --git %s %s\n" % (qa.encode(), qb.encode())]
    if old is None:
        head.append(b"new file mode %06o\n" % new.mode)
    elif new is None:
        head.append(b"deleted file mode %06o\n" % old.mode)
    elif old.mode != new.mode:
        head.append(b"old mode %06o\nnew mode %06o\n" % (old.mode, new.mode))
    old_oid = old.oid if old else NULL_OID
    new_oid = new.oid if new else NULL_OID
    if old_oid != new_oid:
        mode_part = b" %06o" % new.mode if old is not None and new is not None and old.mode == new.mode else b""
        head.append(b"index %s..%s%s\n" % (abbrev(old_oid).encode(), abbrev(new_oid).encode(), mode_part))
    else:
        return b"".join(head)                                                  # a pure mode change has no content part
    a_data = old.bytes() if old else b""
    b_data = new.bytes() if new else b""
    if a_data == b_data:
        return b"".join(head)
    if is_binary(a_data) or is_binary(b_data):
        left = b"/dev/null" if old is None else qa.encode()
        right = b"/dev/null" if new is None else qb.encode()
        return b"".join(head) + b"Binary files %s and %s differ\n" % (left, right)
    tab = b"\t" if " " in path else b""
    left = b"/dev/null" if old is None else qa.encode() + tab
    right = b"/dev/null" if new is None else qb.encode() + tab
    head.append(b"--- %s\n+++ %s\n" % (left, right))
    return b"".join(head) + format_hunks(split_lines(a_data), split_lines(b_data), context)


def counts(old, new):
    """``(added, removed)`` line counts for ``numstat``; ``None`` for binary content."""
    a_data = old.bytes() if old else b""
    b_data = new.bytes() if new else b""
    if is_binary(a_data) or is_binary(b_data):
        return None
    removed, added = myers(split_lines(a_data), split_lines(b_data))
    return sum(added), sum(removed)


def blob_side(mode, oid, odb):
    return Side(mode, oid, lambda: odb.read(oid)[1])


def file_side(mode, oid, data):
    return Side(mode, oid, data)


def hash_data(data):
    return hash_object("blob", data)


__all__ = ["myers", "hunks", "format_hunks", "split_lines", "Change", "Side", "diff_flat", "render_change", "counts", "blob_side", "file_side", "quote_path", "is_binary",
           "MODE_FILE", "hash_data"]


# --- diffstat: a port of show_stats() from git's diff.c --------------------------------------------------------------------------------------------
def _decimal_width(n):
    return len(str(n))


def merge_typechanges(changes):
    """The patch text shows a type change (file <-> symlink) as a removal plus an addition; every summary form (--stat, --numstat, --summary, --name-status) shows
    one entry for the path. Pair them up again."""
    out = []
    for path, old, new in changes:
        if out and out[-1][0] == path and out[-1][2] is None and out[-1][1] is not None and old is None and new is not None:
            out[-1] = (path, out[-1][1], new)
        else:
            out.append((path, old, new))
    return out


def stat_rows(changes):
    """One row per change for ``--stat``: ``(quoted name, added, deleted, binary)``. For a binary file ``added``/``deleted`` are the new/old sizes in bytes."""
    rows = []
    for path, old, new in merge_typechanges(changes):
        a_data = old.bytes() if old else b""
        b_data = new.bytes() if new else b""
        if is_binary(a_data) or is_binary(b_data):
            rows.append((quote_path(path), len(b_data), len(a_data), True))
        else:
            c = counts(old, new)
            rows.append((quote_path(path), c[0], c[1], False))
    return rows


def _scale_linear(it, width, max_change):
    return 0 if not it else 1 + (it * (width - 1) // max_change)


def summary_line(files, adds, dels):
    """git's ``N files changed, N insertions(+), N deletions(-)`` line (without the leading space handling of --shortstat's callers)."""
    out = " %d file%s changed" % (files, "" if files == 1 else "s")
    if adds or not dels:
        out += ", %d insertion%s(+)" % (adds, "" if adds == 1 else "s")
    if dels or not adds:
        out += ", %d deletion%s(-)" % (dels, "" if dels == 1 else "s")
    return out + "\n"


def format_stat(changes, width=80):
    """The text of ``git diff --stat`` for these changes (80 columns, as git prints when the output is not a terminal)."""
    rows = stat_rows(changes)
    if not rows:
        return ""
    max_len = max_change = bin_width = number_width = 0
    for name, added, deleted, binary in rows:
        max_len = max(max_len, len(name))
        if binary:
            bin_width = max(bin_width, 14 + _decimal_width(added) + _decimal_width(deleted))
            number_width = 3
            continue
        max_change = max(max_change, added + deleted)
    number_width = max(_decimal_width(max_change), number_width)
    if width < 16 + 6 + number_width:
        width = 16 + 6 + number_width
    graph_width = max_change if max_change + 4 > bin_width else bin_width - 4
    name_width = max_len
    if name_width + number_width + 6 + graph_width > width:
        if graph_width > width * 3 // 8 - number_width - 6:
            graph_width = width * 3 // 8 - number_width - 6
            if graph_width < 6:
                graph_width = 6
        if name_width > width - number_width - 6 - graph_width:
            name_width = width - number_width - 6 - graph_width
        else:
            graph_width = width - number_width - 6 - name_width
    out, adds, dels = [], 0, 0
    for name, added, deleted, binary in rows:
        prefix, length, shown = "", name_width, name
        if name_width < len(name):
            prefix = "..."
            length -= 3
            shown = name[len(name) - length:]
            slash = shown.find("/")
            if slash >= 0:
                shown = shown[slash:]
        line = " %s%s |" % (prefix, shown.ljust(length))
        if binary:
            line += " " + "Bin".rjust(number_width)
            if not added and not deleted:
                out.append(line + "\n")
                continue
            out.append(line + " %d -> %d bytes\n" % (deleted, added))
            continue
        add, dele = added, deleted
        if graph_width <= max_change:
            total = _scale_linear(add + dele, graph_width, max_change)
            if total < 2 and add and dele:
                total = 2
            if add < dele:
                add = _scale_linear(add, graph_width, max_change)
                dele = total - add
            else:
                dele = _scale_linear(dele, graph_width, max_change)
                add = total - dele
        out.append(line + " " + str(added + deleted).rjust(number_width) + (" " if added + deleted else "") + "+" * add + "-" * dele + "\n")
        adds += added
        dels += deleted
    return "".join(out) + summary_line(len(rows), adds, dels)


def format_shortstat(changes):
    rows = stat_rows(changes)
    if not rows:
        return ""
    return summary_line(len(rows), sum(r[1] for r in rows if not r[3]), sum(r[2] for r in rows if not r[3]))


def format_summary(changes):
    """``--summary``: creations, deletions and mode changes."""
    out = []
    for path, old, new in merge_typechanges(changes):
        q = quote_path(path)
        if old is None:
            out.append(" create mode %06o %s\n" % (new.mode, q))
        elif new is None:
            out.append(" delete mode %06o %s\n" % (old.mode, q))
        elif old.mode != new.mode:
            out.append(" mode change %06o => %06o %s\n" % (old.mode, new.mode, q))
    return "".join(out)
