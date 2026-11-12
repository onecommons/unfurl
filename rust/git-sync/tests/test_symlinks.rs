// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Symlinks: one to a regular file inside the root stands for that file,
//! which is read, written and committed in its place; any other is refused
//! (docs/untracked-and-gitless.md).

mod common;

use std::os::unix::fs::symlink;
use std::path::Path;

use common::git;
use serde_json::json;
use tempfile::TempDir;
use unfurl_git_sync::{DbConfig, FormatRegistry, OpenOptions, RecordQuery, ScanOptions, SyncedDir};

const REPOS: &str = "/repositories";
const A: &str = "git://e.com/a.git";
const LINK: &str = "maps/link.yaml";
const REAL: &str = "real/cloudmap.yaml";

#[derive(Clone, Copy, Debug)]
enum Mode {
    Repository,
    Gitless,
}

const MODES: [Mode; 2] = [Mode::Repository, Mode::Gitless];

fn cloudmap(path: &str) -> String {
    format!("apiVersion: unfurl/v1.0.0\nkind: CloudMap\nrepositories:\n  {A}:\n    path: {path}\n")
}

fn write(root: &Path, rel: &str, text: &str) {
    let path = root.join(rel);
    std::fs::create_dir_all(path.parent().expect("parent")).expect("mkdir");
    std::fs::write(path, text).expect("write");
}

fn link(root: &Path, rel: &str, to: &str) {
    let path = root.join(rel);
    std::fs::create_dir_all(path.parent().expect("parent")).expect("mkdir");
    symlink(to, path).expect("symlink");
}

/// `<tmp>/root`, a repository for [`Mode::Repository`], holding `real/cloudmap.yaml`
/// and `maps/link.yaml` naming it.
fn setup(mode: Mode) -> TempDir {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path().join("root");
    std::fs::create_dir(&root).expect("mkdir");
    if let Mode::Repository = mode {
        unfurl_git_sync::git::init_with_files(
            &root,
            &[("README".to_string(), b"r\n".to_vec())],
            "initial",
        )
        .expect("init");
    }
    write(&root, REAL, &cloudmap("a"));
    link(&root, LINK, "../real/cloudmap.yaml");
    tmp
}

async fn open(root: &Path, pathspecs: &[&str]) -> unfurl_git_sync::Result<SyncedDir> {
    let db = format!("sqlite://{}?mode=rwc", root.join("../sync.db").display());
    SyncedDir::open(
        root,
        DbConfig::Sqlite { url: db },
        FormatRegistry::with_builtins(),
        unfurl_git_sync::git::Trust::Full,
        OpenOptions::default()
            .pathspecs(pathspecs.iter().copied())
            .untracked(true),
    )
    .await
}

async fn scan(sync: &SyncedDir) -> unfurl_git_sync::SyncOutcome {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan")
}

/// `(file, key)` of every record.
async fn records(sync: &SyncedDir) -> Vec<(String, String)> {
    sync.find_records(&RecordQuery::default())
        .await
        .expect("find")
        .into_iter()
        .map(|r| (r.file_path, r.key))
        .collect()
}

fn is_link(root: &Path, rel: &str) -> bool {
    std::fs::symlink_metadata(root.join(rel))
        .expect("stat")
        .file_type()
        .is_symlink()
}

/// A link to a file the pathspecs take in too is that file: a save changes
/// the file and leaves the link a link.
#[tokio::test]
async fn a_save_through_a_link_writes_its_file() {
    for mode in MODES {
        let tmp = setup(mode);
        let root = tmp.path().join("root");
        let sync = open(&root, &[LINK, REAL]).await.expect("open");
        scan(&sync).await;
        assert_eq!(
            records(&sync).await,
            [(REAL.to_string(), A.to_string())],
            "{mode:?}"
        );
        sync.upsert_record(Some(REAL), REPOS, A, json!({"path": "edited"}), None, false)
            .await
            .expect("upsert");
        let saved = sync.save_changes().await.expect("save");
        assert!(saved.failed.is_empty(), "{mode:?}: {saved:?}");
        assert!(is_link(&root, LINK), "{mode:?}");
        assert_eq!(
            std::fs::read_to_string(root.join(REAL)).expect("read"),
            cloudmap("edited")
        );
    }
}

