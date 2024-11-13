set -e
.tox/${1:-py310}/bin/mypy unfurl --install-types --non-interactive
UNFURL_TEST_SKIP_BUILD_RUST=1 UNFURL_TEST_SKIP=docker+slow+k8s+helm+$UNFURL_TEST_SKIP tox --skip-pkg-install -e ${1:-py310} -- -v --no-cov -n auto --dist loadfile $2 $3 $4 $5 $6 $7
