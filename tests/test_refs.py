import os
import random

import pytest

import gitoracle as go
from kendi_git.errors import GitError
from kendi_git.refs import RefStore, check_branch_name, check_ref_format

pytestmark = pytest.mark.skipif(go.GIT is None, reason="real git is not installed")


def git_ok(repo, *args):
    return go.git(repo, *args, check=False).returncode == 0


def for_each(repo):
    out = go.gits(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/")
    return [tuple(line.split(" ")) for line in out.splitlines()]


# --- names ------------------------------------------------------------------------------------------------------------------------------------------
HAND = ["refs/heads/main", "refs/heads/a/b", "refs/heads/a..b", "refs/heads/.hidden", "refs/heads/x.lock", "refs/heads/y.lock/z", "refs/heads/end.", "refs/heads/sp ace",
        "refs/heads/tilde~1", "refs/heads/caret^", "refs/heads/col:on", "refs/heads/q?", "refs/heads/st*r", "refs/heads/br[ack", "refs/heads/back\\slash", "refs/heads//double",
        "/refs/heads/lead", "refs/heads/trail/", "refs/heads/at@{x", "refs/heads/@", "@", "HEAD", "refs", "refs/heads/ünï", "refs/heads/a-b_c.d", "refs/heads/-dash",
        "refs/heads/tab\there", "refs/heads/del\x7f", "refs/heads/ok.lockx", "refs/heads/.", "refs/heads/..", "refs/./x", "refs/heads/a/.b", "refs/heads/@x", "refs/heads/x@"]


@pytest.mark.parametrize("name", HAND)
def test_hand_picked_ref_names_agree_with_git(tmp_path, name):
    for onelevel in (False, True):
        args = ["check-ref-format"] + (["--allow-onelevel"] if onelevel else []) + [name]
        assert check_ref_format(name, onelevel=onelevel) == git_ok(str(tmp_path), *args), (name, onelevel)


def test_random_ref_names_agree_with_git(tmp_path):
    rng = random.Random(5)
    alphabet = list("abcAB19/._-@{}~^:?*[]\\ ") + ["..", "/.", ".lock", "@{", "é", "\t"]
    for _ in range(400):
        name = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 9)))
        assert check_ref_format(name) == git_ok(str(tmp_path), "check-ref-format", name), name


def test_branch_names_agree_with_git_branch(small_repo_factory):
    """The real oracle for branch names is ``git branch NAME`` itself (``check-ref-format --branch`` also expands things like ``@``)."""
    repo, _oids = small_repo_factory()
    names = ["main2", "feature/x", "-lead", "HEAD", "a..b", "ok-name", "@", "x.lock", "sp ace", "ü", "a/b/c", "end/", ".dot", "a@{b", "tab\t", "q?", "new-1.0", "@x"]
    for name in names:
        ok = git_ok(repo, "branch", "--", name)
        assert check_branch_name(name) == ok, name
        if ok:
            go.git(repo, "branch", "-D", "--", name)


# --- reading ---------------------------------------------------------------------------------------------------------------------------------------
def test_loose_and_packed_refs_listed_like_for_each_ref(history_repo, tmp_path):
    for packed in (False, True):
        repo = str(tmp_path / ("p" if packed else "l"))
        import shutil
        shutil.copytree(history_repo, repo, symlinks=True)
        if packed:
            go.git(repo, "pack-refs", "--all")
            assert not os.path.exists(os.path.join(repo, ".git", "refs", "heads", "main"))
        store = RefStore(os.path.join(repo, ".git"))
        assert store.list("refs/") == [(n, o) for n, o in for_each(repo)] and len(store.list("refs/")) >= 4
        assert store.head_oid() == go.gits(repo, "rev-parse", "HEAD").strip()
        assert store.head() == ("ref", go.gits(repo, "symbolic-ref", "HEAD").strip())
        assert store.current_branch() == go.gits(repo, "symbolic-ref", "--short", "HEAD").strip()
        assert [n for n, _ in store.list("refs/tags/")] == [n for n, _ in for_each(repo) if n.startswith("refs/tags/")]


