import os
import random
import time

import pytest

import gitoracle as go
from kendi_git import diff as df
from kendi_git.objects import MODE_FILE


def lcs_length(a, b):
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0]
        for j, y in enumerate(b):
            cur.append(prev[j] + 1 if x == y else max(prev[j + 1], cur[j]))
        prev = cur
    return prev[-1]


def check_valid(a, b):
    """Whatever the heuristics did, what remains of both sides must be the same text."""
    removed, added = df.myers(a, b)
    assert len(removed) == len(a) and len(added) == len(b)
    assert [x for x, r in zip(a, removed) if not r] == [y for y, ad in zip(b, added) if not ad]
    return removed, added


def test_the_edit_script_is_always_valid_and_usually_shortest():
    """Like git's xdiff (of which it is a port) the search discards lines that match nothing, or too much, in the other file before it starts, so a script is not
    always the shortest: for inputs where the heuristics stay out of the way (few repeats, at least 16 lines) it must be exactly the shortest."""
    rng = random.Random(1)
    for _ in range(1500):
        alphabet = "ab" if rng.random() < 0.4 else "abcdefg"
        a = [rng.choice(alphabet) for _ in range(rng.randint(0, 14))]
        b = [rng.choice(alphabet) for _ in range(rng.randint(0, 14))]
        check_valid(a, b)
    for _ in range(300):
        pool = ["line %d" % k for k in range(60)]
        a = [rng.choice(pool) for _ in range(rng.randint(16, 40))]
        b = list(a)
        for _ in range(rng.randint(0, 6)):
            pos = rng.randint(0, len(b))
            b[pos:pos + rng.randint(0, 3)] = [rng.choice(pool) for _ in range(rng.randint(0, 3))]
        removed, added = check_valid(a, b)
        assert sum(removed) + sum(added) == len(a) + len(b) - 2 * lcs_length(a, b), (a, b)


def test_myers_edge_cases():
    for a, b in [([], []), ([], ["x"]), (["x"], []), (["x"], ["x"]), (["a", "b"], ["b", "a"]), (["a"] * 5, ["a"] * 3), (list("abcabba"), list("cbabac")),
                 (list("aaaa"), list("bbbb")), (list("abc"), list("abc") * 3)]:
        check_valid(a, b)
    assert df.myers([], []) == ([], [])
    assert df.myers(["x"], ["x"]) == ([False], [False])
    assert df.myers([], ["x", "y"]) == ([], [True, True])


def test_myers_on_larger_inputs_is_fast_and_correct():
    rng = random.Random(2)
    base = ["line %d\n" % rng.randrange(400) for _ in range(6000)]
    edited = list(base)
    for _ in range(300):
        pos = rng.randrange(len(edited))
        action = rng.random()
        if action < 0.4:
            del edited[pos]
        elif action < 0.8:
            edited.insert(pos, "new %d\n" % rng.randrange(10**6))
        else:
            edited[pos] = "changed %d\n" % rng.randrange(10**6)
    started = time.time()
    removed, added = df.myers(base, edited)
    assert time.time() - started < 20
    assert [x for x, r in zip(base, removed) if not r] == [y for y, ad in zip(edited, added) if not ad]
    assert 0 < sum(removed) + sum(added) <= 900                                  # roughly one removal and one addition per edit
    a = ["x%d\n" % i for i in range(1500)]
    b = ["y%d\n" % i for i in range(1500)]
    assert df.myers(a, b) == ([True] * 1500, [True] * 1500)                      # nothing in common: the worst case, linear memory


def test_split_lines_keeps_endings_and_handles_missing_final_newline():
    assert df.split_lines(b"a\nb\n") == [b"a\n", b"b\n"]
    assert df.split_lines(b"a\nb") == [b"a\n", b"b"]
    assert df.split_lines(b"") == [] and df.split_lines(b"\n") == [b"\n"]
    assert df.split_lines(b"a\r\nb\rc\n") == [b"a\r\n", b"b\rc\n"]


