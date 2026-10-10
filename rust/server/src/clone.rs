// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Clone or update a checkout of every worktree the cloudmap database
//! tracks, at startup, where no checkout is configured: in the directory
//! python's server clones a project into for a request with no
//! credentials (`_get_managed_project_repo_dir` in
//! `unfurl/server/serve.py`), under the same lock protocol. A private
//! project, which needs credentials, fails to clone there and is left to
//! python, which checks a request's.

use std::path::{Path, PathBuf};
use std::time::Duration;

use unfurl_git_sync::{Db, DbConfig, Worktree, WorktreeFilter};

use crate::cloudmap::CloudMapState;
use crate::gitlock::{self, GitLocks};

/// A checkout ready to open: worktree `project` on `branch`, at `path`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Checkout {
    pub path: PathBuf,
    pub project: String,
    pub branch: String,
}

/// A checkout of each worktree in the database `db` whose origin is a
/// project on `cloud_server`, cloned under `clone_root` or brought up to
/// its remote. A worktree that can't be cloned is logged and left out: a
/// project cloning needs credentials for, a branch only this database has
/// (an exported one), one python is cloning or pulling, or a remote that
/// doesn't answer in time. A checkout that can't be brought up, diverged
/// by commits the remote hasn't got, is served as it is.
pub async fn prepare(
    db: &DbConfig,
    clone_root: &Path,
    cloud_server: &str,
    locks: Option<&GitLocks>,
) -> Vec<Checkout> {
    let worktrees = match Db::connect(db).await {
        Ok(db) => db.worktrees(&WorktreeFilter::default()).await,
        Err(e) => Err(e),
    };
    let worktrees = match worktrees {
        Ok(w) => w,
        Err(e) => {
            tracing::error!(
                error = e.to_string().as_str(),
                "can't list the cloudmap worktrees"
            );
            return Vec::new();
        }
    };
    if locks.is_some() {
        // python must resolve its UNFURL_CLONE_ROOT to the same path, or
        // the two servers lock different keys
        tracing::info!(
            root = %gitlock::real_path(clone_root).display(),
            "git locks shared with python are keyed under"
        );
    }
    let mut ready = Vec::new();
    for w in worktrees {
        let Some(project) = project_of(&w, cloud_server) else {
            tracing::debug!(
                origin = w.origin.as_str(),
                "not a project on the cloud server; skipped"
            );
            continue;
        };
        let checkout = Checkout {
            path: clone_root.join("public").join(&project).join(&w.branch),
            project,
            branch: w.branch.clone(),
        };
        let url = format!(
            "{}/{}.git",
            cloud_server.trim_end_matches('/'),
            checkout.project
        );
        match update(&checkout, &url, locks).await {
            Ok(()) => ready.push(checkout),
            Err(e) => tracing::warn!(
                project = checkout.project.as_str(),
                branch = checkout.branch.as_str(),
                error = e.as_str(),
                "no checkout of this cloudmap worktree; it isn't served"
            ),
        }
    }
    ready
}

