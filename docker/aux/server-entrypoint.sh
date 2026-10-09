#!/bin/bash
# The unfurl server: unfurl-server (Rust) on UNFURL_HOST:UNFURL_PORT, in front
# of the Python backend (gunicorn) on 127.0.0.1:UNFURL_BACKEND_PORT.
# UNFURL_RUST_SERVER=0 runs gunicorn alone on UNFURL_HOST:UNFURL_PORT.
# Arguments are passed to gunicorn.
set -euo pipefail

host="${UNFURL_HOST:-0.0.0.0}"
port="${UNFURL_PORT:-5000}"
backend_port="${UNFURL_BACKEND_PORT:-5001}"
workers="${NUM_WORKERS:-4}"

if [ "${UNFURL_RUST_SERVER:-}" = "0" ]; then
    exec gunicorn -b "${host}:${port}" -w "${workers}" unfurl.server.serve:app "$@"
fi

export UNFURL_HOST="$host" UNFURL_PORT="$port"
export UNFURL_BACKEND_URL="http://127.0.0.1:${backend_port}"
# part of the ETag both servers compute, so it must be the backend's
if [ -z "${UNFURL_PACKAGE_DIGEST:-}" ]; then
    UNFURL_PACKAGE_DIGEST="$(python -c 'from unfurl.util import get_package_digest; print(get_package_digest())')" ||
        echo "warning: couldn't read the package digest; ETags won't match the backend's" >&2
fi
export UNFURL_PACKAGE_DIGEST="${UNFURL_PACKAGE_DIGEST:-}"
export RUST_LOG="${RUST_LOG:-info}"

gunicorn -b "127.0.0.1:${backend_port}" -w "${workers}" unfurl.server.serve:app "$@" &
unfurl-server &

# when either exits, exit with its status so the container stops
wait -n
exit $?
