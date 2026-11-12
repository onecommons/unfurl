// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Which untracked files a scan takes in where `.gitignore` excludes some,
//! the same with a repository and without one
//! (docs/untracked-and-gitless.md, "Ignored files, in both modes").

use std::collections::BTreeSet;
use std::path::Path;

use tempfile::TempDir;
use unfurl_git_sync::{
    git, DbConfig, FormatRegistry, OpenOptions, RecordQuery, ScanOptions, SyncedRepo,
};

#[derive(Clone, Copy, Debug)]
enum Mode {
    /// A repository with one commit, of a file outside the pathspecs.
    Repository,
    /// No repository.
    Gitless,
}

const MODES: [Mode; 2] = [Mode::Repository, Mode::Gitless];

fn cloudmap(key: &str) -> String {
    format!("apiVersion: unfurl/v1.0.0\nkind: CloudMap\nrepositories:\n  git://e.com/{key}.git:\n    path: {key}\n")
}

/// A directory holding `files`, each a cloudmap, plus `.gitignore` files
/// as given, in a repository for [`Mode::Repository`].
fn setup(mode: Mode, files: &[&str], ignores: &[(&str, &str)]) -> TempDir {
    let tmp = tempfile::tempdir().expect("tempdir");
    if let Mode::Repository = mode {
        git::init_with_files(
            tmp.path(),
            &[("README".to_string(), b"r\n".to_vec())],
            "initial",
        )
        .expect("init");
    }
    for file in files {
        write(tmp.path(), file, &cloudmap(&file.replace(['/', '.'], "-")));
    }
    for (path, text) in ignores {
        write(tmp.path(), path, text);
    }
    tmp
}

fn write(root: &Path, rel: &str, text: &str) {
    let path = root.join(rel);
    std::fs::create_dir_all(path.parent().expect("parent")).expect("mkdir");
    std::fs::write(path, text).expect("write");
}

async fn open(root: &Path, pathspecs: &[&str]) -> SyncedRepo {
    static NEXT: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
    let n = NEXT.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    // beside the files, outside every pathspec here
    let url = format!(
        "sqlite://{}?mode=rwc",
        root.join(format!("sync-{n}.db")).display()
    );
    SyncedRepo::open_with(
        root,
        DbConfig::Sqlite { url },
        FormatRegistry::with_builtins(),
        git::Trust::Full,
        OpenOptions::default()
            .pathspecs(pathspecs.iter().copied())
            .untracked(true)
            .without_repository(true),
    )
    .await
    .expect("open")
}

/// The files a scan of `root` with `pathspecs` takes records in from.
async fn taken_in(root: &Path, pathspecs: &[&str]) -> BTreeSet<String> {
    let sync = open(root, pathspecs).await;
    files(&sync).await
}

async fn files(sync: &SyncedRepo) -> BTreeSet<String> {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    sync.find_records(&RecordQuery::default())
        .await
        .expect("find")
        .into_iter()
        .map(|r| r.file_path)
        .collect()
}

fn set(files: &[&str]) -> BTreeSet<String> {
    files.iter().map(|f| f.to_string()).collect()
}

#[tokio::test]
async fn a_literal_pathspec_takes_in_an_ignored_file() {
    for mode in MODES {
        let tmp = setup(
            mode,
            &["local/cloudmap.yaml"],
            &[(".gitignore", "local/\n")],
        );
        let got = taken_in(tmp.path(), &["local/cloudmap.yaml"]).await;
        assert_eq!(got, set(&["local/cloudmap.yaml"]), "{mode:?}");
    }
}

#[tokio::test]
async fn a_wildcard_pathspec_skips_an_ignored_file() {
    for mode in MODES {
        let tmp = setup(
            mode,
            &["maps/a.yaml", "maps/b.yaml"],
            &[(".gitignore", "b.yaml\n")],
        );
        for spec in ["maps/", "maps/*.yaml"] {
            let got = taken_in(tmp.path(), &[spec]).await;
            assert_eq!(got, set(&["maps/a.yaml"]), "{mode:?} {spec}");
        }
    }
}

#[tokio::test]
async fn a_nested_gitignore_applies_below_it() {
    for mode in MODES {
        let tmp = setup(
            mode,
            &["maps/x.yaml", "maps/sub/x.yaml", "maps/sub/y.yaml"],
            &[("maps/sub/.gitignore", "x.yaml\n")],
        );
        let got = taken_in(tmp.path(), &["maps/"]).await;
        assert_eq!(got, set(&["maps/sub/y.yaml", "maps/x.yaml"]), "{mode:?}");
    }
}

