#!/usr/bin/env bash
# Clippy, the git-sync and server suites, the segments harness's saved
# seeds and its random histories, on SQLite and (with
# UNFURL_TEST_PG_URL set) Postgres, in parallel.
#
# One `--features postgres` build serves both backends: which one a test
# uses is decided at run time, by UNFURL_TEST_PG_URL for the crud-style
# tests (the ::pg variants only run with it set) and by SEGMENTS_SQL_PG
# for the harness. Clippy without features checks the build CI's SQLite
# job makes.
#
# Suites get RUST_TEST_THREADS=5 each so the two streams share the cores;
# each random run is one test on one thread, so all of them run at once.
# Random runs replay no saved seeds (PROPTEST_DISABLE_FAILURE_PERSISTENCE):
# a known failure there would stop them before they generate anything. A
# new failure still prints its minimal history; pin it in
# git-sync/tests/segments/histories.rs.
#
# Usage: rust/dev/verify.sh [cases]   (random cases per run, default 100)
# Logs land in rust/target/verify/.

set -uo pipefail
cd "$(dirname "$0")/.."
CASES=${1:-100}
LOG=target/verify
rm -rf "$LOG" && mkdir -p "$LOG"
PG=${UNFURL_TEST_PG_URL:-}

summary() {
  grep -E "test result|FAILED|panicked|shrinks|minimal failing" "$1" |
    grep -v "ok. 0 passed" | sort | uniq -c | cut -c1-400
}

echo "== clippy"
cargo clippy --all --workspace --all-targets 2>&1 | grep -E "^(warning|error)" -A6 | head -20

echo "== build"
cargo test -p unfurl-git-sync -p unfurl-server --features unfurl-git-sync/postgres,unfurl-server/postgres --no-run -q 2>&1 |
  grep -E "^error" -A5

echo "== suites"
logs=(suites)
(
  export RUST_TEST_THREADS=5 UNFURL_TEST_PG_URL=$PG
  [ -n "$PG" ] || unset UNFURL_TEST_PG_URL
  cargo test -p unfurl-git-sync -p unfurl-server --features unfurl-git-sync/postgres,unfurl-server/postgres --no-fail-fast \
    > "$LOG/suites.log" 2>&1
) &
if [ -n "$PG" ]; then
  logs+=(pg-harness)
  (
    export RUST_TEST_THREADS=5 UNFURL_TEST_PG_URL=$PG SEGMENTS_SQL_PG=1
    cargo test -p unfurl-git-sync --features postgres --test segments_sql \
      > "$LOG/pg-harness.log" 2>&1
  ) &
fi
wait
for f in "${logs[@]}"; do echo "-- $f"; summary "$LOG/$f.log"; done

echo "== random ($CASES cases each)"
export PROPTEST_CASES=$CASES PROPTEST_DISABLE_FAILURE_PERSISTENCE=1
random() {
  local backend=$1 t=$2
  if [ "$backend" = pg ]; then
    SEGMENTS_SQL_PG=1 UNFURL_TEST_PG_URL=$PG \
      cargo test -p unfurl-git-sync --features postgres --test segments_sql -- --ignored "$t"
  else
    env -u UNFURL_TEST_PG_URL -u SEGMENTS_SQL_PG \
      cargo test -p unfurl-git-sync --features postgres --test segments_sql -- --ignored "$t"
  fi > "$LOG/random-$backend-$t.log" 2>&1
}
tests=(rebases_agree one_worktree_agrees segments_agree publishing_agrees)
for t in "${tests[@]}"; do random sqlite "$t" & done
if [ -n "$PG" ]; then
  for t in "${tests[@]}"; do random pg "$t" & done
fi
wait
for f in "$LOG"/random-*.log; do echo "-- $(basename "$f" .log)"; summary "$f"; done
