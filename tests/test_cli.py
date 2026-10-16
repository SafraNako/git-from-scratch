"""The command line, driven in-process, against real git's output for the same repository state."""
import io
import os
import random
import shutil

import pytest

import gitoracle as go
from kendi_git import cli
from twin import edit_text

pytestmark = pytest.mark.skipif(go.GIT is None, reason="real git is not installed")


def kgit(repo, *argv, stdin=None, env=None):
    out, err = io.BytesIO(), io.BytesIO()
    code = cli.main(list(argv), stdout=out, stderr=err, cwd=repo, env={**go.env(), **(env or {})}, stdin=io.BytesIO(stdin) if stdin is not None else None, now=lambda: 1800000000)
    return code, out.getvalue(), err.getvalue().decode()


@pytest.fixture
def copy(history_repo, tmp_path):
    path = str(tmp_path / "copy")
    shutil.copytree(history_repo, path, symlinks=True)
    return path


def same_as_git(repo, ours, *git_args):
    code, out, err = kgit(repo, *ours)
    theirs = go.git(repo, *git_args)
    assert code == 0, err
    assert out == theirs, (ours, out[:300], theirs[:300])


def test_plumbing_output_is_identical_to_git(copy):
    same_as_git(copy, ["ls-files", "--stage"], "ls-files", "--stage")
    same_as_git(copy, ["ls-files", "-z"], "ls-files", "-z")
    same_as_git(copy, ["ls-tree", "HEAD"], "ls-tree", "HEAD")
    same_as_git(copy, ["ls-tree", "-r", "HEAD"], "ls-tree", "-r", "HEAD")
    same_as_git(copy, ["ls-tree", "-r", "--name-only", "-z", "HEAD"], "ls-tree", "-r", "--name-only", "-z", "HEAD")
    same_as_git(copy, ["write-tree"], "write-tree")
    same_as_git(copy, ["rev-parse", "HEAD", "HEAD~1", "v1^{tree}"], "rev-parse", "HEAD", "HEAD~1", "v1^{tree}")
    same_as_git(copy, ["rev-parse", "--short", "HEAD"], "rev-parse", "--short", "HEAD")
    same_as_git(copy, ["rev-parse", "--verify", "v1"], "rev-parse", "--verify", "v1")
    head = go.gits(copy, "rev-parse", "HEAD").strip()
    same_as_git(copy, ["cat-file", "-t", head], "cat-file", "-t", head)
    same_as_git(copy, ["cat-file", "-s", head], "cat-file", "-s", head)
    same_as_git(copy, ["cat-file", "-p", head], "cat-file", "-p", head)
    same_as_git(copy, ["cat-file", "-p", "HEAD^{tree}"], "cat-file", "-p", "HEAD^{tree}")
    same_as_git(copy, ["cat-file", "-p", "v1"], "cat-file", "-p", "v1")
    blob = go.gits(copy, "ls-tree", "-r", "HEAD").splitlines()[0].split()[2]
    same_as_git(copy, ["cat-file", "--type", "blob", blob], "cat-file", "blob", blob)
    assert kgit(copy, "cat-file", "-e", blob)[0] == 0 and kgit(copy, "cat-file", "-e", "0" * 40)[0] != 0


def test_hash_object(copy, tmp_path):
    data = b"some content\nwith lines\n"
    code, out, _ = kgit(copy, "hash-object", "--stdin", stdin=data)
    assert out.decode().strip() == go.gits(copy, "hash-object", "--stdin", input=data).strip() and code == 0
    path = os.path.join(copy, "newfile")
    open(path, "wb").write(data)
    code, out, _ = kgit(copy, "hash-object", "-w", "newfile")
    oid = out.decode().strip()
    assert go.gits(copy, "cat-file", "-p", oid) == data.decode() and go.gits(copy, "cat-file", "-t", oid).strip() == "blob"


