#!/bin/bash

set -eux

# start nginx
nginx -g "daemon off;" &

# start unfurl-server, and the Python backend behind it, as the unfurl user;
# nginx proxies to it on 5002
gosu unfurl:unfurl env UNFURL_HOST=127.0.0.1 UNFURL_PORT=5002 UNFURL_BACKEND_PORT=5001 \
    /usr/local/bin/server-entrypoint.sh "$@" &

# wait for any process to exit
wait -n

# exit with status of process that exited first
exit $?
