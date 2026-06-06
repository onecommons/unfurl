#!/usr/bin/env bash
# Workspace-level coverage + CRAP-quality report.
#
# Pipeline:
#   1. cargo llvm-cov --no-default-features --workspace  → lcov
#   2. lcov-merge-monomorphizations.awk                  → de-phantom'd lcov
#   3. cargo crap --workspace                            → human-readable report
#
# Generated files land in rust/target/ (gitignored).
#
# Excludes:
#   - server/src/unfurl_types.rs is generated from the OpenAPI spec via
#     build.rs; manual tests would just rot on regeneration. Skip it.
#   - build.rs scripts run at compile time, not from tests — their CRAP
#     score is meaningless.
#   - test_*: test functions themselves — CRAP measures risk of untested
#     production code, so tests reporting as "uncovered" is noise.
#   - dump_*: debug/diagnostic dumpers tend to be branchy and lightly
#     exercised; they're not on a real risk surface.
#
# Modes:
#   default     — build with `-C instrument-coverage`, run the cargo test
#                 suite, then report. Use this locally for a one-shot run.
#   --report-only — assume `.profraw` files already exist under target/
#                 (e.g. produced by an earlier `cargo llvm-cov show-env`
#                 + tox + cargo test sequence in CI), skip the build/run
#                 step, and go straight to lcov export + crap.
#
# Args: anything after `--report-only` (or all args in default mode) is
# passed through to cargo crap. Defaults to `--top 30` when none are given.
# Examples:
#   ./rust/dev/coverage.sh
#   ./rust/dev/coverage.sh --top 50
#   ./rust/dev/coverage.sh --report-only --fail-above
#
# Env (optional):
#   UNFURL_TEST_REDIS_URL   — set to also exercise Redis-gated server and
#                             git-sync integration tests; otherwise those
#                             tests early-return and the corresponding
#                             functions stay at 0% coverage.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RAW_LCOV="$WORKSPACE_ROOT/target/lcov.raw.info"
CLEAN_LCOV="$WORKSPACE_ROOT/target/lcov.clean.info"

REPORT_ONLY=0
if [ "${1:-}" = "--report-only" ]; then
    REPORT_ONLY=1
    shift
fi

cd "$WORKSPACE_ROOT"

if [ "$REPORT_ONLY" -eq 1 ]; then
    echo "==> cargo llvm-cov report (merging existing .profraw files)"
    cargo llvm-cov report --lcov --output-path "$RAW_LCOV"
else
    echo "==> cargo llvm-cov --workspace --no-default-features"
    cargo llvm-cov --lcov --output-path "$RAW_LCOV" --no-default-features --workspace
fi

echo "==> filtering monomorphization phantoms via awk"
awk -f "$SCRIPT_DIR/lcov-merge-monomorphizations.awk" "$RAW_LCOV" > "$CLEAN_LCOV"

if [ "$#" -eq 0 ]; then
    set -- --top 30
fi

echo "==> cargo crap (excluding generated code)"
# `--exclude` globs are matched per-crate under `--workspace`, so the
# pattern needs to be `**/` rather than a workspace-rooted path.
cargo crap \
    --workspace \
    --lcov "$CLEAN_LCOV" \
    --exclude '**/unfurl_types.rs' \
    --exclude '**/build.rs' \
    --allow 'test_*' \
    --allow 'dump_*' \
    "$@"