def test_status_and_log_and_show_match_git(copy):
    go.write(copy, "ünï/new file.txt", b"x\n")
    go.write(copy, "tracked-change.txt", b"y\n")
    go.git(copy, "add", "tracked-change.txt")
    first_file = go.gits(copy, "ls-files").split("\n")[0]
    with open(os.path.join(copy, first_file), "ab") as f:
        f.write(b"more\n")
    same_as_git(copy, ["status", "--porcelain"], "status", "--porcelain", "--no-renames")                       # git's default: untracked directories collapsed
    same_as_git(copy, ["status", "--porcelain", "-uall"], "status", "--porcelain", "--no-renames", "-uall")
    same_as_git(copy, ["status", "--porcelain", "-z", "-uall"], "status", "--porcelain=v1", "-z", "--no-renames", "-uall")
    same_as_git(copy, ["status", "--porcelain", "-uno"], "status", "--porcelain", "--no-renames", "-uno")
    same_as_git(copy, ["status", "-s", "-b"], "status", "-s", "-b", "--no-renames")
    same_as_git(copy, ["log", "--oneline"], "log", "--oneline", "--no-decorate")
    same_as_git(copy, ["log"], "log", "--no-color", "--no-decorate")
    same_as_git(copy, ["log", "-n", "3"], "log", "--no-color", "--no-decorate", "-n", "3")
    same_as_git(copy, ["log", "HEAD~3..HEAD"], "log", "--no-color", "--no-decorate", "HEAD~3..HEAD")
    same_as_git(copy, ["show", "HEAD~1"], "show", "--no-color", "--no-decorate", "--no-renames", "HEAD~1") if len(go.gits(copy, "rev-list", "--parents", "-n", "1", "HEAD~1").split()) == 2 else None
    tip = go.gits(copy, "rev-list", "--no-merges", "-n", "1", "HEAD").strip()
    same_as_git(copy, ["show", tip], "show", "--no-color", "--no-decorate", "--no-renames", tip)
    same_as_git(copy, ["show", "v1"], "show", "--no-color", "--no-decorate", "--no-renames", "v1")
    same_as_git(copy, ["show", f"{tip}:{go.gits(copy, 'ls-tree', '-r', '--name-only', tip).splitlines()[0]}"], "show", f"{tip}:{go.gits(copy, 'ls-tree', '-r', '--name-only', tip).splitlines()[0]}")


def test_diff_variants_match_git(copy):
    rng = random.Random(4)
    files = [f for f in go.gits(copy, "ls-files").splitlines() if f.endswith((".txt", ".py")) and os.path.isfile(os.path.join(copy, f))][:4]
    for f in files:
        go.write(copy, f, edit_text(rng, open(os.path.join(copy, f), "rb").read()))
    for ours, theirs in [(["diff"], ["diff"]), (["diff", "-U1"], ["diff", "-U1"]), (["diff", "--numstat"], ["diff", "--numstat"]), (["diff", "--name-only"], ["diff", "--name-only"]),
                         (["diff", "--name-status"], ["diff", "--name-status"]), (["diff", "HEAD"], ["diff", "HEAD"]), (["diff", "HEAD~2", "HEAD"], ["diff", "HEAD~2", "HEAD"]),
                         (["diff", "--", files[0]], ["diff", "--", files[0]])]:
        same_as_git(copy, ours, *theirs[:1], "--no-color", "--no-renames", *theirs[1:])
    go.git(copy, "add", files[0])
    same_as_git(copy, ["diff", "--cached"], "diff", "--no-color", "--no-renames", "--cached")
    same_as_git(copy, ["diff", "--cached", "--numstat"], "diff", "--no-color", "--no-renames", "--cached", "--numstat")


def test_workflow_through_the_cli(tmp_path):
    repo = str(tmp_path / "w")
    os.makedirs(repo)
    env = {"GIT_AUTHOR_NAME": "Okan", "GIT_AUTHOR_EMAIL": "o@example.org", "GIT_COMMITTER_NAME": "Okan", "GIT_COMMITTER_EMAIL": "o@example.org"}
    run = lambda *a, **k: kgit(repo, *a, env=env, **k)  # noqa: E731
    assert run("init")[0] == 0
    go.write(repo, "a.txt", "one\n")
    assert run("add", "a.txt")[0] == 0
    code, out, err = run("commit", "-m", "first")
    assert code == 0 and b"(root-commit)" in out, err
    assert run("commit", "-m", "again")[0] == 1                                     # nothing to commit
    assert run("branch", "feature")[0] == 0
    assert run("checkout", "feature")[0] == 0
    go.write(repo, "b.txt", "two\n")
    assert run("add", ".")[0] == 0 and run("commit", "-m", "on feature")[0] == 0
    assert run("checkout", "main")[0] == 0 and not os.path.exists(os.path.join(repo, "b.txt"))
    code, out, _ = run("merge", "feature")
    assert code == 0 and "İleri sarma".encode() in out and os.path.exists(os.path.join(repo, "b.txt"))
    assert run("tag", "-a", "-m", "release", "v1")[0] == 0
    assert run("tag")[1].decode().split() == ["v1"]
    code, out, _ = run("log", "--oneline")
    assert code == 0 and len(out.decode().splitlines()) == 2
    assert run("reset", "--hard", "HEAD~1")[0] == 0 and not os.path.exists(os.path.join(repo, "b.txt"))
    assert run("fsck")[0] == 0
    go.git(repo, "fsck", "--strict")
    assert go.gits(repo, "log", "--oneline", "--all").count("\n") == 2