/// A commit records the file the link names; the link stays a link in the
/// index and the tree.
#[tokio::test]
async fn a_commit_records_the_file_not_the_link() {
    let tmp = setup(Mode::Repository);
    let root = tmp.path().join("root");
    git(&root, &["add", REAL, LINK]);
    git(
        &root,
        &[
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@e",
            "commit",
            "-qm",
            "both",
        ],
    );
    let sync = open(&root, &[LINK, REAL]).await.expect("open");
    scan(&sync).await;
    sync.upsert_record(Some(REAL), REPOS, A, json!({"path": "edited"}), None, false)
        .await
        .expect("upsert");
    let repo = sync.repository().expect("a repository");
    repo.commit_repository("edit", Default::default())
        .await
        .expect("commit");
    let changed = git_out(&root, &["diff", "--name-only", "HEAD~1", "HEAD"]);
    assert_eq!(changed.trim(), REAL);
    assert!(git_out(&root, &["ls-files", "-s", LINK]).starts_with("120000 "));
    assert!(git_out(&root, &["ls-tree", "HEAD", LINK]).starts_with("120000 "));
    assert!(is_link(&root, LINK));
    assert_eq!(git_out(&root, &["status", "--porcelain"]), "");
}

/// A link and the file it names, both matched, are one file.
#[tokio::test]
async fn a_link_and_its_file_are_one() {
    for mode in MODES {
        let tmp = setup(mode);
        let root = tmp.path().join("root");
        let sync = open(&root, &[LINK, REAL]).await.expect("open");
        let outcome = scan(&sync).await;
        assert_eq!(outcome.files_seen, 1, "{mode:?}: {outcome:?}");
        assert_eq!(
            records(&sync).await,
            [(REAL.to_string(), A.to_string())],
            "{mode:?}"
        );
    }
}

/// A link to a file outside the root is neither read nor written through.
#[tokio::test]
async fn a_link_outside_the_root_is_refused() {
    for mode in MODES {
        let tmp = setup(mode);
        let root = tmp.path().join("root");
        write(tmp.path(), "outside/cloudmap.yaml", &cloudmap("outside"));
        link(&root, "maps/out.yaml", "../../outside/cloudmap.yaml");
        let sync = open(&root, &["maps/", REAL]).await.expect("open");
        let outcome = scan(&sync).await;
        assert_eq!(
            records(&sync).await,
            [(REAL.to_string(), A.to_string())],
            "{mode:?}"
        );
        assert!(
            outcome
                .unparsed
                .iter()
                .any(|f| f.file_path == "maps/out.yaml"),
            "{mode:?}: {outcome:?}"
        );
        sync.upsert_record(
            Some("maps/out.yaml"),
            REPOS,
            A,
            json!({"path": "x"}),
            None,
            false,
        )
        .await
        .expect("upsert");
        let saved = sync.save_changes().await.expect("save");
        assert!(
            saved.failed.iter().any(|f| f.file_path == "maps/out.yaml"),
            "{saved:?}"
        );
        assert!(is_link(&root, "maps/out.yaml"));
        assert_eq!(
            std::fs::read_to_string(tmp.path().join("outside/cloudmap.yaml")).expect("read"),
            cloudmap("outside")
        );
    }
}

