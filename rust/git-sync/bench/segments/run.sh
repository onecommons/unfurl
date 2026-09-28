#!/usr/bin/env bash
# Build a synthetic segments database and explain the design's queries.
#
#   run.sh [generate|queries|all] [psql-var=value ...]
#
# `generate` recreates $BENCH_DB and loads schema.sql and generate.sql,
# passing each var=value to psql (see generate.sql for the parameters);
# `queries` runs queries.sql against it; `all` (the default) does both.
#
# BENCH_PG_URL defaults to the unfurl-pg-jit container (postgres:17, JIT
# on), whose planner costs are what production sees.
set -euo pipefail

url=${BENCH_PG_URL:-postgres://postgres:pgjit@127.0.0.1:55432}
db=${BENCH_DB:-unfurl_segments_bench}
here=$(cd "$(dirname "$0")" && pwd)

mode=all
case ${1:-} in generate | queries | all) mode=$1; shift ;; esac
vars=()
for a in "$@"; do vars+=(-v "$a"); done

if [[ $mode != queries ]]; then
    psql "$url/postgres" -qX -c "DROP DATABASE IF EXISTS $db" -c "CREATE DATABASE $db"
    psql "$url/$db" -qX -v ON_ERROR_STOP=1 -f "$here/schema.sql"
    psql "$url/$db" -qX -v ON_ERROR_STOP=1 ${vars[@]+"${vars[@]}"} -f "$here/generate.sql"
fi
if [[ $mode != generate ]]; then
    psql "$url/$db" -X -v ON_ERROR_STOP=1 -f "$here/queries.sql"
fi
