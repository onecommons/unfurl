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

use unfurl_git_sync::model::{PRIVATE, PUBLIC};
use unfurl_git_sync::{Db, DbConfig, Worktree, WorktreeFilter};

use crate::cloudmap::CloudMapState;
use crate::gitlock::{self, GitLocks};
use crate::grants::{Credentials, GrantStore};

/// A checkout ready to open: worktree `project` on `branch`, at `path`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Checkout {
    pub path: PathBuf,
    pub project: String,
    pub branch: String,
}

/// A checkout of each worktree in the database `db` whose origin is a
/// project on `cloud_server`, cloned under `clone_root` or brought up to
/// its remote: anonymously, under `public/`, unless the repository is
/// known to be private, and otherwise under `private/` with the most
/// recently used of `grants` for it. Whether a repository turned out
/// public or private is recorded; private can also mean gone, since the
/// cloud server refuses a missing project as it does a private one, and
/// then the grants for it, none of which opens it, are forgotten. A
/// private one no grant opens is served from the checkout it has, if any,
/// as it is. A worktree that can't be
/// cloned is logged and left out: a branch only this database has (an
/// exported one), one python is cloning or pulling, or a remote that
/// doesn't answer in time. A checkout that can't be brought up, diverged
/// by commits the remote hasn't got, is served as it is.
pub async fn prepare(
    db: &DbConfig,
    clone_root: &Path,
    cloud_server: &str,
    locks: Option<&GitLocks>,
    grants: Option<&GrantStore>,
) -> Vec<Checkout> {
    let db = match Db::connect(db).await {
        Ok(db) => db,
        Err(e) => {
            tracing::error!(
                error = e.to_string().as_str(),
                "can't open the cloudmap database"
            );
            return Vec::new();
        }
    };
    let worktrees = db.worktrees(&WorktreeFilter::default()).await;
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
    let startup = Startup {
        db: &db,
        clone_root,
        cloud_server,
        locks,
        grants,
    };
    let mut ready = Vec::new();
    for w in worktrees {
        if let Some(checkout) = startup.settle(&w).await {
            ready.push(checkout);
        }
    }
    ready
}

/// What [`prepare`] brings each worktree's checkout up with.
struct Startup<'a> {
    db: &'a Db,
    clone_root: &'a Path,
    cloud_server: &'a str,
    locks: Option<&'a GitLocks>,
    grants: Option<&'a GrantStore>,
}

/// How many of a private repository's grants are tried before it's left
/// to python.
const GRANTS_TRIED: usize = 3;