/// A broken link and a loop are reported, and the scan takes in the rest.
#[tokio::test]
async fn broken_links_dont_stop_a_scan() {
    for mode in MODES {
        let tmp = setup(mode);
        let root = tmp.path().join("root");
        link(&root, "maps/broken.yaml", "nowhere.yaml");
        link(&root, "maps/loop1.yaml", "loop2.yaml");
        link(&root, "maps/loop2.yaml", "loop1.yaml");
        let sync = open(&root, &["maps/", REAL]).await.expect("open");
        let outcome = scan(&sync).await;
        assert_eq!(
            records(&sync).await,
            [(REAL.to_string(), A.to_string())],
            "{mode:?}"
        );
        let refused: Vec<&str> = outcome
            .unparsed
            .iter()
            .map(|f| f.file_path.as_str())
            .collect();
        for path in ["maps/broken.yaml", "maps/loop1.yaml", "maps/loop2.yaml"] {
            assert!(refused.contains(&path), "{mode:?}: {refused:?}");
        }
    }
}

/// A link the index has, to a file it doesn't, isn't followed by a handle
/// that takes in only what the index has: a commit would add the file.
#[tokio::test]
async fn a_tracked_link_to_an_untracked_file_is_refused() {
    let tmp = setup(Mode::Repository);
    let root = tmp.path().join("root");
    git(&root, &["add", LINK]);
    git(
        &root,
        &[
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@e",
            "commit",
            "-qm",
            "link",
        ],
    );
    let db = format!("sqlite://{}?mode=rwc", root.join("../sync.db").display());
    let sync = SyncedDir::open(
        &root,
        DbConfig::Sqlite { url: db },
        FormatRegistry::with_builtins(),
        unfurl_git_sync::git::Trust::Full,
        OpenOptions::default().pathspecs(["maps/"]),
    )
    .await
    .expect("open");
    let outcome = scan(&sync).await;
    assert!(records(&sync).await.is_empty());
    assert!(
        outcome.unparsed.iter().any(|f| f.file_path == LINK),
        "{outcome:?}"
    );
}

/// A link to a file in the root that the pathspecs don't take in is
/// refused: not read, and neither a save nor a commit touches either.
#[tokio::test]
async fn a_link_to_a_file_the_pathspecs_dont_take_is_refused() {
    for mode in MODES {
        let tmp = setup(mode);
        let root = tmp.path().join("root");
        let sync = open(&root, &["maps/"]).await.expect("open");
        let outcome = scan(&sync).await;
        assert!(records(&sync).await.is_empty(), "{mode:?}");
        let refused = outcome.unparsed.iter().find(|f| f.file_path == LINK);
        assert!(
            refused.is_some_and(|f| f.error.to_string().contains(REAL)),
            "{mode:?}: {outcome:?}"
        );
        assert_untouched_through(&sync, &root, LINK, REAL).await;
    }
}

/// A link to a file `.gitignore` keeps out of a wildcard pathspec is
/// refused likewise.
#[tokio::test]
async fn a_link_to_an_ignored_file_is_refused() {
    for mode in MODES {
        let tmp = setup(mode);
        let root = tmp.path().join("root");
        write(&root, "maps/secret.yaml", &cloudmap("secret"));
        write(&root, "maps/.gitignore", "secret.yaml\n");
        link(&root, "maps/to-secret.yaml", "secret.yaml");
        let sync = open(&root, &["maps/*.yaml"]).await.expect("open");
        let outcome = scan(&sync).await;
        assert!(records(&sync).await.is_empty(), "{mode:?}");
        assert!(
            outcome
                .unparsed
                .iter()
                .any(|f| f.file_path == "maps/to-secret.yaml"),
            "{mode:?}: {outcome:?}"
        );
        assert_untouched_through(&sync, &root, "maps/to-secret.yaml", "maps/secret.yaml").await;
    }
}

