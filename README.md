# git-from-scratch

A Git client written from scratch in pure Python (standard library only, no dependencies): the object model, loose and packed object stores, the index, references and
reflogs, `init` / `add` / `rm` / `mv` / `status` / `commit` / `log` / `show` / `diff` / `branch` / `checkout` / `reset` / `tag` / `merge` / `fsck`, `.gitignore` and the
config format, and the network side: `clone` / `fetch` / `pull` / `push` / `remote` over local paths, `git://` and smart `http(s)://`.

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
| `hash-object` `cat-file` `ls-tree` `ls-files` `write-tree` `rev-parse` `config` `ls-remote` | `init` `add` `rm` `mv` `status` `commit` `log` `show` `diff` `branch` `checkout` `reset` `tag` `merge` `reflog` `clone` `fetch` `pull` `push` `remote` `fsck` |

Beyond the everyday basics: `log --graph` / `--stat` / `-p` / `--follow` / `-- <path>` (with git's history simplification) / `--first-parent` / `-m` / `--cc`; combined diffs of
merge commits and of conflicted files; rename and copy detection in `diff`, `show`, `log` and `status`; merges that leave conflict markers, unmerged index stages and
`MERGE_HEAD` (`merge --abort` / `--continue`) and that follow renames; reflogs and `@{n}` / `@{-n}` / `@{upstream}` revisions; upstream tracking with ahead/behind counts.

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
| `diff.py` | Myers' algorithm in linear space, then a port of xdiff's change compaction **with the indent heuristic**, then git's patch format (hunks, function context, `\ No newline at end of file`, binary files, C-quoted paths, renames) and the `--stat` / `--shortstat` / `--summary` renderers (show_stats). |
| `merge.py` | Merge bases, a port of xmerge (conflict markers byte for byte, the "touching changes conflict" rule, CRLF handling), a tree merge that follows renames, fast-forward and true merges, conflicted merges (stages 1-3, `MERGE_HEAD`/`MERGE_MSG`, `--abort`, concluding commit). |
| `graph.py`, `history.py` | `log --graph` (a port of graph.c: columns, merge rows, collapsing rows) and the revision walk (a port of `limit_list`, `process_parents` and `try_to_simplify_commit`: ranges, `-- <path>`, `--full-history`, parent rewriting). |
| `combine.py`, `rename.py` | Combined diffs (combine-diff.c: per-parent flags, lost-line coalescing, `--cc` hunk filtering) and rename/copy detection (diffcore-rename and diffcore-delta: exact matches, base-name guesses, span-hash similarity, candidate matrix). |
| `packio.py`, `protocol.py`, `transport.py`, `remote.py` | Receiving packs (index-pack: delta resolution, thin packs, `.idx` v2) and writing them (stored deltas are copied), pkt-lines and the protocol v2 `ls-refs`/`fetch` and v0 `receive-pack` conversations, transports for paths, `git://` and smart HTTP, and remotes, refspecs, fetch, push, clone, pull and upstream tracking. |
| `checkout.py` | The two-way tree switch of `git read-tree -m -u`, used by `checkout`, `reset` and file restore: local changes are carried over or the switch is refused, before anything is touched. |
| `repo.py` | The repository (bare ones too): discovery, `add` / `rm` / `mv` pathspecs, `status`, `commit` (message clean-up, `--amend`), reflogs, the date-ordered history walk, `rev-parse` expressions, branches and tags. |
| `fsck.py` | End-to-end integrity checking in the manner of `git fsck --strict`: hash of every loose object, pack checksums, per-type structure rules, links followed from refs and the index, dangling objects. |
| `checks.py`, `cli.py` | The `dogrulama` battery and the `kgit` command line. |

## How it was validated

The ground rule of this project: **a behaviour is only believed once real git has agreed with it.** The suite has 400 tests; the bulk of them run both programs on the
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
| Merge | 300 + 300 random three-way line merges against `git merge-file` (verdict *and* merged text) and 750 more for the conflict markers, 40 random tree merges against `git merge-tree`, and a merge commit that is the very same object git creates; conflicted merges (content, add/add, modify/delete, binary) compared after every step with git: stages, markers, work tree, `MERGE_HEAD` / `MERGE_MSG` / `MERGE_MODE`, reflogs, `--abort`, the concluding commit |
| `log` | `--graph` against git's own graph on random DAGs (octopus merges included), `--topo-order` / `--date-order`; `log -- <path>` on random commit graphs whose trees change at random, with `--full-history`, `--first-parent`, `--graph` (parent rewriting) and ranges `A..B` with out-of-order dates (git's answer depends on how far the "uninteresting" mark has spread when a merge is looked at, so `limit_list` itself is ported); `-p`, `--stat`, `-m`, `--follow` (renames *and* copies) on random histories |
| Combined diffs | `show` / `log --cc` / `-c` / `-m` of merge commits whose results mix the parents' lines at random (trailing deletions, deleted files, mode changes), and `diff` / `--cached` / `--ours` / `--theirs` / `--name-status` of conflicted work trees after hand edits: byte for byte |
| Renames | `diff`, `show`, `log -p`, `--stat`, `--numstat`, `--name-status`, `--summary`, `-M<n>` and the staged part of `status` on 60 random rename-heavy histories; merges that move a file on one side and change it on the other (rename/modify, rename/delete, both sides, mode changes) compared after every step; a directory rename, which git handles by moving new files along, is *refused* instead of guessed |
| Remotes | clone (also `-b`, `--bare`, empty and detached-HEAD sources), fetch (new branches and tags, `--prune`, forced updates, refspecs, `--all`, a bare URL), pull (fast-forward, merge, conflict, `--ff-only`), push (new branch, fast-forward, `fetch first` / non-fast-forward refusals with git's hints, `--force`, `:ref` deletes, `--tags`, `-u`, a checked-out branch refused) and the tracking commands (`branch -vv`, `status -sb`, `@{u}`, `checkout <remote branch>`) against git **over four transports**: a path, `file://`, `git://` served by a real `git daemon` and smart HTTP served by a real `git http-backend`. After every step refs, reflogs, config, `FETCH_HEAD`, index, work tree and `status` must agree, the progress lines match, and `git fsck --strict` must pass on what this program cloned and uploaded |
| Packs | real packs with OFS deltas (`gc --aggressive`) and thin REF-delta packs are received and indexed; the packs written for push are accepted by `git index-pack --strict` |
| Whole sessions | random `add` / `rm` / `commit` / `branch` / `checkout` / `reset` / `merge` / `tag` sessions run in lock step in two repositories, one driven through this package and one through git: after **every step** HEAD, refs, index, work tree and `status` must be identical, and a refusal by one must be a refusal by the other (the suite runs 8 such sessions; longer runs of about 90 plain sessions of 45 steps and 24 branching sessions of 40 rounds each were made on earlier revisions while the divergences below were being found and fixed, and a last round of 12 fresh branching sessions and 16 fresh plain ones passed; that round found one more divergence, `add -u` on an ignored path, which is fixed and covered by a test. Since then merges in those sessions conflict for real and rename detection is live on both sides; 12 fresh branching sessions passed with that, and the sessions also check the reflogs of every ref) |
| `fsck` | missing, truncated, bit-flipped, mis-named objects, damaged packs, malformed trees/commits/tags, broken refs: the verdict and the culprit ids |
| Real repositories | `tools/real_world_sweep.py`: 99 + 19 + 1 real repositories, 0 disagreements (below) |

`kgit dogrulama` ("verification") packs a quick version of this into the program itself: known answers computed with real git, then a differential battery
(objects both ways, packs, index, sessions, commit ids, history order, patches, ignore decisions, merges, renames, combined diffs, a clone/fetch/push round trip,
corruption detection, refs and config) on the git installed on your machine. It is also the last step of CI.

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

* Git's behaviour moves between versions, and the oracle caught it twice: `merge.renames=false` is honoured by `git merge` only from newer releases (2.39 ignores it), so the
  merge tests now run with rename detection on in both programs; and a new git expires 2023-dated reflog entries after a commit (the tests switch the maintenance off).
* `git log A..B -- path` hides a different set of commits depending on *when* the walk learns that a parent is uninteresting: a merge whose old parent was not yet marked
  still counts as TREESAME to it. Only a port of `limit_list` reproduces that; the simple rule ("TREESAME to a relevant parent") does not.
* Combined diffs have quirks the documentation does not mention: the `index` line comes before the mode lines, the hunk-header comment drops its last character, an empty line
  follows the message of a merge even when nothing is left to show (also with `--oneline`), a deleted work tree file shows a header but no hunks while a deleted path in a commit
  shows its lost lines, and `git diff` prints combined diffs *first* and the "Unmerged path" lines and ordinary patches afterwards.
* `log --follow` does not simplify history at all: it looks at every commit, shows those whose change against the parent touches the file, and a file created as a copy of
  another one (not just a rename) moves the search; deleted and changed files are compared before untouched ones.
* `git fetch` lists the branch to be merged first, pads the ref column to ten characters, and also moves the configured remote-tracking branch of a ref fetched by name
  (without listing it in `FETCH_HEAD`); a push of existing objects still needs an empty pack or the server waits forever.

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

Two network checks on real data: `kgit clone` of this very repository from GitHub over `https://` (protocol v2 through GitHub's own servers; `git fsck` and `git log` agree), and
a bare `kgit clone` of Homebrew's repository (52,340 commits, 362,574 objects) from its path took 4 minutes 8 seconds on that laptop (15 minutes before stored deltas were copied instead of rebuilt), left 156 MB on disk (the source's pack is 145 MB), 533 refs identical to the source, and `git fsck --connectivity-only` finds nothing wrong.

## Limits (deliberate, and stated plainly)

* **Network**: `clone`, `fetch`, `pull` and `push` speak protocol v2 for fetching (servers from git 2.18 on) and the classic `receive-pack` conversation for pushing, over a
  path, `file://`, `git://` and smart `http(s)://`. Not done: `ssh` (no ssh client is started), `--depth` / partial clones, submodules, credential helpers (put `user:token@`
  in the URL), push options, hooks, `pull --rebase`, tag-pushing refinements such as `--follow-tags`, signed pushes. Packs written for a push copy the repository's stored
  deltas but are not thin. Pulling more than one branch (octopus) is refused.
* **Renames**: detected in `diff` / `show` / `log` / `status` (not with `-B`; copies only in `log --follow`), with the default 50% threshold (`-M<n>` changes it) and a fixed
  limit of 1000 files; `diff.renames` and `merge.renames` are not read (use `--no-renames`). In a merge a file moved on one side and changed on the other is followed (also
  rename/delete and both sides renaming); **directory renames** (git moves files that the other side added to a renamed directory), renames that collide with another
  change and renames on both sides to different names *with* edits are refused before anything is touched.
* **Merging**: more than one merge base (criss-cross) is refused; symlink, submodule, file-versus-directory and mode-versus-mode conflicts are refused; the merge needs an index
  equal to `HEAD`; an octopus merge is not done.
* **No hooks**; no `.gitattributes`, filters, `autocrlf` or LFS; no submodule operations (gitlinks are carried along); no sparse checkout; no SHA-256 repositories; index versions
  2-4 are read (and rewritten as version 2), the split index is not; no `rebase`, `stash`, `cherry-pick`, `blame`, `bisect`; pathspec magic (`:(glob)`, `:!x`) is refused.
* `commit` needs `-m` (no editor is started); `include`/`includeIf` in config files are not followed; `log` has no `--author` / `--grep` / date filters.
* `log --graph` draws what git draws except in very wide graphs where several lines collapse over a long distance in one row (the `|_|/` forms): there the pictures can differ,
  23 of 24 random graphs and every medium-sized one are identical.
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