def test_errors_are_reported_without_a_traceback(copy):
    for argv in (["cat-file", "-p", "nonexistent"], ["checkout", "nope"], ["rm", "nonexistent"], ["add", "missing-file.zzz"], ["merge", "nope"], ["tag", "bad name", "-m", "x"]):
        code, out, err = kgit(copy, *argv)
        assert code == 1 and err.startswith("kgit: hata:") and "Traceback" not in err, (argv, code, err)
    code, _out, err = kgit(copy, "bogus-command")
    assert code == 2 and "kullanım hatası" in err


def test_outside_a_repository(tmp_path):
    code, _out, err = kgit(str(tmp_path), "status")
    assert code == 1 and "not a git repository" in err
    assert kgit(str(tmp_path), "init")[0] == 0 and kgit(str(tmp_path), "status")[0] == 0


def test_config_roundtrip(copy):
    assert kgit(copy, "config", "user.name", "Someone Else")[0] == 0
    assert go.gits(copy, "config", "user.name").strip() == "Someone Else"
    assert kgit(copy, "config", "user.name")[1].decode().strip() == "Someone Else"
    assert kgit(copy, "config", "no.such")[0] == 1
    code, out, _ = kgit(copy, "config", "--list")
    assert b"user.name=Someone Else" in out and code == 0


def test_remaining_commands_through_the_cli(tmp_path):
    repo = str(tmp_path / "w")
    os.makedirs(repo)
    env = {"GIT_AUTHOR_NAME": "Okan", "GIT_AUTHOR_EMAIL": "o@example.org", "GIT_COMMITTER_NAME": "Okan", "GIT_COMMITTER_EMAIL": "o@example.org"}
    run = lambda *a, **k: kgit(repo, *a, env=env, **k)  # noqa: E731
    assert run("init", "-b", "trunk")[0] == 0
    for name, text in (("a.txt", "a\n"), ("b.txt", "b\n"), ("sub/c.txt", "c\n"), ("ignored.log", "log\n"), (".gitignore", "*.log\n")):
        go.write(repo, name, text)
    assert run("add", "-A".replace("-A", "."), "-v")[1].count(b"add '") == 4                      # the ignored file is not added
    assert run("commit", "-m", "one", "-m", "second paragraph")[0] == 0
    assert go.gits(repo, "log", "-1", "--format=%B").strip() == "one\n\nsecond paragraph"
    # untracked files, as git lists them
    go.write(repo, "new.txt", "n\n")
    code, out, _ = run("ls-files", "-o", "--exclude-standard")
    assert out == go.git(repo, "ls-files", "-o", "--exclude-standard") and b"ignored.log" not in out
    assert b"ignored.log" in run("ls-files", "-o")[1]
    # rm
    assert run("rm", "--cached", "b.txt")[0] == 0 and os.path.exists(os.path.join(repo, "b.txt"))
    assert run("rm", "sub")[0] == 1                                                              # a directory needs -r
    assert run("rm", "-r", "sub")[0] == 0 and not os.path.exists(os.path.join(repo, "sub"))
    assert run("status", "--porcelain")[1] == go.git(repo, "status", "--porcelain")
    assert run("commit", "-m", "two")[0] == 0
    # branches and tags
    assert run("branch", "-m", "trunk", "main")[0] == 0 and run("branch")[1].decode() == "* main\n"
    assert run("branch", "tmp")[0] == 0 and run("branch", "-d", "tmp")[0] == 0 and run("branch", "-d", "tmp")[0] == 1
    assert run("tag", "light")[0] == 0 and run("tag", "-a", "note")[0] == 1                      # an annotated tag needs a message
    assert run("tag", "-d", "light")[0] == 0 and run("tag")[1] == b""
    # restore a file from the index and from a commit
    with open(os.path.join(repo, "a.txt"), "w") as f:
        f.write("changed\n")
    assert run("checkout", "--", "a.txt")[0] == 0 and open(os.path.join(repo, "a.txt")).read() == "a\n"
    assert run("checkout", "HEAD~1", "--", "sub/c.txt")[0] == 0 and open(os.path.join(repo, "sub", "c.txt")).read() == "c\n"
    assert run("status", "--porcelain")[1] == go.git(repo, "status", "--porcelain")
    # reset modes
    head, parent = go.gits(repo, "rev-parse", "HEAD").strip(), go.gits(repo, "rev-parse", "HEAD~1").strip()
    assert run("reset", "--soft", "HEAD~1")[0] == 0 and go.gits(repo, "rev-parse", "HEAD").strip() == parent
    assert run("reset", "--mixed", head)[0] == 0 and go.gits(repo, "rev-parse", "HEAD").strip() == head
    assert run("reset", "--hard", "HEAD~1")[0] == 0 and go.gits(repo, "rev-parse", "HEAD").strip() == parent
    # show of a tree and a blob
    assert run("show", "HEAD:")[1].decode().splitlines()[2:] == go.gits(repo, "ls-tree", "--name-only", "HEAD").splitlines()
    assert run("show", "HEAD:a.txt")[1] == b"a\n"
    assert run("fsck")[0] == 0
    go.git(repo, "fsck", "--strict")


