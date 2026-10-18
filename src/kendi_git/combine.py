"""Combined diffs (``diff --cc`` / ``diff --combined``): a port of git's combine-diff.c.

A merge result is compared with *all* its parents at once. Every line of the result gets one flag bit per parent ("this parent did not have the line"), lines a parent
had but the result lost hang off the result line that follows them, and the output marks each line with one column per parent. ``--cc`` (``dense``) then drops the hunks
in which the result simply took one parent's side.
"""
from __future__ import annotations

from . import diff as df

NULL_OID = "0" * 40


class _Lost:
    __slots__ = ("text", "map")

    def __init__(self, text, parent_map):
        self.text = text
        self.map = parent_map


class _Line:
    __slots__ = ("text", "lost", "flag", "p_lno")

    def __init__(self, text, num_parent):
        self.text = text
        self.lost = []
        self.flag = 0
        self.p_lno = [0] * num_parent


def _runs(a, b):
    """The groups of changed lines between two line lists as ``(a_start, a_count, b_start, b_count)``."""
    removed, added = df.myers(a, b, indent_heuristic=True)
    runs, i, j = [], 0, 0
    while i < len(a) or j < len(b):
        i0, j0 = i, j
        while i < len(a) and removed[i]:
            i += 1
        while j < len(b) and added[j]:
            j += 1
        if (i, j) != (i0, j0):
            runs.append((i0, i - i0, j0, j - j0))
        else:
            i += 1
            j += 1
    return runs


def _coalesce(base, new, n):
    """Merge the lines parent ``n`` lost at one place into the lines earlier parents lost there (a longest common subsequence keeps the shared ones together)."""
    if not new:
        return
    bit = 1 << n
    nb, nn = len(base), len(new)
    lcs = [[0] * (nn + 1) for _ in range(nb + 1)]
    direction = [[0] * (nn + 1) for _ in range(nb + 1)]          # 0 base, 1 new, 2 match
    for i in range(nb + 1):
        direction[i][0] = 0
    for j in range(1, nn + 1):
        direction[0][j] = 1
    for i in range(1, nb + 1):
        for j in range(1, nn + 1):
            if base[i - 1].text == new[j - 1]:
                lcs[i][j] = lcs[i - 1][j - 1] + 1
                direction[i][j] = 2
            elif lcs[i][j - 1] >= lcs[i - 1][j]:
                lcs[i][j] = lcs[i][j - 1]
                direction[i][j] = 1
            else:
                lcs[i][j] = lcs[i - 1][j]
                direction[i][j] = 0
    i, j = nb, nn
    while i != 0 or j != 0:
        d = direction[i][j]
        if d == 2:
            base[i - 1].map |= bit
            i -= 1
            j -= 1
        elif d == 1:
            base.insert(i, _Lost(new[j - 1], bit))
            j -= 1
        else:
            i -= 1


def _lines(data):
    return df.split_lines(data)


def _combine_one(sl, result_lines, parent_data, n, num_parent):
    bit = 1 << n
    cnt = len(result_lines)
    parent_lines = _lines(parent_data)
    for a0, an, b0, bn in _runs(parent_lines, result_lines):
        for k in range(bn):
            sl[b0 + k].flag |= bit
        _coalesce(sl[b0].lost, [x[:-1] if x.endswith(b"\n") else x for x in parent_lines[a0:a0 + an]], n)
    p_lno = 1
    for lno in range(cnt + 1):
        sl[lno].p_lno[n] = p_lno
        p_lno += sum(1 for ll in sl[lno].lost if ll.map & bit)
        if lno < cnt and not sl[lno].flag & bit:
            p_lno += 1
    sl[cnt + 1].p_lno[n] = p_lno


def _reuse(sl, cnt, i, j):
    imask, jmask = 1 << i, 1 << j
    for lno in range(cnt + 2):
        s = sl[lno]
        s.p_lno[i] = s.p_lno[j]
        for ll in s.lost:
            if ll.map & jmask:
                ll.map |= imask
        if s.flag & jmask:
            s.flag |= imask


