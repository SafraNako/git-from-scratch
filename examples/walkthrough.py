"""A tour of the library API: build a repository with this package, then let real git read it.

    python examples/walkthrough.py

Everything happens in a temporary directory. If git is installed, its view of the result is shown at the end.
"""
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from kendi_git import checkout, merge  # noqa: E402
from kendi_git.objects import hash_object  # noqa: E402
from kendi_git.repo import Repo  # noqa: E402


def main():
    root = tempfile.mkdtemp(prefix="kgit-walkthrough-")
    env = {**os.environ, "GIT_AUTHOR_NAME": "Ada", "GIT_AUTHOR_EMAIL": "ada@example.org", "GIT_COMMITTER_NAME": "Ada", "GIT_COMMITTER_EMAIL": "ada@example.org"}
    repo = Repo.init(root, env=env)

    # 1. A blob's id is the SHA-1 of "blob <size>\0<content>": no repository is needed to compute it.
    print("id of 'hello\\n':", hash_object("blob", b"hello\n"))

    # 2. Stage and commit.
    with open(os.path.join(root, "notes.txt"), "w") as f:
        f.write("first line\nsecond line\n")
    repo.add(["notes.txt"])
    first = repo.commit("Add notes")
    print("first commit:", first)

    # 3. Branch, change, commit, come back and merge.
    repo.create_branch("feature")
    checkout.checkout(repo, "feature")
    with open(os.path.join(root, "extra.txt"), "w") as f:
        f.write("added on the feature branch\n")
    repo.add(["extra.txt"])
    repo.commit("Add extra.txt")
    checkout.checkout(repo, "main")
    with open(os.path.join(root, "notes.txt"), "a") as f:
        f.write("third line\n")
    repo.add(["notes.txt"])
    repo.commit("Extend notes")
    result = merge.merge(repo, "feature")
    print("merge:", result.kind, result.oid)

    # 4. Read the history back, in git's order.
    for oid in repo.walk([repo.refs.head_oid()]):
        print("  ", repo.odb.abbrev(oid), repo.read_commit(oid).subject)

    # 5. The same repository, seen by real git.
    if shutil.which("git"):
        print("--- git log --graph --oneline")
        print(subprocess.run(["git", "log", "--graph", "--oneline"], cwd=root, capture_output=True, text=True).stdout)
        print("--- git fsck --strict:", subprocess.run(["git", "fsck", "--strict"], cwd=root, capture_output=True, text=True).returncode == 0 and "clean")
    shutil.rmtree(root)


if __name__ == "__main__":
    main()
