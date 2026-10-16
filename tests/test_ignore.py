import os
import random
import stat

import pytest

import gitoracle as go
from kendi_git.config import Config
from kendi_git.ignore import IgnoreRules, Pattern, parse_patterns, translate

pytestmark = pytest.mark.skipif(go.GIT is None, reason="real git is not installed")


def walk(root):
    """Every file and directory below ``root`` except ``.git``: ``[(relpath, is_dir)]`` (symlinks count as files, as lstat sees them)."""
    out = []
    for cur, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d != ".git")
        for name in dirs + sorted(files):
            full = os.path.join(cur, name)
            out.append((os.path.relpath(full, root).replace(os.sep, "/"), stat.S_ISDIR(os.lstat(full).st_mode)))
    return out


def git_view(repo):
    others = set(go.gits(repo, "ls-files", "-o", "--exclude-standard", "-z").split("\x00")) - {""}
    ignored = set(go.gits(repo, "ls-files", "-o", "-i", "--exclude-standard", "-z").split("\x00")) - {""}
    return others, ignored


def our_view(repo, rules):
    others, ignored = set(), set()
    for rel, is_dir in walk(repo):
        if is_dir:
            continue
        (ignored if rules.is_ignored(rel, False) else others).add(rel)
    return others, ignored


def make_rules(repo, **config):
    for key, value in config.items():
        go.git(repo, "config", key.replace("_", "."), value)
    cfg = Config(os.path.join(repo, ".git"), env={"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "HOME": repo})
    return IgnoreRules(repo, os.path.join(repo, ".git"), cfg, env={"HOME": repo})


def init(tmp_path, name, ignorecase="false"):
    repo = str(tmp_path / name)
    os.makedirs(repo)
    go.git(repo, "init", "-q")
    go.git(repo, "config", "core.ignorecase", ignorecase)
    return repo


# --- hand-written cases ----------------------------------------------------------------------------------------------------------------------------
CASES = [
    ("simple extension", "*.log\n", ["a.log", "d/b.log", "d/e/c.log", "a.txt", "log"]),
    ("anchored", "/top.txt\n", ["top.txt", "d/top.txt"]),
    ("middle slash anchors", "d/x.txt\n", ["d/x.txt", "e/d/x.txt", "d/e/x.txt"]),
    ("directory only", "build/\n", ["build/o", "src/build/o", "build.txt", "x/build/y/z"]),
    ("negation", "*.log\n!keep.log\n", ["a.log", "keep.log", "d/keep.log", "d/x.log"]),
    ("no re-inclusion below an excluded directory", "out/\n!out/keep.txt\n", ["out/keep.txt", "out/other", "keep.txt"]),
    ("re-inclusion works when only the files are excluded", "out/*\n!out/keep.txt\n", ["out/keep.txt", "out/other", "out/d/e"]),
    ("double star prefix", "**/gen\n", ["gen/a", "x/gen/a", "x/y/gen/a", "gen.txt"]),
    ("double star suffix", "data/**\n", ["data/a", "data/x/y", "dat", "other/data/a"]),
    ("double star middle", "a/**/z\n", ["a/z", "a/b/z", "a/b/c/z", "b/a/z", "a/zz"]),
    ("double star glued to text acts as a star", "a**b\n", ["ab", "axxb", "a/b", "x/axb"]),
    ("character classes", "[ab].txt\n[!a-c]x\n[[:digit:]]y\n", ["a.txt", "b.txt", "c.txt", "dx", "ax", "1y", "ay"]),
    ("question mark never crosses a slash", "a?b\n", ["a/b", "axb", "aab"]),
    ("comment and escapes", "# comment\n\\#hash\n\\!bang\n", ["#hash", "!bang", "comment"]),
    ("trailing spaces", "trail  \nkeep\\ \n", ["trail", "trail  ", "keep ", "keep"]),
    ("blank lines and a BOM", "﻿\n\n*.tmp\n\n", ["a.tmp", "b"]),
    ("windows line endings are dropped from patterns", "*.bak\r\n*.tab\t\n", ["a.bak", "b.bak\r", "c.tab", "d.tab\t"]),
    ("unterminated class never matches", "[abc\n", ["a", "[abc", "b"]),
    ("only a slash", "/\n", ["a"]),
    ("star star alone", "**\n!keep\n", ["a", "d/b", "keep"]),
]


@pytest.mark.parametrize("title,rules_text,files", CASES, ids=[c[0] for c in CASES])
def test_hand_written_cases_match_git(tmp_path, title, rules_text, files):
    repo = init(tmp_path, "r")
    go.write(repo, ".gitignore", rules_text.encode("utf-8"))
    for f in files:
        go.write(repo, f, "x")
    rules = make_rules(repo)
    assert our_view(repo, rules) == git_view(repo), title


def test_nested_files_override_their_parents_and_info_exclude_is_weakest(tmp_path):
    repo = init(tmp_path, "r")
    go.write(repo, ".gitignore", "*.dat\n")
    go.write(repo, "sub/.gitignore", "!keep.dat\n*.tmp\n")
    go.write(repo, "sub/deep/.gitignore", "!*.tmp\n/only-here\n")
    go.write(repo, ".git/info/exclude", "*.secret\n!sub/ok.secret\n*.tmp\n")
    for f in ["a.dat", "sub/a.dat", "sub/keep.dat", "sub/x.tmp", "sub/deep/y.tmp", "sub/deep/only-here", "sub/only-here", "a.secret", "sub/ok.secret", "z.tmp", "sub/deep/z.dat"]:
        go.write(repo, f, "x")
    assert our_view(repo, make_rules(repo)) == git_view(repo)


def test_global_excludes_file_and_core_excludesfile(tmp_path):
    repo = init(tmp_path, "r")
    glob = tmp_path / "globalignore"
    glob.write_text("*.glob\n!keep.glob\n")
    go.git(repo, "config", "core.excludesFile", str(glob))
    go.write(repo, ".gitignore", "*.local\n")
    for f in ["a.glob", "keep.glob", "a.local", "plain"]:
        go.write(repo, f, "x")
    assert our_view(repo, make_rules(repo)) == git_view(repo)
    assert our_view(repo, make_rules(repo))[1] == {"a.glob", "a.local"}


def test_a_symlinked_gitignore_is_not_followed(tmp_path):
    repo = init(tmp_path, "r")
    go.write(repo, "real-rules", "*.zap\n")
    os.symlink("real-rules", os.path.join(repo, ".gitignore"))
    go.write(repo, "a.zap", "x")
    assert our_view(repo, make_rules(repo)) == git_view(repo) and "a.zap" in git_view(repo)[0]


def test_case_insensitive_matching_when_configured(tmp_path):
    repo = init(tmp_path, "r", ignorecase="true")
    go.write(repo, ".gitignore", "*.TXT\nBuild/\n")
    for f in ["a.txt", "b.Txt", "c.log", "build/x", "BUILD/y".lower() + "z"]:
        go.write(repo, f, "x")
    assert our_view(repo, make_rules(repo)) == git_view(repo)
    assert "a.txt" in git_view(repo)[1]


# --- translation unit checks -----------------------------------------------------------------------------------------------------------------------
def test_pattern_parsing_rules():
    p = Pattern("!/foo/bar/")
    assert (p.negated, p.dir_only, p.anchored) == (True, True, True)
    assert Pattern("foo").anchored is False and Pattern("a/b").anchored is True and Pattern("**/x").anchored is True
    assert [q.text for q in parse_patterns("a\n#c\n\nb  \n")] == ["a", "b"]
    assert translate("[abc") is None and translate("x\\") is None and translate("[[:nope:]]") is None


# --- randomised comparison with git ----------------------------------------------------------------------------------------------------------------
NAMES = ["a", "b", "ab", "build", "tmp", "x.d", "Keep", "keep.txt", "a.txt", "b.log", "c.o", ".hidden", "sp ace", "ünï", "z"]
ATOMS = ["a", "b", "ab", "*", "?", "**", "[ab]", "[!a]", "[a-c]", "build", "tmp", "x.d", ".txt", ".log", "keep", "*.o", "k*", "*d", "a*b", "[[:alpha:]]", "\\*", "ünï"]


def random_pattern(rng):
    parts = []
    for _ in range(rng.randint(1, 3)):
        parts.append("".join(rng.choice(ATOMS) for _ in range(rng.randint(1, 2))))
    pat = "/".join(parts) if rng.random() < 0.5 else parts[0]
    if rng.random() < 0.25:
        pat = "**/" + pat
    if rng.random() < 0.15:
        pat = pat + "/**"
    if rng.random() < 0.25:
        pat = "/" + pat
    if rng.random() < 0.3:
        pat += "/"
    if rng.random() < 0.25:
        pat = "!" + pat
    return pat


def random_tree(rng):
    files = set()
    for _ in range(rng.randint(8, 26)):
        depth = rng.randint(0, 3)
        path = [rng.choice(NAMES) for _ in range(depth)] + [rng.choice(NAMES)]
        files.add("/".join(path))
    # a path cannot be both a file and a directory
    clean = []
    for f in sorted(files):
        if not any(g != f and g.startswith(f + "/") for g in files) and not any(f.startswith(g + "/") for g in clean):
            clean.append(f)
    return clean


@pytest.mark.parametrize("seed", range(int(os.environ.get("KGIT_FUZZ_SEEDS", "5"))))
def test_random_scenarios_agree_with_git(tmp_path, seed):
    rng = random.Random(1000 + seed)
    for n in range(14):
        repo = init(tmp_path, f"r{seed}_{n}")
        files = random_tree(rng)
        dirs = {""} | {os.path.dirname(f) for f in files}
        dirs |= {d for f in files for d in [os.path.dirname(f)] for d in [d] if d}
        parents = set()
        for d in dirs:
            while d:
                parents.add(d)
                d = os.path.dirname(d)
        for d in sorted(parents | {""}):
            if rng.random() < 0.6:
                text = "\n".join(random_pattern(rng) for _ in range(rng.randint(1, 5))) + "\n"
                if d and any(f == (d + "/.gitignore") for f in files):
                    continue
                go.write(repo, (d + "/" if d else "") + ".gitignore", text)
        for f in files:
            full = os.path.join(repo, f)
            if os.path.isdir(full):
                continue
            go.write(repo, f, "x")
        rules = make_rules(repo)
        theirs, ours_ = git_view(repo), our_view(repo, rules)
        if ours_ != theirs:
            gi = {r: open(os.path.join(repo, r)).read() for r, _ in walk(repo) if r.endswith(".gitignore")}
            raise AssertionError(f"disagreement in {repo}\ngitignore files: {gi}\nonly we ignore: {sorted(theirs[0] & ours_[1])}\nonly git ignores: {sorted(ours_[0] & theirs[1])}")


def test_check_ignore_agrees_for_files_and_directories(tmp_path):
    rng = random.Random(77)
    for n in range(10):
        repo = init(tmp_path, f"c{n}")
        go.write(repo, ".gitignore", "\n".join(random_pattern(rng) for _ in range(6)) + "\n")
        for f in random_tree(rng):
            go.write(repo, f, "x")
        paths = [rel for rel, _ in walk(repo)]
        res = go.git(repo, "check-ignore", "--no-index", "-z", "--stdin", input=("\x00".join(paths) + "\x00").encode(), check=False)
        theirs = {p for p in res.stdout.decode().split("\x00") if p}
        rules = make_rules(repo)
        mine = {rel for rel, is_dir in walk(repo) if rules.is_ignored(rel, is_dir)}
        assert mine == theirs, (n, open(os.path.join(repo, ".gitignore")).read(), sorted(mine ^ theirs))
