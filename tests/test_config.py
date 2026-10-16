import os
import random
import string
import time

import pytest

import gitoracle as go
from kendi_git.config import Config, ConfigFile, identity, parse_date
from kendi_git.errors import GitError

pytestmark = pytest.mark.skipif(go.GIT is None, reason="real git is not installed")


def git_list(path):
    """git's own parse of a config file: ``[(key, value-or-None)]`` in file order."""
    out = []
    for rec in go.git(os.path.dirname(path) or ".", "config", "--file", path, "--list", "-z").split(b"\x00"):
        if rec:
            key, sep, value = rec.partition(b"\n")
            out.append((key.decode(), value.decode() if sep else None))
    return out


def ours(path):
    return list(ConfigFile(path).entries())


TRICKY = r'''# a comment
; another comment
[core]
	bare = false
	filemode=true
	Mixed-Case = Value
	flag
	quoted = "  keeps  leading and trailing  "
	mixed = unquoted   words   "and quoted"   tail   # trailing comment
	escapes = "tab\there\nnewline \"quoted\" back\\slash"
	continued = first \
second \
third
	semi = a;b
	hash = "a#b;c"
[remote "origin"]
	url = https://example.com/a b.git
	fetch = +refs/heads/*:refs/remotes/origin/*
	fetch = +refs/tags/*:refs/tags/*
[Branch "Feature.With.Dots"]
	merge = refs/heads/x
[section "with \"escaped\" quote and \\ backslash"]
	key = v
[legacy.SubName]
	k = legacy
[a] b = inline
[a]
	c = after
	empty =
	spaces =    
	emptyquote = ""
	trail = x ""
'''


def test_a_file_full_of_edge_cases_parses_exactly_like_git(tmp_path):
    path = str(tmp_path / "config")
    open(path, "w").write(TRICKY)
    assert ours(path) == git_list(path) and len(ours(path)) > 18
    assert ConfigFile(path).get_all("remote.origin.fetch") == ["+refs/heads/*:refs/remotes/origin/*", "+refs/tags/*:refs/tags/*"]
    assert ConfigFile(path).get_all("CORE.mixed-case") == ["Value"]
    assert ConfigFile(path).get_all("core.flag") == [None]


@pytest.mark.parametrize("broken", [
    "[core]\n\tname = \"unterminated\n", "[core]\n\tname = bad\\qescape\n", "[core\nname = x\n", "[core]\n\t= novalue\n"
])
def test_files_git_rejects_are_rejected_here_too(tmp_path, broken):
    path = str(tmp_path / "config")
    open(path, "w").write(broken)
    assert go.git(str(tmp_path), "config", "--file", path, "--list", check=False).returncode != 0
    with pytest.raises(GitError):
        ConfigFile(path)


def test_a_continued_last_line_ends_the_value_like_in_git(tmp_path):
    path = str(tmp_path / "config")
    open(path, "w").write("[core]\n\tname = x\\\n")
    assert ours(path) == git_list(path) == [("core.name", "x")]
    open(path, "w").write("[core]\n\tname = \"x\\\n")
    assert go.git(str(tmp_path), "config", "--file", path, "--list", check=False).returncode != 0
    with pytest.raises(GitError):
        ConfigFile(path)


def test_a_variable_before_any_section_is_refused_although_git_tolerates_it(tmp_path):
    """Found by the oracle: ``git config --list`` prints such a line as a section-less key that can never be queried. Refusing it is a deliberate, documented strictness."""
    path = str(tmp_path / "config")
    open(path, "w").write("name = outside a section\n")
    assert go.git(str(tmp_path), "config", "--file", path, "--list", check=False).returncode == 0
    with pytest.raises(GitError):
        ConfigFile(path)


def random_value(rng):
    alphabet = string.ascii_letters + string.digits + " \t#;\"\\=[]é\n<>'"
    return "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 24)))


def last_value_according_to_git(tmp_path, path, key):
    out = go.git(str(tmp_path), "config", "--file", path, "-z", "--get", key)
    return out[:-1].decode()


def test_values_written_by_git_come_back_unchanged_through_our_parser(tmp_path):
    rng = random.Random(11)
    path = str(tmp_path / "config")
    open(path, "w").close()
    for n in range(120):
        key = rng.choice(["a.b", "core.x%d" % n, "remote.o r.u%d" % (n % 5), "s.sub\"q.k%d" % (n % 7)])
        value = random_value(rng)
        go.git(str(tmp_path), "config", "--file", path, key, value)
    assert ours(path) == git_list(path) and len(ours(path)) > 30
    cfg = ConfigFile(path)
    for key in {k for k, _ in git_list(path)}:                                    # git's own answer for every key, key by key
        assert cfg.get_all(key)[-1] == last_value_according_to_git(tmp_path, path, key)


