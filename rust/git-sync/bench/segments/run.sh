#!/usr/bin/env bash
# Build the segment bench's data, and run the design's queries against it.
# The store's own reads are timed by src/db/bench.rs; see README.md.
set -euo pipefail

usage() {
    cat >&2 <<'EOF'
usage: run.sh MODE [var=value ...]

  generate  recreate $BENCH_DB from migrations/postgres and generate.sql,
            then copy it to SQLite. var=value are generate.sql's parameters.
  queries   run queries.sql, the design's hand-written SQL, against $BENCH_DB
  all       generate, then queries
  sqlite    copy $BENCH_DB to SQLite again, without regenerating it

To time what the store actually runs, you probably want these instead of
queries, after generate (from rust/git-sync):

  cargo test --features postgres --lib bench_segments_pg -- --ignored --nocapture
  cargo test --lib bench_segments_sqlite -- --ignored --nocapture

$BENCH_PG_URL defaults to the unfurl-pg-jit container (postgres:17, JIT on),
whose planner costs are what production sees. The SQLite copy goes to
$BENCH_SQLITE, or unfurl_segments_bench.db in the temp directory.
EOF
    exit 1
}

url=${BENCH_PG_URL:-postgres://postgres:pgjit@127.0.0.1:55432}
db=${BENCH_DB:-unfurl_segments_bench}
here=$(cd "$(dirname "$0")" && pwd)

generate() {
    psql "$url/postgres" -qX -c "DROP DATABASE IF EXISTS $db" -c "CREATE DATABASE $db"
    for m in "$here"/../../migrations/postgres/*.sql; do
        psql "$url/$db" -qX -1 -v ON_ERROR_STOP=1 -f "$m"
    done
    psql "$url/$db" -qX -v ON_ERROR_STOP=1 "$@" -f "$here/generate.sql"
    copy_to_sqlite
}

queries() {
    psql "$url/$db" -X -v ON_ERROR_STOP=1 -f "$here/queries.sql"
}

copy_to_sqlite() {
    python3 "$here/to_sqlite.py"
}

[[ $# -ge 1 ]] || usage
mode=$1
shift
vars=()
for a in "$@"; do vars+=(-v "$a"); done
case $mode in
    generate | all) ;;
    queries | sqlite) [[ $# -eq 0 ]] || usage ;;
    *) usage ;;
esac

case $mode in
    generate) generate ${vars[@]+"${vars[@]}"} ;;
    queries) queries ;;
    all) generate ${vars[@]+"${vars[@]}"} && queries ;;
    sqlite) copy_to_sqlite ;;
esac
