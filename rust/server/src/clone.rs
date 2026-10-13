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

use unfurl_git_sync::git::Trust;
use unfurl_git_sync::model::{PRIVATE, PUBLIC};
use unfurl_git_sync::{Db, DbConfig, Worktree, WorktreeFilter};

use crate::cloudmap::CloudMapState;
use crate::gitcli::{
    git, holding_lock_file, refuse_other_origin, refuse_untrusted_config, refused, Access,
};
use crate::gitlock::{self, GitLocks};
use crate::grants::{Credentials, GrantStore};

/// A checkout ready to open: worktree `project` on `branch`, at `path`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Checkout {
    pub path: PathBuf,
    pub project: String,
    pub branch: String,
    /// Whether it's a default cloudmap: read by a request naming no project.
    pub default_cloudmap: bool,
    /// How bringing it up ended, as python's pull record names it; `None`
    /// where its remote couldn't be reached and it's served as it is.
    pub pulled: Option<&'static str>,
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
    trust: Trust,
    default_cloudmaps: &[DefaultCloudmap],
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
        trust,
    };
    let mut ready = Vec::new();
    for mut w in startup.wanted(&worktrees, default_cloudmaps).await {
        if let Some(mut checkout) = startup.settle(&mut w).await {
            // a request naming no project, which the gateway can't check
            // access for, reads the default cloudmaps: never a private repository
            if checkout.default_cloudmap && w.visibility.as_deref() == Some(PRIVATE) {
                tracing::error!(
                    project = checkout.project.as_str(),
                    "a default cloudmap repository is private; it isn't one"
                );
                checkout.default_cloudmap = false;
            }
            ready.push(checkout);
        }
    }
    ready
}

/// The branch `git ls-remote --symref <url> HEAD`'s output `out` says
/// HEAD names.
fn remote_head(out: &str) -> Option<String> {
    out.lines().find_map(|line| {
        let (target, name) = line.strip_prefix("ref: ")?.split_once('\t')?;
        (name == "HEAD").then(|| target.strip_prefix("refs/heads/").map(str::to_string))?
    })
}

/// What [`prepare`] brings each worktree's checkout up with.
struct Startup<'a> {
    db: &'a Db,
    clone_root: &'a Path,
    cloud_server: &'a str,
    locks: Option<&'a GitLocks>,
    grants: Option<&'a GrantStore>,
    trust: Trust,
}

/// Whether `path`, from a request, names somewhere beneath where it's
/// joined: no empty, `.` or `..` component, and not absolute.
pub(crate) fn relative(path: &str) -> bool {
    path.split('/').all(|part| !matches!(part, "" | "." | ".."))
}

/// A lock for each project and branch, which bringing it up holds.
type Gates = std::sync::Mutex<
    std::collections::HashMap<(String, String), std::sync::Arc<tokio::sync::Mutex<()>>>,
>;

/// What brings up a checkout on demand, for a request naming a project
/// and branch the server has none of: as [`prepare`] and [`serve`] do at
/// startup, but with the request's own credentials first.
pub struct Cloner {
    db: Db,
    clone_root: PathBuf,
    cloud_server: String,
    locks: Option<GitLocks>,
    grants: Option<std::sync::Arc<GrantStore>>,
    trust: Trust,
    /// Whether a checkout's scan lets it be served, as at startup.
    accept: Box<dyn Fn(&unfurl_git_sync::SyncOutcome) -> bool + Send + Sync>,
    /// One at a time per project and branch.
    gates: Gates,
}

/// How a request may reach its project's repository, as python decides
/// it (`_get_managed_project_repo_dir`): with any credential, or a
/// `visibility` other than public, its clone is under `private/`; with a
/// user and a secret, those reach git.
#[derive(Debug, Clone, Default)]
pub struct RequestAccess {
    pub private: bool,
    pub credentials: Option<Credentials>,
    /// The cloud server's user making it (`X-Unfurl-Username`), whom a
    /// grant of its credentials is for.
    pub username: String,
}

impl Cloner {
    pub fn new(
        db: Db,
        clone_root: &Path,
        cloud_server: &str,
        locks: Option<GitLocks>,
        grants: Option<std::sync::Arc<GrantStore>>,
        trust: Trust,
        accept: impl Fn(&unfurl_git_sync::SyncOutcome) -> bool + Send + Sync + 'static,
    ) -> Self {
        Cloner {
            db,
            clone_root: clone_root.to_path_buf(),
            cloud_server: cloud_server.to_string(),
            locks,
            grants,
            trust,
            accept: Box::new(accept),
            gates: Default::default(),
        }
    }

