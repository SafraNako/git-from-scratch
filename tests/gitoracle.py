"""Run real git as the oracle: an isolated environment (no user or system configuration), fixed identities and dates."""
import os
import re
import shutil
import subprocess
import tempfile

GIT = shutil.which("git")


def _version():
    if GIT is None:
        return (0, 0, 0)
    text = subprocess.run([GIT, "--version"], capture_output=True, text=True).stdout
    numbers = [int(n) for n in re.findall(r"\d+", text.split("version")[-1])[:3]]
    return tuple(numbers + [0] * (3 - len(numbers)))


GIT_VERSION = _version()
HOME = tempfile.mkdtemp(prefix="kgit-home-")
IDENT = {"GIT_AUTHOR_NAME": "A U Thor", "GIT_AUTHOR_EMAIL": "author@example.org", "GIT_COMMITTER_NAME": "C O Mitter", "GIT_COMMITTER_EMAIL": "committer@example.org",
         "GIT_AUTHOR_DATE": "1700000000 +0000", "GIT_COMMITTER_DATE": "1700000000 +0000"}


def env(extra=None):
    e = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": HOME, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
         "LC_ALL": "C", "GIT_PAGER": "cat", "TZ": "UTC"}
    # newer git runs automatic maintenance after commits and would expire reflog entries dated in 2023: switch that off for the oracle
    e.update({"GIT_CONFIG_COUNT": "4", "GIT_CONFIG_KEY_0": "maintenance.auto", "GIT_CONFIG_VALUE_0": "false", "GIT_CONFIG_KEY_1": "gc.auto", "GIT_CONFIG_VALUE_1": "0",
              "GIT_CONFIG_KEY_2": "gc.reflogExpire", "GIT_CONFIG_VALUE_2": "never", "GIT_CONFIG_KEY_3": "gc.reflogExpireUnreachable", "GIT_CONFIG_VALUE_3": "never"})
    e.update(IDENT)
    e.update(extra or {})
    return e


def git(cwd, *args, input=None, extra=None, check=True):
    """stdout (bytes) of a real ``git`` run in ``cwd``."""
    p = subprocess.run([GIT, *args], cwd=cwd, input=input, capture_output=True, env=env(extra))
    if check and p.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed ({p.returncode}): {p.stderr.decode(errors='replace')}")
    return p.stdout if check else p


def gits(cwd, *args, **kw):
    """Like ``git`` but returns decoded stdout even with ``check=False`` (the exit code is then ignored)."""
    out = git(cwd, *args, **kw)
    out = out if isinstance(out, bytes) else out.stdout
    return out.decode("utf-8", errors="surrogateescape")


def commit_env(n):
    """Distinct, increasing commit dates so that history order is unambiguous."""
    stamp = f"{1700000000 + 100 * n} +0000"
    return {"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}


def write(root, path, content):
    full = os.path.join(root, path)
    os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
    with open(full, "wb" if isinstance(content, bytes) else "w") as f:
        f.write(content)
    return full