/// An edit at `link` is never written through it to `target`: the save
/// fails it, and for a repository the commit too, changing nothing.
async fn assert_untouched_through(sync: &SyncedDir, root: &Path, link: &str, target: &str) {
    let before = std::fs::read_to_string(root.join(target)).expect("read");
    sync.upsert_record(Some(link), REPOS, A, json!({"path": "x"}), None, false)
        .await
        .expect("upsert");
    let saved = sync.save_changes().await.expect("save");
    assert!(
        saved.failed.iter().any(|f| f.file_path == link),
        "{saved:?}"
    );
    if let Some(repo) = sync.repository() {
        let head = git_out(root, &["rev-parse", "HEAD"]);
        assert!(repo
            .commit_repository("x", Default::default())
            .await
            .is_err());
        assert_eq!(git_out(root, &["rev-parse", "HEAD"]), head);
    }
    assert!(is_link(root, link));
    assert_eq!(
        std::fs::read_to_string(root.join(target)).expect("read"),
        before
    );
}

/// A link a pathspec names, to a file the pathspecs don't, makes the scan
/// an error that names the file.
#[tokio::test]
async fn a_named_link_to_a_file_not_named_is_an_error() {
    for mode in MODES {
        let tmp = setup(mode);
        let root = tmp.path().join("root");
        let sync = open(&root, &[LINK]).await.expect("open");
        let scanned = sync.update_from_working_dir(ScanOptions::default()).await;
        let message = match &scanned {
            Err(unfurl_git_sync::Error::Other(m)) => m.clone(),
            other => panic!("{mode:?}: {other:?}"),
        };
        assert!(
            message.contains(REAL) && message.contains("name it"),
            "{message}"
        );
    }
}

/// A refused link a pathspec names literally makes the scan an error.
#[tokio::test]
async fn a_named_link_outside_the_root_is_an_error() {
    for mode in MODES {
        let tmp = setup(mode);
        let root = tmp.path().join("root");
        write(tmp.path(), "outside/cloudmap.yaml", &cloudmap("outside"));
        link(&root, "maps/out.yaml", "../../outside/cloudmap.yaml");
        let sync = open(&root, &["maps/out.yaml"]).await.expect("open");
        let scanned = sync.update_from_working_dir(ScanOptions::default()).await;
        assert!(
            matches!(&scanned, Err(unfurl_git_sync::Error::Other(m)) if m.contains("outside the root")),
            "{mode:?}: {scanned:?}"
        );
    }
}

fn git_out(dir: &Path, args: &[&str]) -> String {
    let out = std::process::Command::new("git")
        .args(args)
        .current_dir(dir)
        .output()
        .expect("git");
    assert!(out.status.success(), "git {args:?}: {out:?}");
    String::from_utf8(out.stdout).expect("utf-8")
}

/// The value of `key` in the file `file`.
async fn path_of(sync: &SyncedDir, file: &str) -> serde_json::Value {
    let found = sync
        .find_records(&RecordQuery::default())
        .await
        .expect("find")
        .into_iter()
        .find(|r| r.file_path == file)
        .expect("record");
    found.json["path"].clone()
}

/// A file in a symlinked directory outside the root is neither read nor
/// written, nor is a new one created there.
#[tokio::test]
async fn a_linked_directory_outside_the_root_is_refused() {
    for mode in MODES {
        let tmp = setup(mode);
        let root = tmp.path().join("root");
        write(tmp.path(), "outside/cloudmap.yaml", &cloudmap("outside"));
        link(&root, "ext", "../outside");
        let sync = open(&root, &["ext/cloudmap.yaml", "ext/new.yaml"])
            .await
            .expect("open");
        let scanned = sync.update_from_working_dir(ScanOptions::default()).await;
        assert!(
            matches!(&scanned, Err(unfurl_git_sync::Error::Other(m)) if m.contains("outside the root")),
            "{mode:?}: {scanned:?}"
        );
        assert!(records(&sync).await.is_empty(), "{mode:?}");
        for file in ["ext/cloudmap.yaml", "ext/new.yaml"] {
            sync.upsert_record(Some(file), REPOS, A, json!({"path": "x"}), None, false)
                .await
                .expect("upsert");
        }
        let saved = sync.save_changes().await.expect("save");
        for file in ["ext/cloudmap.yaml", "ext/new.yaml"] {
            assert!(
                saved.failed.iter().any(|f| f.file_path == file),
                "{mode:?}: {saved:?}"
            );
        }
        if let Some(repo) = sync.repository() {
            assert!(repo
                .commit_repository("x", Default::default())
                .await
                .is_err());
        }
        assert_eq!(
            std::fs::read_to_string(tmp.path().join("outside/cloudmap.yaml")).expect("read"),
            cloudmap("outside")
        );
        assert!(!tmp.path().join("outside/new.yaml").exists(), "{mode:?}");
    }
}