    /// The gate a bring-up of `project` on `branch` holds.
    pub(crate) fn gate(
        &self,
        project: &str,
        branch: &str,
    ) -> std::sync::Arc<tokio::sync::Mutex<()>> {
        let key = (project.to_string(), branch.to_string());
        let mut gates = self.gates.lock().expect("gates");
        gates.entry(key).or_default().clone()
    }

    /// Forget the gate of `project` on `branch` if no bring-up holds it.
    pub(crate) fn release_gate(&self, project: &str, branch: &str) {
        let key = (project.to_string(), branch.to_string());
        let mut gates = self.gates.lock().expect("gates");
        if gates
            .get(&key)
            .is_some_and(|gate| std::sync::Arc::strong_count(gate) == 1)
        {
            gates.remove(&key);
        }
    }

    /// How many gates are kept: one per bring-up in progress.
    pub fn gates_kept(&self) -> usize {
        self.gates.lock().expect("gates").len()
    }

    /// `project` on `branch`, or its default branch, as the database
    /// knows it, or its remote says with `access`.
    pub(crate) async fn wanted(
        &self,
        project: &str,
        branch: Option<&str>,
        access: &RequestAccess,
    ) -> Option<Wanted> {
        let startup = self.startup();
        let host = unfurl_git_sync::git::normalize_git_url_hard(&self.cloud_server);
        let origin = format!("{host}/{project}");
        let filter = WorktreeFilter {
            origin: Some(origin.clone()),
            ..Default::default()
        };
        let known = self.db.worktrees(&filter).await.ok()?;
        let known: Vec<&Worktree> = known.iter().collect();
        let branch = match branch {
            Some(branch) => branch.to_string(),
            None => {
                let url = format!("{}/{project}.git", self.cloud_server.trim_end_matches('/'));
                let access = self.access(&url, access);
                startup.default_branch_of(project, &known, &access).await
            }
        };
        if unfinished_export(&known, &branch) {
            tracing::info!(
                project,
                branch,
                "an unfinished export's branch isn't brought up"
            );
            return None;
        }
        Some(Wanted {
            worktree: known.iter().find(|w| w.branch == branch).map(|w| w.id),
            visibility: known.iter().find_map(|w| w.visibility.clone()),
            origin,
            branch,
            default_cloudmap: false,
        })
    }

    fn startup(&self) -> Startup<'_> {
        Startup {
            db: &self.db,
            clone_root: &self.clone_root,
            cloud_server: &self.cloud_server,
            locks: self.locks.as_ref(),
            grants: self.grants.as_deref(),
            trust: self.trust,
        }
    }

    /// Keep the credentials of `access`, which brought up `project`'s
    /// checkout, as a grant, for its later pulls, as a queued write's are.
    async fn keep_grant(&self, project: &str, access: &RequestAccess) {
        let (Some(grants), Some(credentials)) = (&self.grants, &access.credentials) else {
            return;
        };
        let origin = format!("{}/{project}", self.cloud_server.trim_end_matches('/'));
        let token = crate::grants::credentials(&credentials.user, &credentials.secret);
        if let Err(e) = grants.grant(&access.username, &origin, &token, "").await {
            tracing::warn!(project, error = %e, "can't keep a request's credentials as a grant");
        }
    }

    /// `access` for git to reach `url` with: the request's credentials,
    /// if it has them.
    fn access(&self, url: &str, access: &RequestAccess) -> Access {
        match &access.credentials {
            Some(credentials) => Access::with(url, credentials),
            None => Access::anonymous(url),
        }
        .trusting(self.trust)
    }

    /// A checkout of `wanted`, of `project`, cloned or brought up to its
    /// remote and opened: for a request with private `access`, under
    /// `private/` with its credentials, if any; otherwise as the startup
    /// clone does. Scanned recovering a missing commit's records, as a new
    /// clone is, whatever startup's options, and served only if its scan
    /// is accepted.
    pub(crate) async fn bring_up(
        &self,
        mut wanted: Wanted,
        project: &str,
        access: &RequestAccess,
    ) -> Option<(CloudMapState, Option<&'static str>)> {
        let startup = self.startup();
        let checkout = match access.private {
            true => {
                let url = format!("{}/{project}.git", self.cloud_server.trim_end_matches('/'));
                let access = self.access(&url, access);
                startup.settle_with(&wanted, project, &access).await?
            }
            false => startup.settle(&mut wanted).await?,
        };
        let path = checkout.path.to_string_lossy();
        let scan = Some(unfurl_git_sync::ScanOptions {
            recover_missing: true,
            ..Default::default()
        });
        let opened = CloudMapState::open_locked(
            &path,
            &self.db,
            scan,
            &self.accept,
            self.locks.as_ref(),
            self.trust,
        )
        .await;
        let opened = opened.unwrap_or_else(|e| {
            tracing::warn!(
                project,
                error = e.to_string().as_str(),
                "can't open a checkout brought up for a request"
            );
            None
        })?;
        // only credentials git took, for a checkout served
        if access.private && checkout.pulled.is_some() {
            self.keep_grant(project, access).await;
        }
        Some((opened, checkout.pulled))
    }
}