impl Startup<'_> {
    /// Worktree `w`'s checkout, cloned or brought up: anonymously unless
    /// its repository is known to be private, then with a grant.
    async fn settle(&self, w: &Worktree) -> Option<Checkout> {
        let Some(project) = project_of(w, self.cloud_server) else {
            tracing::debug!(
                origin = w.origin.as_str(),
                "not a project on the cloud server; skipped"
            );
            return None;
        };
        let url = format!("{}/{project}.git", self.cloud_server.trim_end_matches('/'));
        if w.visibility.as_deref() != Some(PRIVATE) {
            let checkout = self.checkout(w, &project, "public");
            match update(&checkout, &url, self.locks, &Access::anonymous(&url)).await {
                Ok(()) => {
                    if w.visibility.is_none() {
                        self.mark(&w.origin, PUBLIC).await;
                    }
                    return Some(checkout);
                }
                Err(e) if refused(&e) => self.mark(&w.origin, PRIVATE).await,
                Err(e) => {
                    left_out(&checkout, &e);
                    return None;
                }
            }
        }
        let private = self.checkout(w, &project, "private");
        if let Some(checkout) = self.settle_private(private.clone(), &w.origin, &url).await {
            return Some(checkout);
        }
        // until a grant opens it, a checkout already here serves as it is:
        // the gateway has checked each reader's access to the project
        [private, self.checkout(w, &project, "public")]
            .into_iter()
            .find(|c| c.path.join(".git").exists())
    }

    /// `checkout`, of private repository `origin`, cloned or brought up
    /// with its most recently used grant that works: one git refuses is
    /// forgotten, and the next tried.
    async fn settle_private(
        &self,
        checkout: Checkout,
        origin: &str,
        url: &str,
    ) -> Option<Checkout> {
        let grants = self.grants?;
        for _ in 0..GRANTS_TRIED {
            let credentials = match grants.latest_credentials(origin).await {
                Ok(Some(credentials)) => credentials,
                Ok(None) => break,
                Err(e) => {
                    left_out(&checkout, &e.to_string());
                    return None;
                }
            };
            let access = Access::with(url, &credentials);
            match update(&checkout, url, self.locks, &access).await {
                Ok(()) => return Some(checkout),
                Err(e) if refused(&e) => {
                    tracing::info!(
                        grant = credentials.grant.as_str(),
                        "git refused a grant; forgotten"
                    );
                    if let Err(e) = grants.forget(&credentials.grant).await {
                        left_out(&checkout, &e.to_string());
                        return None;
                    }
                }
                Err(e) => {
                    left_out(&checkout, &e);
                    return None;
                }
            }
        }
        tracing::info!(
            project = checkout.project.as_str(),
            branch = checkout.branch.as_str(),
            "private, and no grant opens it; left to python"
        );
        None
    }

    fn checkout(&self, w: &Worktree, project: &str, under: &str) -> Checkout {
        Checkout {
            path: self.clone_root.join(under).join(project).join(&w.branch),
            project: project.to_string(),
            branch: w.branch.clone(),
        }
    }

    async fn mark(&self, origin: &str, visibility: &str) {
        if let Err(e) = self.db.set_visibility(origin, Some(visibility)).await {
            tracing::warn!(
                origin,
                error = e.to_string().as_str(),
                "can't record visibility"
            );
        }
    }
}

fn left_out(checkout: &Checkout, error: &str) {
    tracing::warn!(
        project = checkout.project.as_str(),
        branch = checkout.branch.as_str(),
        error,
        "no checkout of this cloudmap worktree; it isn't served"
    );
}

/// Whether git's error `error` is a refusal of the credentials it had, or
/// of none: the remote wants credentials (401), others (403), or answers
/// as if the repository weren't there (404), as GitLab and GitHub answer
/// credentials that can't see it. Not a missing branch, which credentials
/// that work meet too.
fn refused(error: &str) -> bool {
    [
        "could not read Username",
        "Authentication failed",
        "Access denied",
        "returned error: 401",
        "returned error: 403",
        "could not be found or you don't have permission",
    ]
    .iter()
    .any(|sign| error.contains(sign))
        || (error.contains("repository '") && error.contains("' not found"))
}

/// How git reaches the cloud server: its protocol, and credentials for its
/// host, as config in git's environment, which it doesn't store.
struct Access {
    protocol: String,
    env: Vec<(String, String)>,
}

impl Access {
    fn anonymous(url: &str) -> Self {
        Access {
            protocol: protocol_of(url),
            env: Vec::new(),
        }
    }

    /// `credentials` for `url`'s whole host: a `url.<...>.insteadOf`
    /// rewrite, as python's `credentials_config`.
    fn with(url: &str, credentials: &Credentials) -> Self {
        let mut access = Self::anonymous(url);
        if let Ok(parsed) = url::Url::parse(url) {
            if let Some(host) = parsed.host_str() {
                let host = match parsed.port() {
                    Some(port) => format!("{host}:{port}"),
                    None => host.to_string(),
                };
                let scheme = parsed.scheme();
                let user = urlencoding::encode(&credentials.user);
                let secret = urlencoding::encode(&credentials.secret);
                access.env = vec![
                    ("GIT_CONFIG_COUNT".into(), "1".into()),
                    (
                        "GIT_CONFIG_KEY_0".into(),
                        format!("url.{scheme}://{user}:{secret}@{host}/.insteadOf"),
                    ),
                    ("GIT_CONFIG_VALUE_0".into(), format!("{scheme}://{host}/")),
                ];
            }
        }
        access
    }
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
async fn update(
    checkout: &Checkout,
    url: &str,
    locks: Option<&GitLocks>,
    access: &Access,
) -> Result<(), String> {
    let parent = checkout.path.parent().unwrap_or(Path::new("."));
    std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    let git_lock = gitlock::hold(locks, &checkout.path, gitlock::TTL).await?;
    let updated = update_locked(checkout, url, access).await;
    gitlock::release(git_lock).await;
    updated
}

/// [`update`], holding the checkout's git lock.
async fn update_locked(checkout: &Checkout, url: &str, access: &Access) -> Result<(), String> {
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
        true => pull(&checkout.path, access).await,
        false => clone(checkout, url, access).await,
    };
    let _ = std::fs::remove_file(&lock);
    updated
}