def test_peeled_values_of_packed_tags(history_repo, tmp_path):
    import shutil
    repo = str(tmp_path / "r")
    shutil.copytree(history_repo, repo, symlinks=True)
    go.git(repo, "pack-refs", "--all")
    store = RefStore(os.path.join(repo, ".git"))
    assert store.packed_peeled("refs/tags/v1") == go.gits(repo, "rev-parse", "v1^{commit}").strip()      # annotated tag: peeled to its commit
    assert store.packed_peeled("refs/tags/light") is None                                                    # lightweight tag: nothing to peel


def test_detached_head_and_unborn_branch(tmp_path):
    repo = str(tmp_path / "r")
    os.makedirs(repo)
    go.git(repo, "init", "-q", "-b", "trunk")
    store = RefStore(os.path.join(repo, ".git"))
    assert store.head() == ("ref", "refs/heads/trunk") and store.head_oid() is None and store.current_branch() == "trunk"
    go.write(repo, "f", "x")
    go.git(repo, "add", "f")
    go.git(repo, "commit", "-qm", "c", extra=go.commit_env(1))
    oid = go.gits(repo, "rev-parse", "HEAD").strip()
    go.git(repo, "checkout", "-q", "--detach")
    assert store.head() == ("detached", oid) and store.current_branch() is None and store.head_oid() == oid


def test_corrupt_refs_are_reported(tmp_path):
    gitdir = str(tmp_path / ".git")
    os.makedirs(os.path.join(gitdir, "refs", "heads"))
    open(os.path.join(gitdir, "HEAD"), "w").write("ref: refs/heads/main\n")
    open(os.path.join(gitdir, "refs", "heads", "main"), "w").write("not an id\n")
    store = RefStore(gitdir)
    with pytest.raises(GitError):
        store.read("refs/heads/main")
    open(os.path.join(gitdir, "packed-refs"), "w").write("garbage line\n")
    with pytest.raises(GitError):
        store.list()
    open(os.path.join(gitdir, "refs", "heads", "main"), "w").write("ref: refs/heads/main\n")
    with pytest.raises(GitError) as e:
        RefStore(gitdir).read("HEAD")
    assert "loop" in str(e.value) or "packed" in str(e.value)


# --- writing ---------------------------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def small_repo_factory(tmp_path):
    counter = [0]

    def make():
        counter[0] += 1
        return make_small_repo(str(tmp_path / ("r%d" % counter[0])))
    return make


@pytest.fixture
def small_repo(tmp_path):
    return make_small_repo(str(tmp_path / "r"))


def make_small_repo(repo):
    os.makedirs(repo)
    go.git(repo, "init", "-q", "-b", "main")
    go.write(repo, "f", "x")
    go.git(repo, "add", "f")
    oids = []
    for i in range(3):
        go.write(repo, "f", "x%d" % i)
        go.git(repo, "commit", "-qam", "c%d" % i, extra=go.commit_env(i))
        oids.append(go.gits(repo, "rev-parse", "HEAD").strip())
    return repo, oids


def test_updates_are_accepted_by_git_and_through_head(small_repo):
    repo, oids = small_repo
    store = RefStore(os.path.join(repo, ".git"))
    store.update("refs/heads/new/deep", oids[0])
    store.update("refs/tags/t", oids[1])
    store.update("HEAD", oids[2])                                    # HEAD is symbolic: this moves the branch it names
    assert go.gits(repo, "rev-parse", "refs/heads/new/deep", "refs/tags/t", "main").split() == [oids[0], oids[1], oids[2]]
    store.update("refs/heads/new/deep", oids[2], old=oids[0])
    with pytest.raises(GitError):
        store.update("refs/heads/new/deep", oids[0], old=oids[0])      # stale expectation: refused, nothing written
    assert store.read("refs/heads/new/deep") == oids[2]
    go.git(repo, "fsck", "--strict")
    assert not [f for _r, _d, fs in os.walk(os.path.join(repo, ".git", "refs")) for f in fs if f.endswith(".lock")]


@pytest.mark.parametrize("first,second", [("refs/heads/a", "refs/heads/a/b"), ("refs/heads/a/b", "refs/heads/a"), ("refs/heads/x/y/z", "refs/heads/x/y"),
                                          ("refs/heads/x/y", "refs/heads/x/y/z/w")])
