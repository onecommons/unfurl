#!/usr/bin/env bash
# The checks before a git-sync commit. cargo locks its target directory for
# a whole `cargo test`, so the builds run one at a time and the suites then
# run side by side as test binaries: git-sync's on Postgres and SQLite, the
# segments harness on Postgres, and the server's.
#
#   rust/dev/git-sync-gates.sh                  clippy, check and the suites
#   rust/dev/git-sync-gates.sh search NAME N    NAME's random search N times,
#                                               side by side, as well
#
# Needs UNFURL_TEST_PG_URL; logs go to $GATES_DIR (default a temp dir).
# PROPTEST_CASES sets a search's cases (default 200).
set -uo pipefail

rust=$(cd "$(dirname "$0")/.." && pwd)
out=${GATES_DIR:-$(mktemp -d -t git-sync-gates)}
: "${UNFURL_TEST_PG_URL:?set UNFURL_TEST_PG_URL to a Postgres the tests may use}"
echo "logs in $out"
status=0

step() { echo "== $*"; }
fail() { echo "   FAILED: $*"; status=1; }

# the test binaries `cargo test --no-run` built, from its JSON messages
binaries() {
    python3 -c '
import json, sys
for line in sys.stdin:
    m = json.loads(line)
    if m.get("reason") == "compiler-artifact" and m.get("profile", {}).get("test") and m.get("executable"):
        print(m["executable"])'
}

# one line per suite: tests passed and failed over its binaries' logs
tally() {
    grep -h "^test result" "$@" | awk '{p += $4; f += $6} END {printf "passed %d, failed %d\n", p, f}'
}

step clippy
(cd "$rust" && cargo clippy -q --all-features --all-targets -- -D warnings) > "$out/clippy.txt" 2>&1 ||
    fail "clippy, see $out/clippy.txt"

step "git-sync without its postgres feature builds"
(cd "$rust" && cargo check -q -p unfurl-git-sync --tests) > "$out/check.txt" 2>&1 ||
    fail "cargo check, see $out/check.txt"

step "building the suites"
(cd "$rust" && cargo test -p unfurl-git-sync --features postgres --no-run --message-format=json 2> "$out/build-git-sync.txt") |
    binaries > "$out/git-sync.bins" || fail "git-sync's build, see $out/build-git-sync.txt"
(cd "$rust" && cargo test -p unfurl-server --no-run --message-format=json 2> "$out/build-server.txt") |
    binaries > "$out/server.bins" || fail "the server's build, see $out/build-server.txt"
harness=$(grep '/segments_sql-' "$out/git-sync.bins" | head -1)

step "suites, side by side"
(
    cd "$rust/git-sync" || exit 1
    while read -r bin; do "$bin" > "$out/git-sync-$(basename "$bin").txt" 2>&1; done < "$out/git-sync.bins"
) &
(cd "$rust/git-sync" && SEGMENTS_SQL_PG=1 "$harness" > "$out/harness-pg.txt" 2>&1) &
(
    cd "$rust/server" || exit 1
    while read -r bin; do UNFURL_TEST_PG_URL='' "$bin" > "$out/server-$(basename "$bin").txt" 2>&1; done < "$out/server.bins"
) &
wait
echo "   git-sync, both backends: $(tally "$out"/git-sync-*.txt); Postgres skipped: $(cat "$out"/git-sync-*.txt | grep -c 'skip: UNFURL_TEST_PG_URL')"
echo "   harness on Postgres:     $(tally "$out/harness-pg.txt")"
echo "   server:                  $(tally "$out"/server-*.txt)"
grep -h "^test .* FAILED$" "$out"/git-sync-*.txt "$out/harness-pg.txt" "$out"/server-*.txt | sed 's/^/   /'
grep -qh "^test result: FAILED" "$out"/git-sync-*.txt "$out/harness-pg.txt" "$out"/server-*.txt && status=1

step doctests
(cd "$rust" && cargo test -q -p unfurl-git-sync --features postgres --doc) > "$out/doc.txt" 2>&1 ||
    fail "doctests, see $out/doc.txt"

if [ "${1:-}" = search ]; then
    name=$2 runs=$3
    if ! test=$("$harness" --list --ignored 2>/dev/null | "$rust/dev/git-sync-searches.sh" "$name"); then
        fail "no random search named $name"
        exit $status
    fi
    regressions="$rust/git-sync/tests/segments_sql.proptest-regressions"
    cp "$regressions" "$out/regressions.before"
    step "$name's random search, $runs times side by side"
    for i in $(seq "$runs"); do
        (cd "$rust/git-sync" && PROPTEST_CASES=${PROPTEST_CASES:-200} "$harness" --ignored --exact \
            "$test" > "$out/search-$i.txt" 2>&1) &
    done
    wait
    for i in $(seq "$runs"); do
        echo "   run $i: $(grep -hE "^test $test \.\.\. (ok|FAILED)$" "$out/search-$i.txt" | sed 's/.* //')"
    done
    if ! cmp -s "$out/regressions.before" "$regressions"; then
        cp "$regressions" "$out/regressions.after"
        cp "$out/regressions.before" "$regressions"
        fail "new seeds, in $out/regressions.after"
    fi
fi
exit $status