def _interesting(s, all_mask):
    return bool(s.flag & all_mask) or bool(s.lost)


def _adjust_hunk_tail(sl, all_mask, hunk_begin, i):
    if hunk_begin + 1 <= i and not sl[i - 1].flag & all_mask:
        i -= 1
    return i


def _find_next(sl, mark, i, cnt, look_for_uninteresting):
    while i < cnt:
        if (not sl[i].flag & mark) if look_for_uninteresting else (sl[i].flag & mark):
            return i
        i += 1
    return cnt


def _give_context(sl, cnt, num_parent, context):
    all_mask = (1 << num_parent) - 1
    mark = 1 << num_parent
    no_pre_delete = 2 << num_parent
    i = _find_next(sl, mark, 0, cnt, False)
    if cnt <= i:
        return False
    while i < cnt:
        j = i - context if context < i else 0
        while j < i:
            if not sl[j].flag & mark:
                sl[j].flag |= no_pre_delete
            sl[j].flag |= mark
            j += 1
        while True:
            j = _find_next(sl, mark, i, cnt, True)
            if cnt <= j:
                return True
            k = _find_next(sl, mark, j, cnt, False)
            j = _adjust_hunk_tail(sl, all_mask, i, j)
            if k < j + context:
                while j < k:
                    sl[j].flag |= mark
                    j += 1
                i = k
                continue
            break
        i = k
        k = j + context if j + context < cnt + 1 else cnt + 1
        while j < k:
            sl[j].flag |= mark
            j += 1
    return True


def _make_hunks(sl, cnt, num_parent, dense, context):
    all_mask = (1 << num_parent) - 1
    mark = 1 << num_parent
    for i in range(cnt):
        if _interesting(sl[i], all_mask):
            sl[i].flag |= mark
        else:
            sl[i].flag &= ~mark
    if not dense:
        return _give_context(sl, cnt, num_parent, context)
    i = 0
    while i < cnt:
        while i < cnt and not sl[i].flag & mark:
            i += 1
        if cnt <= i:
            break
        hunk_begin = i
        j = i + 1
        while j < cnt:
            if not sl[j].flag & mark:
                la = _adjust_hunk_tail(sl, all_mask, hunk_begin, j)
                la = la + context if la + context < cnt else cnt
                contin = False
                while j <= la - 1:
                    la -= 1
                    if sl[la].flag & mark:
                        contin = True
                        break
                if not contin:
                    break
                j = la
            j += 1
        hunk_end = j
        same_diff = 0
        has_interesting = False
        j = i
        while j < hunk_end and not has_interesting:
            this_diff = sl[j].flag & all_mask
            if this_diff:
                if not same_diff:
                    same_diff = this_diff
                elif same_diff != this_diff:
                    has_interesting = True
                    break
            for ll in sl[j].lost:
                if has_interesting:
                    break
                this_diff = ll.map
                if not same_diff:
                    same_diff = this_diff
                elif same_diff != this_diff:
                    has_interesting = True
            j += 1
        if not has_interesting and same_diff != all_mask:
            for j in range(hunk_begin, hunk_end):
                sl[j].flag &= ~mark
        i = hunk_end
    return _give_context(sl, cnt, num_parent, context)


def _comment_line(text):
    return bool(text) and (text[:1].isalpha() or text[:1] in (b"_", b"$"))


