docker run --rm -it -u $(id -u):$(id -g) -w /data -v "$(pwd)":/data \
    -e UNFURL_TEST_SKIP=terraform+k8s+helm -e UNFURL_LOGGING=debug \
    --name unfurltoxtest -v /var/run/docker.sock:/var/run/docker.sock \
    unfurl_tox:latest tox -e py38-docker "$@"