/// A file in a symlinked directory naming one in the root is that file
/// when the pathspecs take it in too, and refused when they don't.
#[tokio::test]
async fn a_linked_directory_inside_the_root_is_its_files() {
    for mode in MODES {
        let tmp = setup(mode);
        let root = tmp.path().join("root");
        link(&root, "alias", "real");
        let sync = open(&root, &["alias/cloudmap.yaml", REAL])
            .await
            .expect("open");
        let outcome = scan(&sync).await;
        assert_eq!(outcome.files_seen, 1, "{mode:?}: {outcome:?}");
        assert_eq!(
            records(&sync).await,
            [(REAL.to_string(), A.to_string())],
            "{mode:?}"
        );

        let tmp = setup(mode);
        let root = tmp.path().join("root");
        link(&root, "alias", "real");
        let sync = open(&root, &["alias/cloudmap.yaml"]).await.expect("open");
        let scanned = sync.update_from_working_dir(ScanOptions::default()).await;
        assert!(
            matches!(&scanned, Err(unfurl_git_sync::Error::Other(m)) if m.contains(REAL)),
            "{mode:?}: {scanned:?}"
        );
    }
}

/// A tracked file whose directory becomes a link to one outside the root
/// is refused: its records stay as they were, and nothing is written or
/// committed through the link.
#[tokio::test]
async fn a_tracked_file_whose_directory_becomes_a_link_is_refused() {
    let tmp = setup(Mode::Repository);
    let root = tmp.path().join("root");
    write(&root, "a/b.yaml", &cloudmap("a"));
    git(&root, &["add", "a/b.yaml"]);
    git(
        &root,
        &[
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@e",
            "commit",
            "-qm",
            "a",
        ],
    );
    let sync = open(&root, &["a/"]).await.expect("open");
    scan(&sync).await;
    assert_eq!(path_of(&sync, "a/b.yaml").await, "a");

    std::fs::rename(root.join("a"), tmp.path().join("outside")).expect("move");
    write(tmp.path(), "outside/b.yaml", &cloudmap("outside"));
    link(&root, "a", "../outside");
    let outcome = scan(&sync).await;
    assert!(
        outcome.unparsed.iter().any(|f| f.file_path == "a/b.yaml"),
        "{outcome:?}"
    );
    assert_eq!(outcome.files_deleted, 0, "{outcome:?}");
    assert_eq!(path_of(&sync, "a/b.yaml").await, "a");
    let saved = sync.save_changes().await.expect("save");
    assert!(saved.failed.is_empty(), "{saved:?}");

    let head = git_out(&root, &["rev-parse", "HEAD"]);
    sync.upsert_record(
        Some("a/b.yaml"),
        REPOS,
        A,
        json!({"path": "x"}),
        None,
        false,
    )
    .await
    .expect("upsert");
    let saved = sync.save_changes().await.expect("save");
    assert!(
        saved.failed.iter().any(|f| f.file_path == "a/b.yaml"),
        "{saved:?}"
    );
    let repo = sync.repository().expect("repository");
    assert!(repo
        .commit_repository("x", Default::default())
        .await
        .is_err());
    assert_eq!(git_out(&root, &["rev-parse", "HEAD"]), head);
    assert_eq!(
        std::fs::read_to_string(tmp.path().join("outside/b.yaml")).expect("read"),
        cloudmap("outside")
    );

    let named = open(&root, &["a/b.yaml"]).await.expect("open");
    let scanned = named.update_from_working_dir(ScanOptions::default()).await;
    assert!(
        matches!(&scanned, Err(unfurl_git_sync::Error::Other(m)) if m.contains("outside the root")),
        "{scanned:?}"
    );
}

