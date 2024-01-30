#!/bin/bash

set -eux

# start nginx
nginx -g "daemon off;" &

# start unfurl-server as unfurl user
gosu unfurl:unfurl gunicorn -b=0.0.0.0:5001 -w=${NUM_WORKERS} unfurl.server:app $* &

# wait for any process to exit
wait -n

# exit with status of process that exited first
exit $?
