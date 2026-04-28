# Test fixtures

`expected_cloudmap.yaml` is a one-shot manual copy of
`/Users/adam/_dev/unfurl/tests/fixtures/expected_cloudmap.yaml` (i.e.
the cloudmap fixture used by `tests/test_cloudmap.py`). Re-vendor by
re-copying when the upstream fixture changes — we deliberately do not
symlink across crates so this crate stays self-contained.