/// Whether `branch`, among the worktrees `known`, is one an export hasn't
/// finished: a checkout of it would take in the edits moving to it, which
/// the export then commits too.
fn unfinished_export(known: &[&Worktree], branch: &str) -> bool {
    known
        .iter()
        .any(|w| w.branch == branch && w.exporting_from.is_some())
}

/// A worktree [`prepare`] brings a checkout up for: one the database
/// tracks, a default cloudmap, or one a request names.
pub(crate) struct Wanted {
    origin: String,
    pub(crate) branch: String,
    visibility: Option<String>,
    default_cloudmap: bool,
    /// Its row in the database, if it has one.
    worktree: Option<i64>,
}

impl Wanted {
    fn of(w: &Worktree) -> Self {
        Wanted {
            origin: w.origin.clone(),
            branch: w.branch.clone(),
            visibility: w.visibility.clone(),
            default_cloudmap: false,
            worktree: Some(w.id),
        }
    }
}

/// A cloudmap repository a request naming no project reads: a project on
/// the cloud server, and its branch, or with none its default.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DefaultCloudmap {
    pub project: String,
    pub branch: Option<String>,
}

impl DefaultCloudmap {
    /// `project[#branch]`; an error without a project.
    pub fn parse(text: &str) -> Result<Self, String> {
        let (project, branch) = match text.trim().split_once('#') {
            Some((project, branch)) => (project, Some(branch.trim().to_string())),
            None => (text.trim(), None),
        };
        let project = project.trim().trim_matches('/');
        if project.is_empty() {
            return Err(format!("default cloudmap {text:?} names no project"));
        }
        Ok(DefaultCloudmap {
            project: project.to_string(),
            branch: branch.filter(|b| !b.is_empty()),
        })
    }
}

/// How many of a private repository's grants are tried before it's left
/// to python.
const GRANTS_TRIED: usize = 3;

