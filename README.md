# git-from-scratch

A Git client written from scratch in pure Python (standard library only, no dependencies): the object model, loose and packed object stores, the index, references,
`init` / `add` / `rm` / `status` / `commit` / `log` / `show` / `diff` / `branch` / `checkout` / `reset` / `tag` / `merge` / `fsck`, `.gitignore` and the config format.

It does **not** shell out to git and does not use GitHub's. It reads and writes the real on-disk formats, so a repository made by `kgit` is an ordinary git
repository (and the other way round), and **real git is the oracle in the test-suite**: every component is checked against it, byte for byte, never against a mock.

```
$ kgit init demo && cd demo
$ kgit add . && kgit commit -m "First commit"
[main (root-commit) 41634ee] First commit
$ kgit checkout -b topic && ... && kgit checkout main && kgit merge topic
$ git log --graph --oneline          # real git reads the result
$ git fsck --strict                  # and finds nothing wrong
```

The command names are git's own (`kgit commit`, `kgit checkout`, ...) so muscle memory and scripts carry over; the prose output of the porcelain commands is Turkish, while
the machine formats (`status --porcelain`, `ls-files --stage`, `log --oneline`, patches, `cat-file`, `ls-tree`) are git's byte for byte.

## Install and use

```bash
pip install -e .          # Python 3.9+, no dependencies
kgit --help
kgit dogrulama            # the self-verification battery (see below)
```

| Plumbing | Porcelain |
|---|---|
| `hash-object` `cat-file` `ls-tree` `ls-files` `write-tree` `rev-parse` `config` | `init` `add` `rm` `status` `commit` `log` `show` `diff` `branch` `checkout` `reset` `tag` `merge` `fsck` |

As a library: `from kendi_git.repo import Repo` (see [`examples/walkthrough.py`](examples/walkthrough.py)); a shell session is in
[`examples/session.sh`](examples/session.sh).

## How it works

