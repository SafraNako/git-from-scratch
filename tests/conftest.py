import os
import random

import pytest

import gitoracle as go


def make_repo(path):
    os.makedirs(path, exist_ok=True)
    go.git(path, "init", "-q", "-b", "main")
    return path


def build_history(path, seed=0, commits=12):
    """A real repository made by real git with a varied history: nested directories, an executable, a symlink, an empty file, binary data, unicode names, deletions,
    two branches and a merge, an annotated and a lightweight tag. Returns the repository path."""
    rng = random.Random(seed)
    make_repo(path)
    names = ["README.md", "src/main.py", "src/util/helpers.py", "docs/guide.txt", "ünïcode/файл.txt", "a", "a.b", "a-b", "a/inner.txt", "data/blob.bin", "empty.txt"]
    n = 0
    for i in range(commits):
        for name in rng.sample(names, rng.randint(1, 4)):
            if name == "a" and os.path.isdir(os.path.join(path, "a")) or name == "a/inner.txt" and os.path.isfile(os.path.join(path, "a")):
                continue
            if name == "data/blob.bin":
                content = bytes(rng.randrange(256) for _ in range(rng.randint(0, 3000)))
            elif name == "empty.txt":
                content = ""
            else:
                content = "\n".join(f"line {j} of {name} rev {i} " + "x" * rng.randint(0, 40) for j in range(rng.randint(1, 60))) + "\n"
            go.write(path, name, content)
        if i == 3:
            go.write(path, "tool.sh", "#!/bin/sh\necho hi\n")
            os.chmod(os.path.join(path, "tool.sh"), 0o755)
            if not os.path.lexists(os.path.join(path, "link")):
                os.symlink("README.md", os.path.join(path, "link"))
        if i == 6 and os.path.exists(os.path.join(path, "docs/guide.txt")):
            os.unlink(os.path.join(path, "docs/guide.txt"))
        go.git(path, "add", "-A")
        n += 1
        go.git(path, "commit", "-q", "-m", f"commit {i}\n\nbody of commit {i}", extra=go.commit_env(n))
    go.git(path, "tag", "light")
    go.git(path, "tag", "-a", "-m", "annotated tag", "v1", extra=go.commit_env(n + 1))
    go.git(path, "checkout", "-q", "-b", "side")
    go.write(path, "side.txt", "on the side branch\n")
    go.git(path, "add", "-A")
    go.git(path, "commit", "-q", "-m", "side commit", extra=go.commit_env(n + 2))
    go.git(path, "checkout", "-q", "main")
    go.write(path, "main-only.txt", "only on main\n")
    go.git(path, "add", "-A")
    go.git(path, "commit", "-q", "-m", "main commit", extra=go.commit_env(n + 3))
    go.git(path, "merge", "-q", "--no-ff", "-m", "merge side", "side", extra=go.commit_env(n + 4))
    return path


@pytest.fixture(scope="session")
def history_repo(tmp_path_factory):
    if go.GIT is None:
        pytest.skip("real git is not installed")
    return build_history(str(tmp_path_factory.mktemp("history")))


@pytest.fixture
def repo_copy(history_repo, tmp_path):
    """A fresh copy of the shared history repository that a test may change or repack."""
    import shutil
    dest = str(tmp_path / "copy")
    shutil.copytree(history_repo, dest, symlinks=True)
    return dest