impl Startup<'_> {
    /// Worktree `w`'s checkout, cloned or brought up: anonymously unless
    /// its repository is known to be private, then with a grant.
    async fn settle(&self, w: &mut Wanted) -> Option<Checkout> {
        let Some(project) = project_of(&w.origin, self.cloud_server) else {
            tracing::debug!(
                origin = w.origin.as_str(),
                "not a project on the cloud server; skipped"
            );
            return None;
        };
        let url = format!("{}/{project}.git", self.cloud_server.trim_end_matches('/'));
        if w.visibility.as_deref() != Some(PRIVATE) {
            let mut checkout = self.checkout(w, &project, "public");
            match update(
                &checkout,
                &url,
                self.locks,
                &Access::anonymous(&url).trusting(self.trust),
                self.uncommitted(w).await,
            )
            .await
            {
                Ok(pulled) => {
                    // only what the remote answered says it's public
                    if w.visibility.is_none() && pulled.is_some() {
                        self.mark(&w.origin, PUBLIC).await;
                    }
                    checkout.pulled = pulled;
                    return Some(checkout);
                }
                Err(e) if refused(&e) => {
                    self.mark(&w.origin, PRIVATE).await;
                    w.visibility = Some(PRIVATE.to_string());
                }
                Err(e) => {
                    left_out(&checkout, &e);
                    return None;
                }
            }
        }
        let private = self.checkout(w, &project, "private");
        let uncommitted = self.uncommitted(w).await;
        let settled = self.settle_private(private.clone(), &w.origin, &url, uncommitted);
        if let Some(checkout) = settled.await {
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
        uncommitted: bool,
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
            let access = Access::with(url, &credentials).trusting(self.trust);
            match update(&checkout, url, self.locks, &access, uncommitted).await {
                Ok(pulled) => return Some(Checkout { pulled, ..checkout }),
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

    /// Whether the database holds work `w`'s commits don't; where it
    /// can't tell, as if it did.
    async fn uncommitted(&self, w: &Wanted) -> bool {
        match w.worktree {
            Some(id) => self.db.has_uncommitted(id).await.unwrap_or(true),
            None => false,
        }
    }

    fn checkout(&self, w: &Wanted, project: &str, under: &str) -> Checkout {
        Checkout {
            path: self.clone_root.join(under).join(project).join(&w.branch),
            project: project.to_string(),
            branch: w.branch.clone(),
            default_cloudmap: w.default_cloudmap,
            pulled: None,
        }
    }

    /// Worktree `w`'s checkout, of `project`, cloned or brought up under
    /// `private/` with a request's `access`.
    async fn settle_with(&self, w: &Wanted, project: &str, access: &Access) -> Option<Checkout> {
        let url = format!("{}/{project}.git", self.cloud_server.trim_end_matches('/'));
        let checkout = self.checkout(w, project, "private");
        match update(
            &checkout,
            &url,
            self.locks,
            access,
            self.uncommitted(w).await,
        )
        .await
        {
            Ok(pulled) => Some(Checkout { pulled, ..checkout }),
            Err(e) => {
                left_out(&checkout, &e);
                None
            }
        }
    }

    /// The worktrees to bring checkouts up for: the database's, and the
    /// `default_cloudmaps` it lacks, each default cloudmap marked.
    async fn wanted(
        &self,
        worktrees: &[Worktree],
        default_cloudmaps: &[DefaultCloudmap],
    ) -> Vec<Wanted> {
        let host = unfurl_git_sync::git::normalize_git_url_hard(self.cloud_server);
        // not one without a checkout, or brought up on demand, which a
        // request brings up again, unless it holds commits to push; never
        // an unfinished export's
        let mut wanted: Vec<Wanted> = worktrees
            .iter()
            .filter(|w| (w.clone_on_start || w.push_pending) && w.exporting_from.is_none())
            .map(Wanted::of)
            .collect();
        for entry in default_cloudmaps {
            let origin = format!("{host}/{}", entry.project);
            let known: Vec<&Worktree> = worktrees.iter().filter(|w| w.origin == origin).collect();
            let branch = match &entry.branch {
                Some(branch) => branch.clone(),
                None => {
                    let url = format!(
                        "{}/{}.git",
                        self.cloud_server.trim_end_matches('/'),
                        entry.project
                    );
                    let anonymous = Access::anonymous(&url).trusting(self.trust);
                    self.default_branch_of(&entry.project, &known, &anonymous)
                        .await
                }
            };
            let visibility = known.iter().find_map(|w| w.visibility.clone());
            match wanted
                .iter_mut()
                .find(|w| w.origin == origin && w.branch == branch)
            {
                Some(w) => w.default_cloudmap = true,
                None => wanted.push(Wanted {
                    worktree: known.iter().find(|w| w.branch == branch).map(|w| w.id),
                    origin,
                    branch,
                    visibility,
                    default_cloudmap: true,
                }),
            }
        }
        wanted
    }

    /// `project`'s default branch: as recorded on one of its worktrees
    /// `known`, or its remote's HEAD, or `main`.
    async fn default_branch_of(
        &self,
        project: &str,
        known: &[&Worktree],
        access: &Access,
    ) -> String {
        if let Some(branch) = known.iter().find_map(|w| w.default_branch.clone()) {
            return branch;
        }
        let url = format!("{}/{project}.git", self.cloud_server.trim_end_matches('/'));
        // in the clone root: not the server's own directory, see `clone`
        let asked = match std::fs::create_dir_all(self.clone_root) {
            Ok(()) => {
                let args = ["ls-remote", "--symref", url.as_str(), "HEAD"];
                git(self.clone_root, access, &args).await
            }
            Err(e) => Err(e.to_string()),
        };
        match asked.map(|out| remote_head(&out)) {
            Ok(Some(branch)) => branch,
            found => {
                tracing::debug!(
                    project,
                    found = ?found,
                    "no default branch for a default cloudmap; taking main"
                );
                "main".to_string()
            }
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
    trust: Trust,
) -> Result<Option<CloudMapState>, unfurl_git_sync::Error> {
    let db = crate::cloudmap::connect(db_url)
        .await
        .map_err(|e| unfurl_git_sync::Error::Other(e.to_string()))?;
    let mut open = Vec::new();
    let mut defaults = Vec::new();
    for checkout in checkouts {
        let path = checkout.path.to_string_lossy();
        match CloudMapState::open_locked(&path, &db, scan, &accept, locks.as_ref(), trust).await {
            Ok(None) => tracing::error!(
                project = checkout.project.as_str(),
                branch = checkout.branch.as_str(),
                "this cloudmap checkout isn't served"
            ),
            Ok(Some(cm)) => {
                if checkout.default_cloudmap {
                    defaults.push(cm.synced().worktree_id());
                }
                open.push(cm)
            }
            Err(e) => tracing::error!(
                project = checkout.project.as_str(),
                branch = checkout.branch.as_str(),
                error = format!("{e}{}", hint(&*e, &path)).as_str(),
                "failed to open a cloudmap checkout; it isn't served"
            ),
        }
    }
    Ok(CloudMapState::serving(open, locks)
        .await?
        .map(|cm| cm.with_default_cloudmaps(defaults)))
}

/// The project on `cloud_server` of a worktree of `origin`: its origin with the server's
/// host, as the database normalizes both, taken off.
pub(crate) fn project_of(origin: &str, cloud_server: &str) -> Option<String> {
    let host = unfurl_git_sync::git::normalize_git_url_hard(cloud_server);
    let project = origin.strip_prefix(&format!("{host}/"))?;
    (!project.is_empty()).then(|| project.to_string())
}

/// Clone `url`'s branch to the checkout, or bring the one there up to its
/// remote as [`refresh`] does, holding its git lock and python's lock
/// file; a checkout its remote can't be reached for is served as it is.
/// `uncommitted` is whether the database holds work its commits don't.
/// How it ended, as python's pull record names it, or `None` where the
/// remote couldn't be reached.
async fn update(
    checkout: &Checkout,
    url: &str,
    locks: Option<&GitLocks>,
    access: &Access,
    uncommitted: bool,
) -> Result<Option<&'static str>, String> {
    let parent = checkout.path.parent().unwrap_or(Path::new("."));
    std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    let git_lock = gitlock::hold(locks, &checkout.path, gitlock::TTL).await?;
    let updated = holding_lock_file(&checkout.path, async {
        if !checkout.path.exists() {
            return clone(checkout, url, access).await.map(|()| Some("cloned"));
        }
        let r = Refresh {
            path: &checkout.path,
            url,
            reclone: true,
            branch: &checkout.branch,
            credentials: None,
            trust: access.trust,
            uncommitted,
        };
        match refresh_with(&r, access).await {
            Ok((refreshed, _)) => Ok(Some(refreshed.outcome())),
            Err(RefreshError::Remote(e)) if !refused(&e) => {
                tracing::warn!(
                    path = %checkout.path.display(),
                    error = e.as_str(),
                    "can't reach this cloudmap checkout's remote; serving it as it is"
                );
                Ok(None)
            }
            Err(e) => Err(e.to_string()),
        }
    })
    .await;
    gitlock::release(git_lock).await;
    updated?
}

/// Whether a directory below `dir` is a checkout.
fn holds_checkouts(dir: &Path) -> bool {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return false;
    };
    entries
        .filter_map(|e| e.ok())
        .filter(|e| e.file_type().is_ok_and(|t| t.is_dir()) && e.file_name() != ".git")
        .any(|e| e.path().join(".git").exists() || holds_checkouts(&e.path()))
}

/// What a refresh did to a checkout.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Refreshed {
    /// Fast-forwarded to its remote, or already there.
    Pulled,
    /// Reset to its remote, which it couldn't fast-forward to and held no
    /// work the remote lacks.
    Reset,
    /// Moved aside, broken, and cloned again.
    Recloned,
    /// Left as it is: it can't fast-forward to its remote, and holds work
    /// the remote lacks.
    Diverged,
    /// On no branch with an upstream: nothing to pull.
    Detached,
}

impl Refreshed {
    /// The outcome python's pull record names it by.
    pub fn outcome(self) -> &'static str {
        match self {
            Refreshed::Pulled | Refreshed::Reset => "pulled",
            Refreshed::Recloned => "cloned",
            Refreshed::Diverged => "diverged",
            Refreshed::Detached => "detached",
        }
    }
}