def test_hunk_headers_and_context_merging():
    a = ["l%d\n" % i for i in range(30)]
    b = list(a)
    b[2] = "X\n"
    b[9] = "Y\n"            # 6 unchanged lines between the changes: still one hunk (2 * 3)
    b[17] = "Z\n"           # 7 unchanged lines after Y: a second hunk
    text = df.format_hunks([l.encode() for l in a], [l.encode() for l in b]).decode()
    assert text.count("@@ -") == 2
    assert text.startswith("@@ -1,13 +1,13 @@\n") and "\n@@ -15,7 +15,7 @@ l13\n" in text
    one = df.format_hunks([b"a\n"], [b"b\n"]).decode()
    assert one.startswith("@@ -1 +1 @@\n")
    assert df.format_hunks([], [b"x\n", b"y\n"]).startswith(b"@@ -0,0 +1,2 @@")
    assert df.format_hunks([b"x\n"], []).startswith(b"@@ -1 +0,0 @@")
    assert df.format_hunks([b"same\n"], [b"same\n"]) == b""


def test_function_context_follows_git_rules():
    a = [b"def top():\n", b"    x = 1\n", b"    y = 2\n"] + [b"    pad\n"] * 10 + [b"tail\n"]
    b = list(a)
    b[-1] = b"changed\n"
    assert df.format_hunks(a, b).startswith(b"@@ -11,4 +11,4 @@ def top():\n")       # indented lines and blank lines are skipped, the first column-0 word wins
    c = [b"\tindented\n"] * 10 + [b"x\n"]
    d = [b"\tindented\n"] * 10 + [b"y\n"]
    assert df.format_hunks(c, d).startswith(b"@@ -8,4 +8,4 @@\n")             # no candidate line above: no text


def test_quote_path_follows_core_quotepath():
    assert df.quote_path("plain/file.txt") == "plain/file.txt"
    assert df.quote_path("with space") == "with space"
    assert df.quote_path("ünï") == '"\\303\\274n\\303\\257"'
    assert df.quote_path('qu"ote') == '"qu\\"ote"' and df.quote_path("tab\there") == '"tab\\there"' and df.quote_path("back\\slash") == '"back\\\\slash"'


# --- patches must be accepted by git apply and reproduce the new file --------------------------------------------------------------------------
pytestmark_git = pytest.mark.skipif(go.GIT is None, reason="real git is not installed")


def random_text(rng, n, alphabet_size=6):
    lines = ["row %d\n" % rng.randrange(alphabet_size) if rng.random() < 0.7 else "\n" for _ in range(n)]
    if lines and rng.random() < 0.3:
        lines[-1] = lines[-1].rstrip("\n")                                          # no newline at the end of the file
    return "".join(lines).encode()


def mutate(rng, data):
    lines = df.split_lines(data)
    for _ in range(rng.randint(0, 9)):
        pos = rng.randint(0, len(lines))
        kind = rng.random()
        if kind < 0.35 and lines:
            del lines[min(pos, len(lines) - 1)]
        elif kind < 0.7:
            lines.insert(pos, b"ins %d\n" % rng.randrange(5))
        elif lines:
            lines[min(pos, len(lines) - 1)] = b"row %d\n" % rng.randrange(6)
    out = b"".join(lines)
    if rng.random() < 0.2 and out:
        out = out.rstrip(b"\n")
    return out


