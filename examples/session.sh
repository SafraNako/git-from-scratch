#!/bin/sh
# A first session with the kgit command line, in a throw-away directory.
set -e
dir=$(mktemp -d)
cd "$dir"
export GIT_CONFIG_GLOBAL=/dev/null

kgit init demo
cd demo
kgit config user.name "Ada Lovelace"
kgit config user.email ada@example.org

printf 'hello\nworld\n' > greeting.txt
kgit add greeting.txt
kgit commit -m "First commit"

kgit branch topic
kgit checkout topic
printf 'hello\nbrave new world\n' > greeting.txt
kgit diff
kgit commit -a -m "Change the greeting"

kgit checkout main
kgit merge topic
kgit log --oneline
kgit fsck

# the repository is plain git: any git can read it
if command -v git >/dev/null 2>&1; then git log --oneline --graph; fi
