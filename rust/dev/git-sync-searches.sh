#!/usr/bin/env bash
# The segments harness's random searches NAME..., as test names, one a
# line, from the harness's `--list --ignored` on stdin; all of them, with
# no NAME. A NAME is a test's name without `_agree(s)_with_the_implementation`.
# Fails, naming it, on a NAME that matches no search.
#
#   segments_sql --list --ignored | rust/dev/git-sync-searches.sh [NAME...]
set -uo pipefail

all=$(sed -En 's/^(.+_agrees?_with_the_implementation): test$/\1/p')
if [ $# -eq 0 ]; then
    echo "$all"
    exit 0
fi
status=0
for name in "$@"; do
    # `rebases_agree_...` but `same_content_agrees_...`
    if ! grep -E "^${name}_agrees?_with_the_implementation$" <<<"$all"; then
        echo "no random search named $name" >&2
        status=1
    fi
done
exit $status