def _dump(sl, cnt, num_parent):
    mark = 1 << num_parent
    no_pre_delete = 2 << num_parent
    out = []
    lno = 0
    while True:
        comment = None
        while lno <= cnt and not sl[lno].flag & mark:
            if _comment_line(sl[lno].text):
                comment = sl[lno].text
            lno += 1
        if cnt < lno:
            break
        hunk_end = lno + 1
        while hunk_end <= cnt and sl[hunk_end].flag & mark:
            hunk_end += 1
        rlines = hunk_end - lno
        if cnt < hunk_end:
            rlines -= 1
        head = b"@" * (num_parent + 1)
        for i in range(num_parent):
            head += b" -%d,%d" % (sl[lno].p_lno[i], sl[hunk_end].p_lno[i] - sl[lno].p_lno[i])
        head += b" +%d,%d " % (lno + 1, rlines) + b"@" * (num_parent + 1)
        if comment:
            end = 0
            for i, c in enumerate(comment[:40]):
                if c in b"\n\0":
                    break
                if not chr(c).isspace():
                    end = i                                    # git prints up to, not including, the last non-blank character
            if end:
                head += b" " + comment[:end]
        out.append(head + b"\n")
        while lno < hunk_end:
            s = sl[lno]
            lno += 1
            if not s.flag & no_pre_delete:
                for ll in s.lost:
                    out.append(b"".join(b"-" if ll.map & (1 << j) else b" " for j in range(num_parent)) + ll.text + b"\n")
            if cnt < lno:
                break
            out.append(b"".join(b"+" if s.flag & (1 << j) else b" " for j in range(num_parent)) + s.text + b"\n")
    return b"".join(out)


def combined_patch(path, result, parents, abbrev, dense=True, context=3, force_header=False):
    """The ``diff --cc`` text of one path, or ``b""`` when nothing is to be shown. ``result`` and every entry of ``parents`` are ``(mode, oid, data)``; ``mode`` 0 means the
    path does not exist there (``data`` may be a callable that reads the content)."""
    num_parent = len(parents)

    def data_of(side):
        d = side[2]
        return d() if callable(d) else (d if d is not None else b"")

    result_deleted = not result[0]
    result_data = b"" if result_deleted else data_of(result)
    mode_differs = any(p[0] != result[0] for p in parents)
    quoted = df.quote_path(path).encode()
    binary = df.is_binary(result_data) or any(p[0] and df.is_binary(data_of(p)) for p in parents)
    show_hunks = False
    sl = []
    cnt = 0
    if not binary:
        result_lines = [x[:-1] if x.endswith(b"\n") else x for x in _lines(result_data)]
        cnt = len(result_lines)
        sl = [_Line(result_lines[k] if k < cnt else b"", num_parent) for k in range(cnt + 2)]
        raw = _lines(result_data)
        for i, p in enumerate(parents):
            dup = next((j for j in range(i) if parents[j][1] == p[1] and parents[j][0] == p[0]), None)
            if dup is not None:
                _reuse(sl, cnt, i, dup)
                continue
            if p[0]:
                _combine_one(sl, raw, data_of(p), i, num_parent)
            else:
                _combine_one(sl, raw, b"", i, num_parent)
        show_hunks = _make_hunks(sl, cnt + 1, num_parent, dense, context)         # the extra line after the last holds the lines lost at the very end
    elif binary:
        show_hunks = True
    if not (show_hunks or mode_differs or force_header):
        return b""
    lines = [b"diff --%s %s\n" % (b"cc" if dense else b"combined", quoted)]
    lines.append(b"index " + b",".join(abbrev(p[1] if p[0] else NULL_OID).encode() for p in parents) + b".." + abbrev(result[1] if result[0] else NULL_OID).encode() + b"\n")
    added = deleted = False
    if mode_differs:
        deleted = not result[0]
        added = not deleted and all(not p[0] for p in parents)
        if added:
            lines.append(b"new file mode %06o\n" % result[0])
        else:
            text = b"deleted file " if deleted else b""
            text += b"mode " + b",".join(b"%06o" % p[0] for p in parents)
            if result[0]:
                text += b"..%06o" % result[0]
            lines.append(text + b"\n")
    if binary:
        lines.append(b"Binary files differ\n")
        return b"".join(lines)
    lines.append(b"--- " + (b"/dev/null" if added else df.quote_path("a/" + path).encode()) + b"\n")
    lines.append(b"+++ " + (b"/dev/null" if deleted else df.quote_path("b/" + path).encode()) + b"\n")
    if show_hunks and not (force_header and result_deleted):                     # a work tree file that is gone shows no hunks, a deleted path in a commit does
        lines.append(_dump(sl, cnt, num_parent))
    return b"".join(lines)