/// A checkout to refresh and how to reach its remote.
pub struct Refresh<'a> {
    pub path: &'a Path,
    /// What its credentials are for: its project's url.
    pub url: &'a str,
    /// Cloned again from `url` when broken; never if false.
    pub reclone: bool,
    pub branch: &'a str,
    pub credentials: Option<&'a Credentials>,
    pub trust: Trust,
    /// Whether the database holds work its commits don't.
    pub uncommitted: bool,
}

/// Why a refresh failed.
#[derive(Debug)]
pub enum RefreshError {
    /// Its own config or state is refused -- what git would run, a remote
    /// not its project's, another branch checked out: it isn't served or
    /// pushed as it is, until someone changes it.
    Untrusted(String),
    /// Its remote couldn't be reached, or refused its credentials.
    Remote(String),
    /// Git failed in it, or it couldn't be cloned again.
    Local(String),
}

impl std::fmt::Display for RefreshError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            RefreshError::Untrusted(e) | RefreshError::Remote(e) | RefreshError::Local(e) => {
                f.write_str(e)
            }
        }
    }
}

impl RefreshError {
    /// Whether its remote refused its credentials, or the lack of them.
    pub fn refused(&self) -> bool {
        matches!(self, RefreshError::Remote(e) if refused(e))
    }
}