| Module | What it implements |
|---|---|
| `objects.py` | Object ids (`SHA-1("<type> <size>\0" + content)`), blobs, trees (`<octal mode> <name>\0<20 raw bytes>`, directories sorted as `name/`), commits, annotated tags. Unknown headers (`gpgsig`, `encoding`, `mergetag`) are preserved so that re-serialising never changes an id. |
| `odb.py`, `pack.py` | The object database: loose objects (zlib, written atomically, mode 0444), packfiles with index v2 (fan-out table, 32/64-bit offsets), `OFS_DELTA` and `REF_DELTA` chains, `alternates`, unique-prefix lookup and git's abbreviation length rules. |
| `index.py` | The staging area, `DIRC` versions 2 to 4 (v4's prefix-compressed paths are read): 62-byte fixed entries, NUL padding to eight bytes, the 12-bit name length that overflows for long paths, conflict stages, stat data in git's truncated form, atomic `index.lock` writes. |
| `refs.py` | `HEAD`, branches, tags, `packed-refs` (with peeled lines), symbolic refs, `.lock` files, directory/file conflicts, empty-directory pruning, the full `check-ref-format` rules. |
| `config.py` | The config file format (sections, subsections, quotes, escapes, continuation lines, comments), layered lookup, in-place edits that leave the rest of the file alone, author/committer identity and every date format git accepts. |
| `ignore.py` | `.gitignore` semantics: anchoring, `**` forms, character classes, negation, nested files, `info/exclude`, the global file, and git's literal-prefix quirk. |
| `diff.py` | Myers' algorithm in linear space, then a port of xdiff's change compaction **with the indent heuristic**, then git's patch format (hunks, function context, `\ No newline at end of file`, binary files, C-quoted paths). |
| `merge.py` | Merge bases, the line-level three-way merge with xdiff's conflict rule (changes that overlap *or touch* conflict unless identical), a tree merge, fast-forward and true merges. |
| `checkout.py` | The two-way tree switch of `git read-tree -m -u`, used by `checkout`, `reset` and file restore: local changes are carried over or the switch is refused, before anything is touched. |
| `repo.py` | The repository: discovery, `add` / `rm` pathspecs, `status`, `commit` (message clean-up, `--amend`), the date-ordered history walk, `rev-parse` expressions, branches and tags. |
| `fsck.py` | End-to-end integrity checking in the manner of `git fsck --strict`: hash of every loose object, pack checksums, per-type structure rules, links followed from refs and the index, dangling objects. |
| `checks.py`, `cli.py` | The `dogrulama` battery and the `kgit` command line. |

## How it was validated

The ground rule of this project: **a behaviour is only believed once real git has agreed with it.** The suite has 240 tests; the bulk of them run both programs on the
same input and compare the answers.

| Component | Compared with git on |
|---|---|
| Objects, loose and packed | every object of a real repository read back loose, after `gc --aggressive` (OFS deltas), after a `useDeltaBaseOffset=false` repack (REF deltas) and mixed; commits with `gpgsig`/`mergetag` re-serialise to the same bytes; damaged objects are detected |
| Index | `ls-files --stage` and `--debug` (stat data) of git's index, byte-exact re-serialisation, paths around the 4095-byte name limit, conflict stages; an index written here is accepted by `git status` and `fsck` |
| Refs and names | 400 random ref names against `check-ref-format`, loose/packed listings against `for-each-ref`, directory/file conflicts, lock files, deletion from `packed-refs` |
| Config and dates | a file of edge cases parsed like `git config --list -z`, random values written by either side and read by the other, 27 date spellings (including ones git refuses), identity clean-up |
| `.gitignore` | 70 random repositories per run (840 in a longer one: random patterns in random nested files) against `ls-files --others --ignored` and `check-ignore` |
| Patches | `diff`, `diff --cached`, commit-to-commit and `show` byte for byte; 296 random patches of repetitive, code-like text (the hard case: many equally good edit scripts) are all byte-identical to `git diff` |
| History | default log order (including ties and clock skew) on random DAGs, `A..B` ranges, merge bases (including criss-cross pairs), the log/show/date formats |
| Merge | 300 + 300 random three-way line merges against `git merge-file` (verdict *and* merged text), 40 random tree merges against `git merge-tree`, and a merge commit that is the very same object git creates |
| Whole sessions | random `add` / `rm` / `commit` / `branch` / `checkout` / `reset` / `merge` / `tag` sessions run in lock step in two repositories, one driven through this package and one through git: after **every step** HEAD, refs, index, work tree and `status` must be identical, and a refusal by one must be a refusal by the other (the suite runs 8 such sessions; longer runs of about 90 plain sessions of 45 steps and 24 branching sessions of 40 rounds each were made on earlier revisions while the divergences below were being found and fixed, and a last round of 12 fresh branching sessions and 16 fresh plain ones passed; that round found one more divergence, `add -u` on an ignored path, which is fixed and covered by a test) |
| `fsck` | missing, truncated, bit-flipped, mis-named objects, damaged packs, malformed trees/commits/tags, broken refs: the verdict and the culprit ids |
| Real repositories | `tools/real_world_sweep.py`: 99 + 19 + 1 real repositories, 0 disagreements (below) |

`kgit dogrulama` ("verification") packs a quick version of this into the program itself: known answers computed with real git, then a differential battery
(objects both ways, packs, index, sessions, commit ids, history order, patches, ignore decisions, merges, corruption detection, refs and config) on the git installed on your
machine. It is also the last step of CI.

### What the oracle found

Comparing with git found things that no documentation or intuition had suggested. A selection, all fixed and now covered by tests:

* In git 2.39 a `.gitignore` line like `k**/**` matches `keep.txt` (later versions fixed this; the tests keep clear of it): git compares the literal prefix (`k`) on its own and hands only the *rest* of the pattern to the glob matcher, which
  then sees `**/` at the start of a pattern. Also: CRLF line endings are stripped from patterns, and a glob that matches nothing but ignored files "matches nothing".
* `git add <path>` reports an ignored *directory* that the path points into, even when files below it are tracked, but only after it has staged everything else.
* `git log` indents blank lines of a message with four spaces; `git tag -m` drops lines starting with `#` while `git commit -m` keeps them.
* `commit --amend` is refused when the result would equal the amended commit's parent; a bare number is only a timestamp from nine digits on unless it starts with `@`.
* `git fsck --strict` does not fail on a missing `tagger` or a nonstandard file mode (they are informational), and calls an object *dangling* only if nothing, not even another
  unreachable object, refers to it.
* A truncated delta ending in a dangling insert or copy instruction was accepted by the first version of the pack reader.
* Reproducing git's patches on real code takes a *port of xdiff's whole pipeline*, not just Myers' algorithm: with the textbook shortest-edit-script version about one patch in
  five differed from git's (an inserted blank line one line too high), after adding change compaction and the indent heuristic a few large ones still did (replacing a
  600-line licence file: git's record-discarding and search cut-offs deliberately give a *longer* script than the shortest). Ported as well, all recent patches of the real
  repositories below are byte-identical.
* Shallow clones (`.git/shallow`) broke the first run over real repositories: the boundary commits' parent objects are absent, and git treats those commits as parentless.
* Smaller ones: `rev-parse` had to try `^{type}` before `^N`; `-C` clashed with the positional argument of `init`; a ref directory vanished after `pack-refs`; ignore rules cached across edits.

### Real repositories

`tools/real_world_sweep.py` clones each repository (`git clone --no-local`, so the history arrives as a real pack with deltas) and compares, read-only: every object read back
must hash to its own id; the reference list; the `rev-list` order of `HEAD` and the set of commits reachable from all refs; the index; `status` (the clone *and* the original
work tree with its real stat data, dirty files and untracked files); the verdict of `fsck --strict`; and the patches of the latest commits against `git diff`.

| Set | Repositories | Objects read back | Commits walked | Patches byte-identical to `git diff` | Disagreements |
|---|---|---|---|---|---|
| Small portfolio projects | 99 | 2,915 | 118 | 114 / 114 | 0 |
| Larger private repositories (up to 566 commits and 9,500 objects, 14 of them *shallow* clones, one with a dirty work tree of 29 changed and untracked paths) | 19 | 31,952 | 1,543 | 54 / 54 | 0 |
| Homebrew's own repository (52,340 commits, 362,574 objects in 11 packs, 535 refs; 20,000 random objects read back, `fsck` over all of them) | 1 | 20,000 (of 362,574) | 52,340 | 6 / 6 | 0 |

All of it ran on an old laptop under a busy virus scanner; the Homebrew repository took 21 minutes, most of it in `git fsck` and this program's own `fsck`.

## Limits (deliberate, and stated plainly)

* **No network**: no `clone`, `fetch`, `push`, remotes or upstream tracking (`@{upstream}`); no packfile *writing* (objects are stored loose; `git gc` can pack them).
* **No rename or copy detection** anywhere. A rename on one side plus an edit on the other is a conflict here where git merges it; `diff` and `show` print a deletion and an
  addition (compare with `git diff --no-renames`).
* **Merging**: a conflicted merge is *refused* and nothing is touched (no conflict markers, no `MERGE_HEAD`); more than one merge base (criss-cross) is refused; the merge
  needs an index equal to `HEAD`. Merge commits are shown without a combined diff.
* **No reflogs**, so `HEAD@{1}`-style revisions are refused; no hooks; no `.gitattributes`, filters, `autocrlf` or LFS; no submodule operations (gitlinks are carried along);
  no sparse checkout; no SHA-256 repositories; index versions 2-4 are read (and rewritten as version 2), the split index is not; no `rebase`, `stash`, `cherry-pick`, `blame`, `bisect`, `--graph`, `--stat`; pathspec magic (`:(glob)`, `:!x`) is refused.
* `commit` needs `-m` (no editor is started); `log -- <path>` (history simplification) is not implemented; `include`/`includeIf` in config files are not followed.
* POSIX only (symlinks, permission bits, `lstat` stat data); tested on macOS and Linux. Case-insensitive file systems are honoured for ignore matching but not elsewhere,
  and Unicode normalisation of file names (`core.precomposeunicode`) is not performed.
* Hunk headers print git's default function context (a line starting with a letter, `_` or `$`); custom `diff=` drivers are not read.
* Diff and merge are checked against xdiff statistically, not proven identical: where several shortest edit scripts exist the choice can differ in rare cases (see the numbers above).

## Development

```bash
pip install -e ".[dev]"
pytest -q                                   # needs a real git on the PATH as the oracle (tests skip themselves without it)
KGIT_SEED_BASE=100 KGIT_TWIN_SEEDS=30 KGIT_BRANCH_SEEDS=10 pytest tests/test_twin.py   # a longer random-session fuzz run
KGIT_FUZZ_SEEDS=60 pytest tests/test_ignore.py -k random                             # a longer ignore-rule fuzz run
python tools/real_world_sweep.py ~/some/folder/of/repositories                      # compare with git on your own repositories
```

## License

MIT