def test_global_config_and_identity_errors(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo = str(tmp_path / "w")
    os.makedirs(repo)
    env = {"HOME": str(home), "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"), "GIT_AUTHOR_NAME": "", "GIT_COMMITTER_NAME": ""}
    for key in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        env.pop(key)
    clean = {k: v for k, v in {**go.env()}.items() if not k.startswith(("GIT_AUTHOR", "GIT_COMMITTER"))}
    out, err = io.BytesIO(), io.BytesIO()
    run = lambda *a: cli.main(list(a), stdout=out, stderr=err, cwd=repo, env={**clean, **env}, now=lambda: 1800000000)  # noqa: E731
    assert run("init") == 0
    go.write(repo, "f", "x")
    assert run("add", "f") == 0
    assert run("commit", "-m", "no identity yet") == 1 and b"user.name" in err.getvalue()          # the error names the fix
    assert run("config", "--global", "user.name", "Global Name") == 0 and run("config", "--global", "user.email", "g@example.org") == 0
    assert (home / ".gitconfig").read_text().startswith("[user]")
    assert run("commit", "-m", "now it works") == 0
    assert go.gits(repo, "log", "-1", "--format=%an <%ae>", extra={"GIT_CONFIG_GLOBAL": str(home / ".gitconfig")}).strip() == "Global Name <g@example.org>"


def test_commands_run_from_a_subdirectory_behave_like_git(tmp_path):
    repo = str(tmp_path / "w")
    os.makedirs(repo)
    go.git(repo, "init", "-q", "-b", "main")
    for name in ("top.txt", "src/a.py", "src/deep/b.py", "docs/c.md"):
        go.write(repo, name, name + "\n")
    go.git(repo, "add", "-A")
    go.git(repo, "commit", "-qm", "base", extra=go.commit_env(1))
    go.write(repo, "src/a.py", "changed\n")
    go.write(repo, "src/new.py", "n\n")
    go.write(repo, "top2.txt", "t\n")
    go.write(repo, "docs/d.md", "d\n")
    sub = os.path.join(repo, "src")

    def same(ours, *theirs):
        code, out, err = kgit(sub, *ours)
        assert code == 0, err
        assert out == go.git(sub, *theirs), (ours, out, go.git(sub, *theirs))

    same(["status", "-s"], "status", "-s", "--no-renames")                              # short format: paths relative to where we are, "../x" for the rest
    same(["status", "--porcelain"], "status", "--porcelain", "--no-renames")            # porcelain: always relative to the root
    same(["ls-files"], "ls-files")
    same(["ls-files", "--full-name"], "ls-files", "--full-name")
    same(["ls-files", "-s"], "ls-files", "-s")
    same(["ls-files", "-o"], "ls-files", "-o")
    same(["ls-tree", "HEAD"], "ls-tree", "HEAD")
    same(["ls-tree", "--full-tree", "HEAD"], "ls-tree", "--full-tree", "HEAD")
    same(["ls-tree", "-r", "--name-only", "HEAD"], "ls-tree", "-r", "--name-only", "HEAD")
    same(["diff"], "diff", "--no-renames")                                                # a patch is always root-relative and covers the whole tree
    assert kgit(sub, "add", "new.py", "../top2.txt")[0] == 0
    assert go.gits(repo, "status", "--porcelain").splitlines() == [" M src/a.py", "A  src/new.py", "A  top2.txt", "?? docs/d.md"]
    assert kgit(sub, "add", "-u")[0] == 0                                                 # -u without a path covers the whole work tree
    assert go.gits(repo, "status", "--porcelain").splitlines() == ["M  src/a.py", "A  src/new.py", "A  top2.txt", "?? docs/d.md"]