/// Bring the checkout up to its remote, holding its git lock: fast-forward
/// it; failing that, reset it to its remote if it holds no work the
/// remote lacks, or leave it diverged; a broken one is moved aside and
/// cloned again. With whether its HEAD moved.
pub async fn refresh(r: &Refresh<'_>) -> Result<(Refreshed, bool), RefreshError> {
    let access = match r.credentials {
        Some(credentials) => Access::with(r.url, credentials),
        None => Access::anonymous(r.url),
    }
    .trusting(r.trust);
    holding_lock_file(r.path, refresh_with(r, &access))
        .await
        .map_err(RefreshError::Local)?
}

/// [`refresh`], reaching its remote with `access`.
async fn refresh_with(r: &Refresh<'_>, access: &Access) -> Result<(Refreshed, bool), RefreshError> {
    if !r.path.join(".git").exists() {
        return reclone(r, access, "it isn't a git checkout").await;
    }
    if access.trust == Trust::Reduced {
        refuse_untrusted_config(r.path, access)
            .await
            .map_err(RefreshError::Untrusted)?;
    }
    if let Some(why) = unreadable(r.path, access).await {
        return reclone(r, access, &why).await;
    }
    // one in the clone root: its credentials are for its project alone
    if r.reclone {
        refuse_other_origin(r.path, access, r.url)
            .await
            .map_err(RefreshError::Untrusted)?;
    }
    let head = async || {
        git(r.path, access, &["rev-parse", "HEAD"])
            .await
            .map_err(RefreshError::Local)
    };
    let before = head().await?;
    let refreshed = fast_forward(r, access).await?;
    Ok((refreshed, before != head().await?))
}

/// Why git can't read the checkout at `path`, if it can't.
async fn unreadable(path: &Path, access: &Access) -> Option<String> {
    for args in [
        &["rev-parse", "--verify", "HEAD"][..],
        &["status", "--porcelain"],
    ] {
        if let Err(e) = git(path, access, args).await {
            return Some(e);
        }
    }
    None
}

