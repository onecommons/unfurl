# Test fixtures

`expected_cloudmap.yaml` is a copy of
`tests/fixtures/expected_cloudmap.yaml` in this git repository. It isn't particularly important to keep in sync with that file, but if it is updated with the latest, `expected_cloudmap_after_save.yaml` will have to be manually updated too for the tests in `test_crud.rs` to pass.

`literate_cloudmap.md` is a cloudmap written as prose, and
`literate_cloudmap_after_save.md` is what `test_markdown.rs` expects a
save to produce from it. Regenerate the second with
`UPDATE_FIXTURES=1 cargo test -p unfurl-git-sync --test test_markdown`,
and read the diff: it is the only end-to-end record of where the
renderer puts an edit, so an unexpected line moving is the finding, not
noise to accept.