def test_values_written_by_us_are_read_back_by_git(tmp_path):
    rng = random.Random(12)
    path = str(tmp_path / "config")
    cfg = ConfigFile(path)
    last = {}
    for n in range(120):
        key = rng.choice(["user.name", "core.x%d" % (n % 9), "remote.or igin.url%d" % (n % 4), "branch.main.merge", "Alias.Co-%d" % (n % 3)])
        value = random_value(rng)
        cfg.set(key, value)
        last[key] = value
        if n % 20 == 19:
            cfg.save()
            assert git_list(path) == ours(path)
    cfg.save()
    listing = git_list(path)
    assert listing == ours(path)
    assert len(listing) == len(last)                                                  # set replaces, never duplicates
    for key, value in last.items():
        flat = ".".join(part for part in ConfigFile.split_key(key) if part is not None)
        assert last_value_according_to_git(tmp_path, path, flat) == value                  # what we wrote is exactly what git reads
        assert ConfigFile(path).get_all(key)[-1] == last_value_according_to_git(tmp_path, path, flat)


def test_layers_and_booleans(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[user]\n\tname = Global\n\temail = g@x\n[color]\n\tui\n[core]\n\tflag = off\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    go.git(str(repo), "init", "-q")
    (repo / ".git" / "config").write_text("[user]\n\tname = Local\n")
    env = {"HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1"}
    cfg = Config(str(repo / ".git"), env=env)
    assert cfg.get("user.name") == "Local" and cfg.get("user.email") == "g@x" and cfg.get("nope.x", "d") == "d"
    assert cfg.get_bool("color.ui") is True and cfg.get_bool("core.flag") is False and cfg.get_bool("core.missing", True) is True
    isolated = Config(str(repo / ".git"), env={"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(home)})
    assert isolated.get("user.email") is None and isolated.get("user.name") == "Local"
    real = go.gits(str(repo), "config", "--get", "user.name", extra={"HOME": str(home), "GIT_CONFIG_GLOBAL": str(home / ".gitconfig")}).strip()
    assert real == "Local"                                                               # git layers the files the same way
    assert go.gits(str(repo), "config", "--get", "user.email", extra={"HOME": str(home), "GIT_CONFIG_GLOBAL": str(home / ".gitconfig")}).strip() == "g@x"


def test_editing_leaves_the_rest_of_the_file_alone(tmp_path):
    path = str(tmp_path / "config")
    open(path, "w").write("# header\n[core]\n\tbare = false  # keep me\n[user]\n\tname = Old\n\temail = o@x\n[core]\n\tlast = 1\n")
    cfg = ConfigFile(path)
    cfg.set("user.name", "New Name")
    cfg.set("core.added", "yes")
    cfg.set("alias.st", "status -s")
    cfg.save()
    text = open(path).read()
    assert "# header" in text and "bare = false  # keep me" in text and "email = o@x" in text
    assert git_list(path) == ours(path)
    assert go.gits(str(tmp_path), "config", "--file", path, "--get", "user.name").strip() == "New Name"
    assert go.gits(str(tmp_path), "config", "--file", path, "--get", "alias.st").strip() == "status -s"


# --- dates and identities -------------------------------------------------------------------------------------------------------------------------
def commit_tree_with(repo, committer_date=None, author_name=None, author_email=None, committer_name=None, committer_email=None):
    tree = go.gits(repo, "hash-object", "-w", "-t", "tree", "--stdin", input=b"").strip()
    extra = {}
    for var, val in (("GIT_COMMITTER_DATE", committer_date), ("GIT_AUTHOR_NAME", author_name), ("GIT_AUTHOR_EMAIL", author_email),
                     ("GIT_COMMITTER_NAME", committer_name), ("GIT_COMMITTER_EMAIL", committer_email)):
        if val is not None:
            extra[var] = val
    oid = go.gits(repo, "commit-tree", tree, "-m", "m", extra=extra).strip()
    headers = go.gits(repo, "cat-file", "commit", oid).split("\n\n")[0].split("\n")
    return {h.split(" ", 1)[0]: h.split(" ", 1)[1] for h in headers if h.startswith(("author", "committer"))}


DATES = ["1700000000 +0100", "1700000000 -0530", "@1700000000 +0200", "2023-11-14T22:13:20+00:00", "2023-11-14 22:13:20 +0100", "2023-11-14T22:13:20Z",
         "2023-11-14T22:13:20+05:30", "Tue, 14 Nov 2023 22:13:20 +0100", "14 Nov 2023 22:13:20 -0800", "2001-09-09 01:46:40 +0000", "1000000000 +0000",
         # forms found by probing real git: git's raw-number rules and its leniency about the zone
         "@1700000000", "1700000000", "42557065 +0530", "@42557065 +0530", "@42557065", "123456789 +0000", "99999999 +0000", "1700000000 0100", "1700000000 +01:00",
         "2023-11-14", "2023-11-14 22:13", "2023-11-14T22:13:20.5Z", "  1700000000 +0100  ", "1700000000 +0100 extra", "yesterday", "12"]


@pytest.fixture
def utc(monkeypatch):
    """The oracle runs in UTC; forms without a zone take the local one, so compare in UTC."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.mark.parametrize("text", DATES)
def test_date_formats_agree_with_git(tmp_path, text, utc):
    repo = str(tmp_path / "r")
    os.makedirs(repo)
    go.git(repo, "init", "-q")
    tree = go.gits(repo, "hash-object", "-w", "-t", "tree", "--stdin", input=b"").strip()
    proc = go.git(repo, "commit-tree", tree, "-m", "m", extra={"GIT_COMMITTER_DATE": text}, check=False)
    if proc.returncode != 0:                                            # git refuses the text: so must we
        with pytest.raises(GitError):
            parse_date(text)
        return
    oid = proc.stdout.decode().strip()
    theirs = [ln for ln in go.gits(repo, "cat-file", "commit", oid).splitlines() if ln.startswith("committer")][0]
    when, tz = parse_date(text)
    assert theirs.endswith(f" {when} {tz}"), (text, theirs, when, tz)


def test_dates_without_a_zone_use_the_local_zone(tmp_path, monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Kolkata")
    time.tzset()
    try:
        assert parse_date("2023-11-14 22:13:20") == (1700000000 - 19800, "+0530")           # 22:13:20 in India is 16:43:20 UTC
        assert parse_date("1700000000") == (1700000000, "+0530")
    finally:
        monkeypatch.undo()
        time.tzset()
    ident = identity("author", Config(None, env={"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}),
                     env={"GIT_AUTHOR_NAME": "N", "GIT_AUTHOR_EMAIL": "n@x", "GIT_AUTHOR_DATE": ""}, now=1700000000)
    assert ident.time == 1700000000                                                     # an empty date variable means "now", as in git


NAMES = ["Ada Lovelace", "  padded  ", "dot.", ".dot", "a<b>c", "'quoted'", '"dq"', "tab\there", "x,", ":y;", "back\\slash", "ünï çödé", "multi\nline", "Zoë"]


@pytest.mark.parametrize("name", NAMES)
def test_identity_cleaning_matches_git(tmp_path, name):
    repo = str(tmp_path / "r")
    os.makedirs(repo)
    go.git(repo, "init", "-q")
    email = "e" + name.replace(" ", "") + "@x.org"
    p = go.git(repo, "commit-tree", "4b825dc642cb6eb9a060e54bf8d69288fbee4904", "-m", "m", check=False,
               extra={"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email})
    cfg = Config(None, env={"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"})
    env = {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_AUTHOR_DATE": "1700000000 +0000"}
    if p.returncode != 0:                                                                     # git refuses (e.g. nothing left of the name)
        with pytest.raises(GitError):
            identity("author", cfg, env=env)
        return
    oid = p.stdout.decode().strip()
    line = go.gits(repo, "cat-file", "commit", oid).split("\n")[1 if go.gits(repo, "cat-file", "commit", oid).startswith("tree") else 0]
    theirs = [ln for ln in go.gits(repo, "cat-file", "commit", oid).split("\n") if ln.startswith("author ")][0][len("author "):]
    mine = identity("author", cfg, env=env)
    assert (mine.name, mine.email) == (theirs.split(" <")[0], theirs.split(" <")[1].split(">")[0]), (name, theirs, line)


def test_missing_identity_is_an_error_naming_the_fix():
    cfg = Config(None, env={"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"})
    with pytest.raises(GitError) as e:
        identity("committer", cfg, env={})
    assert "user.name" in str(e.value)
    ident = identity("committer", cfg, env={"GIT_COMMITTER_NAME": "N", "GIT_COMMITTER_EMAIL": "n@x"}, now=1700000000)
    assert (ident.name, ident.email, ident.time) == ("N", "n@x", 1700000000)