/// Open each of `checkouts` on the database at `db_url`, scanning it with
/// `scan`, and serve those that open and whose scan `accept` takes. One
/// that doesn't is logged, with `hint`'s advice on its error, and left to
/// python: the rest are still served.
pub async fn serve(
    checkouts: Vec<Checkout>,
    db_url: &str,
    scan: Option<unfurl_git_sync::ScanOptions>,
    accept: impl Fn(&unfurl_git_sync::SyncOutcome) -> bool,
    hint: impl Fn(&(dyn std::error::Error + 'static), &str) -> String,
    locks: Option<GitLocks>,
) -> Result<Option<CloudMapState>, unfurl_git_sync::Error> {
    let mut open = Vec::new();
    for checkout in checkouts {
        let path = checkout.path.to_string_lossy();
        match CloudMapState::open_locked(&path, db_url, scan, &accept, locks.as_ref()).await {
            Ok(None) => tracing::error!(
                project = checkout.project.as_str(),
                branch = checkout.branch.as_str(),
                "this cloudmap checkout isn't served"
            ),
            Ok(Some(cm)) => open.push(cm),
            Err(e) => tracing::error!(
                project = checkout.project.as_str(),
                branch = checkout.branch.as_str(),
                error = format!("{e}{}", hint(&*e, &path)).as_str(),
                "failed to open a cloudmap checkout; it isn't served"
            ),
        }
    }
    CloudMapState::serving(open, locks).await
}

/// Worktree `w`'s project on `cloud_server`: its origin with the server's
/// host, as the database normalizes both, taken off.
fn project_of(w: &Worktree, cloud_server: &str) -> Option<String> {
    let host = unfurl_git_sync::git::normalize_git_url_hard(cloud_server);
    let project = w.origin.strip_prefix(&format!("{host}/"))?;
    (!project.is_empty()).then(|| project.to_string())
}

/// Clone `url`'s branch to the checkout, or fast-forward the one there,
/// holding its lock as python does: created exclusively, with this
/// process's pid in it.
async fn update(checkout: &Checkout, url: &str, locks: Option<&GitLocks>) -> Result<(), String> {
    let parent = checkout.path.parent().unwrap_or(Path::new("."));
    std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    let git_lock = gitlock::hold(locks, &checkout.path, gitlock::TTL).await?;
    let updated = update_locked(checkout, url).await;
    gitlock::release(git_lock).await;
    updated
}

/// [`update`], holding the checkout's git lock.
async fn update_locked(checkout: &Checkout, url: &str) -> Result<(), String> {
    let lock = lock_path(&checkout.path);
    let held = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&lock)
        .and_then(|mut f| {
            std::io::Write::write_all(&mut f, std::process::id().to_string().as_bytes())
        });
    held.map_err(|e| {
        format!(
            "{} is held: python is cloning or pulling it ({e})",
            lock.display()
        )
    })?;
    let updated = match checkout.path.exists() {
        true => pull(&checkout.path).await,
        false => clone(checkout, url).await,
    };
    let _ = std::fs::remove_file(&lock);
    updated
}

/// Fast-forward the checkout at `path` to its remote, or leave it as it is
/// where it can't be: its commits the remote lacks are its to keep, as
/// python's server keeps the cloudmap commits it hasn't pushed.
async fn pull(path: &Path) -> Result<(), String> {
    if !path.join(".git").exists() {
        return Err(format!(
            "{} is there but isn't a git checkout",
            path.display()
        ));
    }
    // as `clone` does, for a checkout made before it did, or by python
    git(path, &["config", "core.symlinks", "false"]).await?;
    if git(path, &["rev-parse", "--is-shallow-repository"]).await? == "true" {
        // git-sync needs the history a shallow clone of python's lacks
        git(path, &["fetch", "-q", "--unshallow"]).await?;
    }
    if let Err(e) = git(path, &["pull", "-q", "--ff-only"]).await {
        tracing::warn!(
            path = %path.display(),
            error = e.as_str(),
            "can't fast-forward this cloudmap checkout; serving it as it is"
        );
    }
    Ok(())
}

/// Clone `url`'s branch to the checkout. No submodules: git-sync reads
/// none, and their urls are whoever can push to the project's.
async fn clone(checkout: &Checkout, url: &str) -> Result<(), String> {
    let path = checkout.path.to_string_lossy();
    // from here, where a relative `path` is
    git(
        Path::new("."),
        &[
            "clone",
            // a committed symlink would otherwise expose a file outside it;
            // in the clone's config, so it holds for every later checkout
            "-c",
            "core.symlinks=false",
            "-q",
            "--branch",
            &checkout.branch,
            "--no-single-branch",
            url,
            &path,
        ],
    )
    .await
    .map(|_| ())
}

/// The lock python's `lock_file` holds while it clones `path`.
fn lock_path(path: &Path) -> PathBuf {
    let mut lock = path.as_os_str().to_owned();
    lock.push(".lock");
    PathBuf::from(lock)
}

/// How long one git command may take before it's killed: a remote that
/// stops answering would otherwise hold startup up for good.
const GIT_TIMEOUT: Duration = Duration::from_secs(300);

/// `git args` in `dir`: its output, or why it failed.
async fn git(dir: &Path, args: &[&str]) -> Result<String, String> {
    let run = tokio::process::Command::new("git")
        .args(args)
        .current_dir(dir)
        // never wait on a prompt for credentials
        .env("GIT_TERMINAL_PROMPT", "0")
        .kill_on_drop(true)
        .output();
    let out = tokio::time::timeout(GIT_TIMEOUT, run)
        .await
        .map_err(|_| format!("git {}: no answer in {GIT_TIMEOUT:?}", args.join(" ")))?
        .map_err(|e| format!("git: {e}"))?;
    match out.status.success() {
        true => Ok(String::from_utf8_lossy(&out.stdout).trim().to_string()),
        false => Err(format!(
            "git {}: {}",
            args.join(" "),
            String::from_utf8_lossy(&out.stderr).trim()
        )),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn worktree(origin: &str) -> Worktree {
        Worktree {
            id: 1,
            origin: origin.into(),
            branch: "main".into(),
            commit_id: None,
            default_file_path: None,
            exporting_from: None,
        }
    }

    #[test]
    fn a_project_is_its_origin_on_the_cloud_server() {
        let w = worktree("unfurl.cloud/onecommons/cloudmap");
        for server in ["https://unfurl.cloud", "https://unfurl.cloud/"] {
            assert_eq!(
                project_of(&w, server).as_deref(),
                Some("onecommons/cloudmap"),
                "{server}"
            );
        }
        assert_eq!(project_of(&w, "https://elsewhere.example"), None);
        assert_eq!(
            project_of(&worktree("unfurl.cloud"), "https://unfurl.cloud"),
            None
        );
    }
}