#[tokio::test]
async fn a_negated_pattern_keeps_a_file() {
    for mode in MODES {
        let tmp = setup(
            mode,
            &["maps/drop.yaml", "maps/keep.yaml"],
            &[(".gitignore", "maps/*.yaml\n!maps/keep.yaml\n")],
        );
        let got = taken_in(tmp.path(), &["maps/"]).await;
        assert_eq!(got, set(&["maps/keep.yaml"]), "{mode:?}");
    }
}

/// `.git/info/exclude` isn't read by the untracked walk, so a repository
/// takes in what the same directory without one does.
#[tokio::test]
async fn info_exclude_is_not_read() {
    let tmp = setup(Mode::Repository, &["maps/a.yaml"], &[]);
    write(tmp.path(), ".git/info/exclude", "maps/\n");
    assert_eq!(
        taken_in(tmp.path(), &["maps/"]).await,
        set(&["maps/a.yaml"])
    );
}

/// A file the index has is taken in whatever `.gitignore` says.
#[tokio::test]
async fn a_tracked_file_is_unaffected() {
    let tmp = setup(
        Mode::Repository,
        &["maps/a.yaml"],
        &[(".gitignore", "maps/\n")],
    );
    git::commit_paths(
        &gix::open(tmp.path()).expect("repo"),
        &["maps/a.yaml".to_string()],
        "add",
    )
    .expect("commit");
    assert_eq!(
        taken_in(tmp.path(), &["maps/"]).await,
        set(&["maps/a.yaml"])
    );
}

/// The same directory takes in the same files before and after `git init`,
/// with nothing staged.
#[tokio::test]
async fn git_init_changes_nothing() {
    let tmp = setup(
        Mode::Gitless,
        &[
            "maps/a.yaml",
            "maps/b.yaml",
            "maps/sub/c.yaml",
            "local/cloudmap.yaml",
        ],
        &[
            (".gitignore", "b.yaml\nlocal/\n"),
            ("maps/sub/.gitignore", "*\n!c.yaml\n"),
        ],
    );
    let specs = ["maps/", "local/cloudmap.yaml"];
    let before = taken_in(tmp.path(), &specs).await;
    let out = std::process::Command::new("git")
        .args(["init", "-q", "-b", "main"])
        .current_dir(tmp.path())
        .output()
        .expect("git");
    assert!(out.status.success(), "{out:?}");
    let after = taken_in(tmp.path(), &specs).await;
    assert_eq!(before, after);
    assert_eq!(
        before,
        set(&["local/cloudmap.yaml", "maps/a.yaml", "maps/sub/c.yaml"])
    );
}

/// A file taken in that `.gitignore` then excludes, matched only by a
/// wildcard, is gone like one that left the index.
#[tokio::test]
async fn a_file_that_becomes_ignored_is_an_orphan() {
    for mode in MODES {
        let tmp = setup(mode, &["maps/a.yaml", "maps/b.yaml"], &[]);
        let sync = open(tmp.path(), &["maps/"]).await;
        assert_eq!(
            files(&sync).await,
            set(&["maps/a.yaml", "maps/b.yaml"]),
            "{mode:?}"
        );
        write(tmp.path(), ".gitignore", "b.yaml\n");
        assert_eq!(files(&sync).await, set(&["maps/a.yaml"]), "{mode:?}");
    }
}

/// A symlink to a file is taken in, as the file it names; a symlinked
/// directory isn't entered.
#[tokio::test]
async fn symlinks() {
    for mode in MODES {
        let tmp = setup(
            mode,
            &["elsewhere/real.yaml", "elsewhere/dir/inner.yaml"],
            &[],
        );
        std::fs::create_dir(tmp.path().join("maps")).expect("mkdir");
        std::os::unix::fs::symlink("../elsewhere/real.yaml", tmp.path().join("maps/link.yaml"))
            .expect("symlink");
        std::os::unix::fs::symlink("../elsewhere/dir", tmp.path().join("maps/linked"))
            .expect("symlink");
        assert_eq!(
            taken_in(tmp.path(), &["maps/"]).await,
            set(&["maps/link.yaml"]),
            "{mode:?}"
        );
    }
}
