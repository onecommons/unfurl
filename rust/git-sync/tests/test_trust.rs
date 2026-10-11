// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Reduced trust holds under a global `safe.directory` that matches the
//! checkout, which gix otherwise reads as trusting it fully. Its own test
//! binary, since it sets this process's git config environment.

use std::path::Path;
use unfurl_git_sync::git::{worktree_meta, Opener, Trust};

fn git(dir: &Path, args: &[&str]) {
    let out = std::process::Command::new("git")
        .args(args)
        .current_dir(dir)
        .output()
        .expect("git");
    assert!(out.status.success(), "{out:?}");
}

#[test]
fn reduced_trust_holds_for_a_safe_directory() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let global = tmp.path().join("gitconfig");
    std::fs::write(&global, "[safe]\n\tdirectory = *\n").expect("write");
    std::env::set_var("GIT_CONFIG_GLOBAL", &global);

    let checkout = tmp.path().join("checkout");
    std::fs::create_dir(&checkout).expect("mkdir");
    git(&checkout, &["init", "-q"]);
    for (key, value) in [
        ("remote.origin.url", "https://unfurl.cloud/org/proj.git"),
        ("filter.evil.smudge", "touch /tmp/pwned"),
        ("core.hooksPath", "/tmp/hooks"),
    ] {
        git(&checkout, &["config", key, value]);
    }

    let repo = Opener::new(&checkout, Trust::Reduced)
        .and_then(|o| o.open(&checkout))
        .expect("open");
    assert_eq!(
        worktree_meta(&repo).expect("meta").origin,
        "unfurl.cloud/org/proj"
    );
    let config = repo.config_snapshot();
    assert_eq!(
        config.trusted_program("filter.evil.smudge").expect("read"),
        None
    );
    assert_eq!(config.trusted_path("core.hooksPath").expect("read"), None);
}