/// A file the database holds that becomes a refused link keeps its records:
/// a file that can't be read isn't deleted, and a save leaves it alone.
#[tokio::test]
async fn a_known_file_that_becomes_a_refused_link_keeps_its_records() {
    for mode in MODES {
        let tmp = setup(mode);
        let root = tmp.path().join("root");
        write(&root, "maps/x.yaml", &cloudmap("x"));
        let sync = open(&root, &["maps/"]).await.expect("open");
        scan(&sync).await;
        assert_eq!(path_of(&sync, "maps/x.yaml").await, "x", "{mode:?}");

        write(tmp.path(), "outside/x.yaml", &cloudmap("outside"));
        std::fs::remove_file(root.join("maps/x.yaml")).expect("rm");
        link(&root, "maps/x.yaml", "../../outside/x.yaml");
        let outcome = scan(&sync).await;
        assert_eq!(outcome.files_deleted, 0, "{mode:?}: {outcome:?}");
        assert!(
            outcome
                .unparsed
                .iter()
                .any(|f| f.file_path == "maps/x.yaml"),
            "{mode:?}: {outcome:?}"
        );
        assert_eq!(path_of(&sync, "maps/x.yaml").await, "x", "{mode:?}");
        let saved = sync.save_changes().await.expect("save");
        assert!(saved.failed.is_empty(), "{mode:?}: {saved:?}");
        assert!(is_link(&root, "maps/x.yaml"));
    }
}

/// A database built before links were read as their files holds a link's
/// records twice: under the link and under its file. A scan leaves them
/// once, under the file.
async fn an_old_databases_link(mode: Mode) -> (TempDir, std::path::PathBuf, SyncedDir) {
    let tmp = setup(mode);
    let root = tmp.path().join("root");
    std::fs::remove_file(root.join(LINK)).expect("rm");
    write(&root, LINK, &cloudmap("a"));
    let sync = open(&root, &[LINK, REAL]).await.expect("open");
    scan(&sync).await;
    assert_eq!(
        records(&sync).await,
        [
            (LINK.to_string(), A.to_string()),
            (REAL.to_string(), A.to_string())
        ],
        "{mode:?}"
    );
    (tmp, root, sync)
}

/// Make `maps/link.yaml` the link it was in the old database's day.
fn relink(root: &Path) {
    std::fs::remove_file(root.join(LINK)).expect("rm");
    link(root, LINK, "../real/cloudmap.yaml");
}

#[tokio::test]
async fn a_known_file_that_becomes_a_link_to_a_taken_file_is_read_once() {
    for mode in MODES {
        let (_tmp, root, sync) = an_old_databases_link(mode).await;
        relink(&root);
        let outcome = scan(&sync).await;
        assert!(outcome.unparsed.is_empty(), "{mode:?}: {outcome:?}");
        assert_eq!(
            records(&sync).await,
            [(REAL.to_string(), A.to_string())],
            "{mode:?}"
        );
        let saved = sync.save_changes().await.expect("save");
        assert!(saved.failed.is_empty(), "{mode:?}: {saved:?}");
        if let Some(repo) = sync.repository() {
            repo.commit_repository("x", Default::default())
                .await
                .expect("commit");
            assert!(!git_out(&root, &["ls-tree", "-r", "HEAD"]).contains(LINK));
        }
        // without a repository nothing purges a gone file's tombstone
        let row = sync.get_file(LINK).await.expect("file");
        match mode {
            Mode::Repository => assert!(row.is_none(), "{row:?}"),
            Mode::Gitless => assert!(row.as_ref().is_none_or(|f| f.deleted), "{row:?}"),
        }
        assert!(is_link(&root, LINK));
        assert_eq!(
            std::fs::read_to_string(root.join(REAL)).expect("read"),
            cloudmap("a")
        );
    }
}