/// Fast-forward the checkout at `path` to its remote, or leave it as it is
/// where it can't be: its commits the remote lacks are its to keep, as
/// python's server keeps the cloudmap commits it hasn't pushed. A remote
/// refusing its credentials, or the lack of them, is an error.
async fn pull(path: &Path, access: &Access) -> Result<(), String> {
    if !path.join(".git").exists() {
        return Err(format!(
            "{} is there but isn't a git checkout",
            path.display()
        ));
    }
    // as `clone` does, for a checkout made before it did, or by python
    git(path, access, &["config", "core.symlinks", "false"]).await?;
    if git(path, access, &["rev-parse", "--is-shallow-repository"]).await? == "true" {
        // git-sync needs the history a shallow clone of python's lacks
        git(path, access, &["fetch", "-q", "--unshallow"]).await?;
    }
    if let Err(e) = git(path, access, &["pull", "-q", "--ff-only"]).await {
        if refused(&e) {
            return Err(e);
        }
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
async fn clone(checkout: &Checkout, url: &str, access: &Access) -> Result<(), String> {
    let path = checkout.path.to_string_lossy();
    // from here, where a relative `path` is
    git(
        Path::new("."),
        access,
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
/// What git keeps of the server's environment: what it needs to reach the
/// cloud server, through a proxy and with the host's certificates.
const KEPT_ENV: [&str; 16] = [
    "PATH",
    "HOME",
    "TMPDIR",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "all_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "ALL_PROXY",
    "GIT_SSL_CAINFO",
    "GIT_SSL_CAPATH",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "CURL_CA_BUNDLE",
];

/// `text` with any credentials in a url's userinfo taken out.
fn redact(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut rest = text;
    while let Some(start) = rest.find("//") {
        let (before, after) = rest.split_at(start + 2);
        out.push_str(before);
        let authority = after
            .find(|c: char| c == '/' || c.is_whitespace() || c == '\'')
            .unwrap_or(after.len());
        match after[..authority].rfind('@') {
            Some(at) => {
                out.push_str("<redacted>");
                rest = &after[at..];
            }
            None => rest = after,
        }
    }
    out.push_str(rest);
    out
}

/// The protocol `url` uses, as `GIT_ALLOW_PROTOCOL` names it.
fn protocol_of(url: &str) -> String {
    match url.split_once("://") {
        Some((scheme, _)) => scheme.to_ascii_lowercase(),
        None if url.starts_with('/') || url.starts_with('.') => "file".into(),
        None => "ssh".into(),
    }
}

/// `git args` in `dir`, allowed only `protocol`: its output, or why it
/// failed. Nothing configured on the host -- its environment, its system
/// or global git config, such as a credential helper or a url rewrite --
/// reaches a clone of a user's repository.
async fn git(dir: &Path, access: &Access, args: &[&str]) -> Result<String, String> {
    let mut command = tokio::process::Command::new("git");
    command.env_clear();
    for name in KEPT_ENV {
        if let Some(value) = std::env::var_os(name) {
            command.env(name, value);
        }
    }
    let run = command
        .args(args)
        .current_dir(dir)
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_ALLOW_PROTOCOL", &access.protocol)
        .envs(access.env.iter().map(|(k, v)| (k, v)))
        // never wait on a prompt for credentials
        .env("GIT_TERMINAL_PROMPT", "0")
        // git's messages, for telling a refusal of credentials apart
        .env("LC_ALL", "C")
        .kill_on_drop(true)
        .output();
    let out = tokio::time::timeout(GIT_TIMEOUT, run)
        .await
        .map_err(|_| format!("git {}: no answer in {GIT_TIMEOUT:?}", args.join(" ")))?
        .map_err(|e| format!("git: {e}"))?;
    match out.status.success() {
        true => Ok(String::from_utf8_lossy(&out.stdout).trim().to_string()),
        // without any credentials git might show in a url
        false => Err(redact(&format!(
            "git {}: {}",
            args.join(" "),
            String::from_utf8_lossy(&out.stderr).trim()
        ))),
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
            visibility: None,
        }
    }

    #[test]
    fn credentials_in_gits_errors_are_redacted() {
        let error = "fatal: unable to access 'https://deploy:t=k%40n:x%2Fy@host/org/p.git/': \
                     The requested URL returned error: 403 (http://u:p@other:8080/x)";
        let redacted = redact(error);
        assert!(
            !redacted.contains("t=k") && !redacted.contains("u:p"),
            "{redacted}"
        );
        assert!(
            redacted.contains("https://<redacted>@host/org/p.git/"),
            "{redacted}"
        );
        assert!(
            redacted.contains("http://<redacted>@other:8080/x"),
            "{redacted}"
        );
        assert_eq!(redact("git clone file:///srv/x"), "git clone file:///srv/x");
    }

    #[test]
    fn a_refusal_of_credentials_is_told_apart() {
        for refusal in [
            "fatal: could not read Username for 'http://h': terminal prompts disabled",
            "fatal: Authentication failed for 'https://h/x.git/'",
            "remote: HTTP Basic: Access denied",
            "The requested URL returned error: 403",
        ] {
            assert!(refused(refusal), "{refusal}");
        }
        for other in [
            "fatal: unable to access 'https://h/': Could not resolve host: h",
            "fatal: Not possible to fast-forward, aborting.",
            "The requested URL returned error: 404",
        ] {
            assert!(!refused(other), "{other}");
        }
    }

    #[test]
    fn a_repository_hidden_from_credentials_is_told_from_a_missing_branch() {
        for hidden in [
            "remote: The project you were looking for could not be found or you don't have permission to view it.",
            "fatal: repository 'https://h/org/proj.git/' not found",
        ] {
            assert!(refused(hidden), "{hidden}");
        }
        assert!(!refused(
            "fatal: Remote branch exported not found in upstream origin"
        ));
    }

    /// Credentials go to git for the cloud server's whole host, encoded,
    /// and not anywhere it stores them.
    #[test]
    fn credentials_reach_git_for_the_whole_host() {
        let credentials = Credentials {
            grant: "g".into(),
            user: "deploy".into(),
            secret: "t=k@n:x/y".into(),
        };
        let access = Access::with("https://unfurl.cloud:8443/org/proj.git", &credentials);
        assert_eq!(
            access.env,
            [
                ("GIT_CONFIG_COUNT".to_string(), "1".to_string()),
                (
                    "GIT_CONFIG_KEY_0".to_string(),
                    "url.https://deploy:t%3Dk%40n%3Ax%2Fy@unfurl.cloud:8443/.insteadOf".to_string()
                ),
                (
                    "GIT_CONFIG_VALUE_0".to_string(),
                    "https://unfurl.cloud:8443/".to_string()
                ),
            ]
        );
        assert_eq!(access.protocol, "https");
        assert!(Access::anonymous("https://h/x.git").env.is_empty());
    }

    #[test]
    fn a_urls_protocol_is_as_git_names_it() {
        for (url, protocol) in [
            ("https://unfurl.cloud/org/proj.git", "https"),
            ("HTTP://unfurl.cloud/org/proj.git", "http"),
            ("file:///srv/org/proj.git", "file"),
            ("/srv/org/proj.git", "file"),
            ("git@unfurl.cloud:org/proj.git", "ssh"),
        ] {
            assert_eq!(protocol_of(url), protocol, "{url}");
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