def test_file_directory_conflicts_agree_with_git(small_repo, first, second):
    repo, oids = small_repo
    ours_store = RefStore(os.path.join(repo, ".git"))
    ours_store.update(first, oids[0])
    theirs_ok = git_ok(repo, "update-ref", second, oids[0])
    go.git(repo, "update-ref", "-d", second, check=False)
    go.git(repo, "pack-refs", "--all")                              # also when the conflicting ref is only a packed line
    try:
        ours_store.update(second, oids[0])
        ours_ok = True
    except GitError:
        ours_ok = False
    assert ours_ok == theirs_ok is False


def test_conflicts_are_checked_against_packed_refs_too(small_repo):
    repo, oids = small_repo
    go.git(repo, "update-ref", "refs/heads/p", oids[0])
    go.git(repo, "pack-refs", "--all")
    store = RefStore(os.path.join(repo, ".git"))
    with pytest.raises(GitError):
        store.update("refs/heads/p/child", oids[0])


def test_empty_directories_left_by_deleted_refs_do_not_block_new_ones(small_repo):
    repo, oids = small_repo
    store = RefStore(os.path.join(repo, ".git"))
    store.update("refs/heads/a/b/c", oids[0])
    store.delete("refs/heads/a/b/c")
    assert not os.path.exists(os.path.join(repo, ".git", "refs", "heads", "a"))      # emptied parents are pruned
    os.makedirs(os.path.join(repo, ".git", "refs", "heads", "a", "left", "over"))
    store.update("refs/heads/a", oids[1])                                              # and stray empty dirs are swept for a ref of that name
    assert go.gits(repo, "rev-parse", "refs/heads/a").strip() == oids[1]


def test_deleting_loose_packed_and_both(small_repo):
    repo, oids = small_repo
    for n in range(6):
        go.git(repo, "update-ref", f"refs/heads/b{n}", oids[n % 3])
    go.git(repo, "tag", "-a", "-m", "annotated", "ann", oids[0], extra=go.commit_env(9))
    go.git(repo, "pack-refs", "--all")
    go.git(repo, "update-ref", "refs/heads/b0", oids[2])             # b0 is now loose AND packed (with an older value)
    store = RefStore(os.path.join(repo, ".git"))
    assert store.read("refs/heads/b0") == oids[2]                    # the loose value wins
    for name in ["refs/heads/b0", "refs/heads/b3", "refs/tags/ann"]:
        store.delete(name)
    assert [n for n, _ in for_each(repo)] == [n for n, _ in store.list("refs/")]
    assert "refs/heads/b0" not in [n for n, _ in for_each(repo)] and "refs/tags/ann" not in [n for n, _ in for_each(repo)]
    go.git(repo, "fsck", "--strict")
    with pytest.raises(GitError):
        store.delete("refs/heads/b0")
    assert go.gits(repo, "show-ref", "--verify", "refs/heads/b1").split()[0] == oids[1]


def test_head_and_symbolic_refs_can_be_written(small_repo):
    repo, oids = small_repo
    store = RefStore(os.path.join(repo, ".git"))
    store.update("refs/heads/other", oids[0])
    store.set_symbolic("HEAD", "refs/heads/other")
    assert go.gits(repo, "symbolic-ref", "HEAD").strip() == "refs/heads/other" and store.head() == ("ref", "refs/heads/other")
    with pytest.raises(GitError):
        store.set_symbolic("HEAD", "not a ref")
    with pytest.raises(GitError):
        store.update("refs/heads/bad name", oids[0])
    with pytest.raises(GitError):
        store.update("refs/heads/main", "xyz")


def test_a_held_lock_blocks_updates_and_is_left_alone(small_repo):
    repo, oids = small_repo
    store = RefStore(os.path.join(repo, ".git"))
    lock = os.path.join(repo, ".git", "refs", "heads", "main.lock")
    open(lock, "w").close()
    with pytest.raises(GitError) as e:
        store.update("refs/heads/main", oids[0])
    assert "lock" in str(e.value) and os.path.exists(lock)
    os.unlink(lock)
    store.update("refs/heads/main", oids[0])
    assert go.gits(repo, "rev-parse", "main").strip() == oids[0]