/// A pending edit under the link's path follows to its file's record.
#[tokio::test]
async fn a_pending_edit_at_a_link_follows_to_its_file() {
    for mode in MODES {
        let (_tmp, root, sync) = an_old_databases_link(mode).await;
        sync.upsert_record(Some(LINK), REPOS, A, json!({"path": "edited"}), None, false)
            .await
            .expect("upsert");
        relink(&root);
        let outcome = scan(&sync).await;
        assert!(outcome.conflicts.is_empty(), "{mode:?}: {outcome:?}");
        assert_eq!(
            records(&sync).await,
            [(REAL.to_string(), A.to_string())],
            "{mode:?}"
        );
        assert_eq!(path_of(&sync, REAL).await, "edited", "{mode:?}");
        let saved = sync.save_changes().await.expect("save");
        assert!(saved.failed.is_empty(), "{mode:?}: {saved:?}");
        assert!(is_link(&root, LINK));
        assert_eq!(
            std::fs::read_to_string(root.join(REAL)).expect("read"),
            cloudmap("edited")
        );
    }
}

/// When the file has its own edit to the same record, the link is refused
/// and both edits stay.
#[tokio::test]
async fn a_link_whose_edit_the_file_has_too_is_refused() {
    for mode in MODES {
        let (_tmp, root, sync) = an_old_databases_link(mode).await;
        for (file, value) in [(LINK, "link"), (REAL, "real")] {
            sync.upsert_record(Some(file), REPOS, A, json!({"path": value}), None, false)
                .await
                .expect("upsert");
        }
        relink(&root);
        let outcome = scan(&sync).await;
        assert!(
            outcome.unparsed.iter().any(|f| f.file_path == LINK),
            "{mode:?}: {outcome:?}"
        );
        assert_eq!(path_of(&sync, LINK).await, "link", "{mode:?}");
        assert_eq!(path_of(&sync, REAL).await, "real", "{mode:?}");
    }
}

/// A refused link a `:(literal)` pathspec names makes the scan an error.
#[tokio::test]
async fn a_link_named_with_literal_magic_is_an_error() {
    for mode in MODES {
        let tmp = setup(mode);
        let root = tmp.path().join("root");
        write(tmp.path(), "outside/cloudmap.yaml", &cloudmap("outside"));
        link(&root, "maps/out[1].yaml", "../../outside/cloudmap.yaml");
        let sync = open(&root, &[":(literal)maps/out[1].yaml"])
            .await
            .expect("open");
        let scanned = sync.update_from_working_dir(ScanOptions::default()).await;
        assert!(
            matches!(&scanned, Err(unfurl_git_sync::Error::Other(m)) if m.contains("outside the root")),
            "{mode:?}: {scanned:?}"
        );
    }
}

/// A commit never reads through a link: a file the database holds that
/// became a link, refused or the same as a file taken in, isn't committed.
#[tokio::test]
async fn a_commit_never_reads_through_a_link() {
    for dedup in [false, true] {
        let tmp = setup(Mode::Repository);
        let root = tmp.path().join("root");
        let file = "maps/x.yaml";
        write(&root, file, &cloudmap("x"));
        let sync = open(&root, &["maps/", REAL]).await.expect("open");
        scan(&sync).await;
        std::fs::remove_file(root.join(file)).expect("rm");
        if dedup {
            link(&root, file, "../real/cloudmap.yaml");
        } else {
            write(tmp.path(), "outside/x.yaml", &cloudmap("outside"));
            link(&root, file, "../../outside/x.yaml");
        }
        scan(&sync).await;
        let repo = sync.repository().expect("repository");
        let _ = repo.commit_repository("x", Default::default()).await;
        let tree = git_out(&root, &["ls-tree", "-r", "HEAD"]);
        assert!(!tree.contains(file), "dedup {dedup}: {tree}");
        let index = git_out(&root, &["ls-files", "-s", file]);
        assert!(!index.starts_with("100644"), "dedup {dedup}: {index}");
        assert!(is_link(&root, file));
    }
}