/// The broken checkout moved aside, if it's there, and cloned again, if
/// it may be.
async fn reclone(
    r: &Refresh<'_>,
    access: &Access,
    why: &str,
) -> Result<(Refreshed, bool), RefreshError> {
    if !r.reclone {
        let broken = format!("{} is broken: {why}", r.path.display());
        return Err(RefreshError::Local(broken));
    }
    if r.path.exists() {
        move_aside(r.path, why)?;
    }
    let checkout = Checkout {
        path: r.path.to_path_buf(),
        project: String::new(),
        branch: r.branch.to_string(),
        default_cloudmap: false,
        pulled: None,
    };
    if let Err(e) = clone(&checkout, r.url, access).await {
        tracing::error!(
            checkout = %r.path.display(),
            error = e.as_str(),
            "a broken cloudmap checkout couldn't be cloned again; \
             tried again after the pull timeout"
        );
        return Err(match refused(&e) {
            true => RefreshError::Remote(e),
            false => RefreshError::Local(e),
        });
    }
    Ok((Refreshed::Recloned, true))
}

/// Move the broken checkout at `path` to `<path>.broken-<unix time>`,
/// unless it holds other checkouts: those of branches named below its own,
/// which aren't its to move.
fn move_aside(path: &Path, why: &str) -> Result<(), RefreshError> {
    if holds_checkouts(path) {
        let display = path.display();
        return Err(RefreshError::Local(format!(
            "{display} is broken ({why}), and holds other checkouts: not moved aside"
        )));
    }
    let stamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| d.as_secs());
    let mut aside = path.as_os_str().to_owned();
    aside.push(format!(".broken-{stamp}"));
    let aside = PathBuf::from(aside);
    std::fs::rename(path, &aside).map_err(|e| {
        let display = path.display();
        RefreshError::Local(format!(
            "{display} is broken ({why}) and can't be moved aside: {e}"
        ))
    })?;
    tracing::error!(
        checkout = %path.display(),
        moved_to = %aside.display(),
        why,
        "a broken cloudmap checkout was moved aside; cloning it again"
    );
    Ok(())
}

/// Fetch the checkout's remote and fast-forward to its upstream; failing
/// that, reset to it if the checkout holds no work it lacks.
async fn fast_forward(r: &Refresh<'_>, access: &Access) -> Result<Refreshed, RefreshError> {
    let local = async |args: &[&str]| git(r.path, access, args).await.map_err(RefreshError::Local);
    // as `clone` does, for a checkout made before it did, or by python
    local(&["config", "core.symlinks", "false"]).await?;
    let mut fetch = vec![
        "-c",
        "remote.origin.followRemoteHEAD=always",
        "fetch",
        "-q",
        "origin",
    ];
    if local(&["rev-parse", "--is-shallow-repository"]).await? == "true" {
        // git-sync needs the history a shallow clone of python's lacks
        fetch.push("--unshallow");
    }
    // before the fetch: after it, a remote's rewritten history leaves
    // commits on no remote-tracking branch that were never this checkout's
    let ahead = git(
        r.path,
        access,
        &["rev-list", "HEAD", "--not", "--remotes", "--count"],
    )
    .await;
    git(r.path, access, &fetch)
        .await
        .map_err(RefreshError::Remote)?;
    let upstream = ["rev-parse", "--verify", "-q", "@{upstream}"];
    if git(r.path, access, &upstream).await.is_err() {
        return Ok(Refreshed::Detached);
    }
    let Err(e) = git(r.path, access, &["merge", "-q", "--ff-only", "@{upstream}"]).await else {
        return Ok(Refreshed::Pulled);
    };
    if let Some(work) = unpushed(r, access, ahead).await {
        tracing::warn!(
            checkout = %r.path.display(),
            work = work.as_str(),
            error = e.as_str(),
            "can't fast-forward this cloudmap checkout, which holds work its remote lacks; \
             serving it as it is"
        );
        return Ok(Refreshed::Diverged);
    }
    local(&["reset", "-q", "--hard", "@{upstream}"]).await?;
    tracing::warn!(
        checkout = %r.path.display(),
        error = e.as_str(),
        "can't fast-forward this cloudmap checkout, which holds no work its remote lacks; \
         reset to its remote"
    );
    Ok(Refreshed::Reset)
}

