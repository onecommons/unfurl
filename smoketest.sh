set -e
.tox/${1:-py39}/bin/mypy unfurl
UNFURL_TEST_SKIP=docker+slow+k8s+helm+$UNFURL_TEST_SKIP tox --skip-pkg-install -e ${1:-py39} -- -v --no-cov -n auto --dist loadfile $2 $3 $4 $5 $6 $7