/// A tracked file with a hand edit taken in, whose directory then becomes a
/// link, isn't committed through it.
#[tokio::test]
async fn a_dirty_tracked_file_isnt_committed_through_a_linked_directory() {
    let tmp = setup(Mode::Repository);
    let root = tmp.path().join("root");
    write(&root, "a/b.yaml", &cloudmap("a"));
    git(&root, &["add", "a/b.yaml"]);
    git(
        &root,
        &[
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@e",
            "commit",
            "-qm",
            "a",
        ],
    );
    let sync = open(&root, &["a/"]).await.expect("open");
    scan(&sync).await;
    write(&root, "a/b.yaml", &cloudmap("hand"));
    scan(&sync).await;
    let blob = git_out(&root, &["rev-parse", "HEAD:a/b.yaml"]);
    std::fs::rename(root.join("a"), tmp.path().join("outside")).expect("move");
    write(tmp.path(), "outside/b.yaml", &cloudmap("outside"));
    link(&root, "a", "../outside");
    scan(&sync).await;
    let repo = sync.repository().expect("repository");
    let _ = repo.commit_repository("x", Default::default()).await;
    assert_eq!(git_out(&root, &["rev-parse", "HEAD:a/b.yaml"]), blob);
}

/// `delete_file` at a refused link whose records were kept deletes the
/// records and leaves the link, and the file it names, on disk.
#[tokio::test]
async fn deleting_a_refused_links_file_leaves_the_link() {
    for mode in MODES {
        let tmp = setup(mode);
        let root = tmp.path().join("root");
        write(&root, "maps/x.yaml", &cloudmap("x"));
        let sync = open(&root, &["maps/"]).await.expect("open");
        scan(&sync).await;
        write(tmp.path(), "outside/x.yaml", &cloudmap("outside"));
        std::fs::remove_file(root.join("maps/x.yaml")).expect("rm");
        link(&root, "maps/x.yaml", "../../outside/x.yaml");
        scan(&sync).await;
        assert_eq!(path_of(&sync, "maps/x.yaml").await, "x", "{mode:?}");

        sync.delete_file("maps/x.yaml", None).await.expect("delete");
        let written = sync.write_file("maps/x.yaml").await.expect("write");
        assert!(
            written.deleted && written.written.is_none(),
            "{mode:?}: {written:?}"
        );
        assert!(
            !records(&sync).await.iter().any(|(f, _)| f == "maps/x.yaml"),
            "{mode:?}"
        );
        assert!(is_link(&root, "maps/x.yaml"), "{mode:?}");
        assert_eq!(
            std::fs::read_to_string(tmp.path().join("outside/x.yaml")).expect("read"),
            cloudmap("outside")
        );
        scan(&sync).await;
        assert!(
            !records(&sync).await.iter().any(|(f, _)| f == "maps/x.yaml"),
            "{mode:?}"
        );
    }
}

/// A link read as the file it names has no file of its own to delete.
#[tokio::test]
async fn deleting_a_link_read_as_its_file_is_not_found() {
    for mode in MODES {
        let tmp = setup(mode);
        let root = tmp.path().join("root");
        let sync = open(&root, &[LINK, REAL]).await.expect("open");
        scan(&sync).await;
        let deleted = sync.delete_file(LINK, None).await;
        assert!(
            matches!(deleted, Err(unfurl_git_sync::Error::NotFound { .. })),
            "{mode:?}: {deleted:?}"
        );
        assert_eq!(
            records(&sync).await,
            [(REAL.to_string(), A.to_string())],
            "{mode:?}"
        );
    }
}