/// The work the checkout holds that its remote lacks, as python's
/// `_unpushed_work` tells, but for `ahead`, its commits on no
/// remote-tracking branch before the fetch; and records the database
/// hasn't committed.
async fn unpushed(
    r: &Refresh<'_>,
    access: &Access,
    ahead: Result<String, String>,
) -> Option<String> {
    if r.uncommitted {
        return Some("records not yet committed".to_string());
    }
    // every untracked file, whatever the checkout's own config says to show
    let status = ["status", "--porcelain", "--untracked-files=all"];
    match git(r.path, access, &status).await {
        Ok(changes) if !changes.is_empty() => return Some("uncommitted changes".to_string()),
        Ok(_) => {}
        Err(e) => return Some(format!("can't tell: {e}")),
    }
    match ahead {
        Ok(n) if n == "0" => None,
        Ok(n) => Some(format!("{n} commits not pushed")),
        Err(e) => Some(format!("can't tell: {e}")),
    }
}

/// Whether `name` is a commit's hex id, or an abbreviation of one.
pub fn commit_id(name: &str) -> bool {
    (4..=64).contains(&name.len()) && name.bytes().all(|b| b.is_ascii_hexdigit())
}

/// Whether the checkout at `path` has `commit`, a [`commit_id`].
pub async fn has_commit(path: &Path, commit: &str, trust: Trust) -> bool {
    if !commit_id(commit) {
        return false;
    }
    let access = Access::anonymous("file:///").trusting(trust);
    let object = format!("{commit}^{{commit}}");
    git(path, &access, &["cat-file", "-e", &object])
        .await
        .is_ok()
}

/// Clone `url`'s branch to the checkout. No submodules: git-sync reads
/// none, and their urls are whoever can push to the project's.
async fn clone(checkout: &Checkout, url: &str, access: &Access) -> Result<(), String> {
    // from its parent, which `update` made: never from the server's own
    // directory, which may be in a repository whose config git would read
    let parent = checkout.path.parent().unwrap_or(Path::new("."));
    let name = checkout
        .path
        .file_name()
        .unwrap_or_default()
        .to_string_lossy();
    git(
        parent,
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
            &name,
        ],
    )
    .await
    .map(|_| ())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_default_cloudmap_names_a_project_and_maybe_a_branch() {
        let parsed = |text| {
            let l = DefaultCloudmap::parse(text).expect("parse");
            (l.project, l.branch)
        };
        assert_eq!(parsed("org/proj"), ("org/proj".into(), None));
        assert_eq!(
            parsed(" org/proj/#dev "),
            ("org/proj".into(), Some("dev".into()))
        );
        assert_eq!(parsed("org/proj#"), ("org/proj".into(), None));
        assert!(DefaultCloudmap::parse("#dev").is_err());
        assert!(DefaultCloudmap::parse(" / ").is_err());
    }

    #[test]
    fn the_remote_head_is_read_from_ls_remote() {
        let out = "ref: refs/heads/trunk\tHEAD\n0123abc\tHEAD\n";
        assert_eq!(remote_head(out).as_deref(), Some("trunk"));
        assert_eq!(remote_head("0123abc\tHEAD\n"), None);
    }

    #[test]
    fn an_unfinished_exports_branch_isnt_brought_up() {
        let mut exporting = worktree("unfurl.cloud/org/proj");
        exporting.branch = "git-sync/conflicts".into();
        exporting.exporting_from = Some("main".into());
        let main = worktree("unfurl.cloud/org/proj");
        let known = [&exporting, &main];
        assert!(unfinished_export(&known, "git-sync/conflicts"));
        assert!(!unfinished_export(&known, "main"));
        exporting.exporting_from = None;
        assert!(!unfinished_export(&[&exporting], "git-sync/conflicts"));
    }

    fn worktree(origin: &str) -> Worktree {
        Worktree {
            id: 1,
            origin: origin.into(),
            branch: "main".into(),
            commit_id: None,
            default_file_path: None,
            exporting_from: None,
            visibility: None,
            default_branch: None,
            clone_on_start: true,
            push_pending: false,
        }
    }

    #[test]
    fn a_project_is_its_origin_on_the_cloud_server() {
        let w = worktree("unfurl.cloud/onecommons/cloudmap");
        for server in ["https://unfurl.cloud", "https://unfurl.cloud/"] {
            assert_eq!(
                project_of(&w.origin, server).as_deref(),
                Some("onecommons/cloudmap"),
                "{server}"
            );
        }
        assert_eq!(project_of(&w.origin, "https://elsewhere.example"), None);
        assert_eq!(
            project_of(&worktree("unfurl.cloud").origin, "https://unfurl.cloud"),
            None
        );
    }
}