@pytestmark_git
def test_our_patches_apply_with_git_and_have_minimal_size(tmp_path):
    rng = random.Random(31)
    repo = str(tmp_path / "r")
    os.makedirs(repo)
    go.git(repo, "init", "-q")
    for n in range(60):
        old = random_text(rng, rng.randint(0, 40))
        new = mutate(rng, old)
        old_side = df.file_side(MODE_FILE, df.hash_data(old), old) if old or rng.random() < 0.5 else None
        if old_side is None:
            old = b""
        new_side = df.file_side(MODE_FILE, df.hash_data(new), new)
        patch = df.render_change("f.txt", old_side, new_side, lambda oid: oid[:7])
        go.write(repo, "f.txt", old if old_side else b"")
        if not old_side:
            os.unlink(os.path.join(repo, "f.txt"))
        if old == new and old_side is not None:
            assert patch == b"diff --git a/f.txt b/f.txt\n"                        # nothing differs: a header and no content part
            continue
        go.git(repo, "apply", "--check", input=patch)
        go.git(repo, "apply", input=patch)
        assert open(os.path.join(repo, "f.txt"), "rb").read() == new, (n, patch)
        # the number of changed lines equals what git's own (--minimal) diff reports
        if old != new and old_side and new:
            a, b = os.path.join(str(tmp_path), "a"), os.path.join(str(tmp_path), "b")
            open(a, "wb").write(old)
            open(b, "wb").write(new)
            numstat = go.git(str(tmp_path), "diff", "--no-index", "--minimal", "--numstat", a, b, check=False).stdout.decode()
            added, removed = df.counts(old_side, new_side)
            assert numstat.split()[:2] == [str(added), str(removed)], (n, numstat, added, removed)
        os.unlink(os.path.join(repo, "f.txt"))


# --- patches of repetitive, code-like text against git diff ---------------------------------------------------------------------------------------
VOCAB = ["def f(x):\n", "    if x:\n", "        return 1\n", "    else:\n", "        return 2\n", "\n", "\n", "}\n", "{\n", "    pass\n", "class K:\n", "    def g(self):\n",
         "        self.a = 1\n", "# comment\n", "import os\n", "    return x\n", "\treturn y\n", "  \n", "end\n"]


def code_like(rng, n):
    return [rng.choice(VOCAB) for _ in range(n)]


def edit_code(rng, lines):
    lines = list(lines)
    for _ in range(rng.randint(1, 5)):
        action = rng.random()
        pos = rng.randint(0, len(lines))
        if action < 0.45:
            block = [rng.choice(VOCAB) for _ in range(rng.randint(1, 6))] if rng.random() < 0.5 else lines[max(0, pos - 3):pos]    # new or copied lines
            lines[pos:pos] = block
        elif action < 0.75:
            del lines[pos:pos + rng.randint(1, 5)]
        elif lines:
            lines[min(pos, len(lines) - 1)] = rng.choice(VOCAB)
    return lines


@pytest.mark.skipif(go.GIT is None, reason="real git is not installed")
def test_patches_of_repetitive_code_match_git_most_of_the_time(tmp_path):
    """Many equally good edit scripts exist for such text; git's xdiff picks one with its own search, record discarding, change compaction and indent heuristic. This
    program is a port of all of those steps, so the patches are byte-identical; the rate is asserted so that a regression in any step shows up."""
    rng = random.Random(77)
    total = same = same_counts = 0
    pa, pb = str(tmp_path / "a.txt"), str(tmp_path / "b.txt")
    for _ in range(300):
        old = code_like(rng, rng.randint(8, 60))
        new = edit_code(rng, old)
        if old == new:
            continue
        open(pa, "wb").write("".join(old).encode())
        open(pb, "wb").write("".join(new).encode())
        theirs = go.git(str(tmp_path), "diff", "--no-index", "--no-color", pa, pb, check=False).stdout
        theirs = theirs[theirs.index(b"@@"):] if b"@@" in theirs else b""
        mine = df.format_hunks([x.encode() for x in old], [x.encode() for x in new])
        total += 1
        same += mine == theirs
        changed = lambda text, mark: sum(1 for ln in text.split(b"\n") if ln.startswith(mark) and not ln.startswith(mark * 3))  # noqa: E731
        same_counts += (changed(mine, b"+"), changed(mine, b"-")) == (changed(theirs, b"+"), changed(theirs, b"-"))
    print(f"byte-identical: {same}/{total}, same added/removed counts: {same_counts}/{total}")
    assert same_counts / total >= 0.99
    assert same / total >= 0.99, (same, total)
