// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Keeping the checkouts served as fresh as python keeps its clones, by
//! the pull record python keeps for each: see "Freshness" in the README.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use redis::AsyncCommands;
use unfurl_git_sync::git::Trust;
use unfurl_git_sync::{ScanOptions, SyncOutcome, SyncedRepo};

use crate::cache;
use crate::clone::{self, RequestAccess};
use crate::gitlock::{self, GitLocks};
use crate::grants::{Credentials, GrantStore};

/// Where pull records are kept: python's cache, or this process alone.
enum PullRecords {
    Redis(redis::aio::MultiplexedConnection),
    Memory(std::sync::Mutex<HashMap<String, (f64, String)>>),
}

impl PullRecords {
    async fn get(&self, key: &str) -> Option<(f64, String)> {
        match self {
            PullRecords::Redis(conn) => cache::pull_record_at(&mut conn.clone(), key).await,
            PullRecords::Memory(records) => records.lock().expect("records").get(key).cloned(),
        }
    }

    async fn set(&self, key: &str, outcome: &str) {
        let now = now();
        match self {
            PullRecords::Redis(conn) => {
                let raw = cache::pickled_pull_record(now, outcome);
                let set: Result<(), _> = conn.clone().set(key, raw).await;
                if let Err(e) = set {
                    tracing::warn!(key, error = %e, "can't record a pull");
                }
            }
            PullRecords::Memory(records) => {
                let mut records = records.lock().expect("records");
                records.insert(key.to_string(), (now, outcome.to_string()));
            }
        }
    }
}

fn now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0.0, |d| d.as_secs_f64())
}

/// A served checkout, as its pulls need it.
struct Tracked {
    /// Its pull record's key.
    key: String,
    origin: String,
    /// Its project's url on the cloud server.
    url: String,
    branch: String,
    /// In the clone root: cloned again if broken.
    cloned: bool,
    /// Under `private/`: pulled with a grant without a request's
    /// credentials.
    private: bool,
    /// Held by its pull: one at a time.
    pulling: Arc<tokio::sync::Mutex<()>>,
}

/// What the checkouts are pulled with.
pub struct Settings {
    /// `CACHE_DEFAULT_PULL_TIMEOUT`.
    pub timeout: i64,
    pub cache_key_prefix: String,
    pub cloud_server: String,
    pub clone_root: Option<PathBuf>,
    pub grants: Option<Arc<GrantStore>>,
    pub locks: Option<GitLocks>,
    pub trust: Trust,
}

/// Pulls the checkouts served when python would pull its clones.
pub struct Freshener {
    records: PullRecords,
    settings: Settings,
    /// Whether a checkout's scan after a pull is fine.
    accept: Box<dyn Fn(&SyncOutcome) -> bool + Send + Sync>,
    /// By worktree id.
    tracked: std::sync::Mutex<HashMap<i64, Arc<Tracked>>>,
    pulls: AtomicUsize,
}

impl Freshener {
    /// Keeping pull records in python's cache at `redis`, or without one
    /// in this process.
    pub fn new(
        redis: Option<redis::aio::MultiplexedConnection>,
        settings: Settings,
        accept: impl Fn(&SyncOutcome) -> bool + Send + Sync + 'static,
    ) -> Self {
        Freshener {
            records: match redis {
                Some(conn) => PullRecords::Redis(conn),
                None => PullRecords::Memory(Default::default()),
            },
            settings,
            accept: Box::new(accept),
            tracked: Default::default(),
            pulls: AtomicUsize::new(0),
        }
    }

    /// How many pulls have started.
    pub fn pulls_started(&self) -> usize {
        self.pulls.load(Ordering::SeqCst)
    }

    /// Before `handle` serves a request with `access`: pulled first if it
    /// lacks the request's `latest_commit`, else in the background if its
    /// last pull is older than the timeout.
    pub async fn freshen(
        self: &Arc<Self>,
        handle: &Arc<SyncedRepo>,
        access: &RequestAccess,
        latest_commit: Option<&str>,
    ) {
        let Some(tracked) = self.tracked(handle).await else {
            return;
        };
        let wanted = latest_commit
            .map(|c| c.trim_end_matches("-dirty"))
            .filter(|c| clone::commit_id(c));
        let credentials = access.credentials.clone();
        if let Some(commit) = wanted {
            if self.has(handle, commit).await || self.unpulled(&tracked.key).await {
                return;
            }
            // its own task: a request dropped while it waits can't cut git
            // off mid-merge
            let (me, handle, commit) = (self.clone(), handle.clone(), commit.to_string());
            let pulled = tokio::spawn(async move {
                let _held = tracked.pulling.lock().await;
                // another request's pull may have brought it
                if !me.has(&handle, &commit).await {
                    me.pull(&tracked, &handle, credentials, true).await;
                }
            });
            let _ = pulled.await;
            return;
        }
        if !self.stale(&tracked.key).await {
            return;
        }
        let Ok(held) = tracked.pulling.clone().try_lock_owned() else {
            return;
        };
        let (me, handle) = (self.clone(), handle.clone());
        tokio::spawn(async move {
            me.pull(&tracked, &handle, credentials, false).await;
            drop(held);
        });
    }

    /// Record how `handle`'s checkout was just pulled: one brought up.
    pub async fn record(&self, handle: &SyncedRepo, outcome: &str) {
        if let Some(tracked) = self.tracked(handle).await {
            self.records.set(&tracked.key, outcome).await;
        }
    }

    async fn has(&self, handle: &SyncedRepo, commit: &str) -> bool {
        clone::has_commit(handle.repo_path(), commit, self.settings.trust).await
    }

    /// Whether the pull record at `key` is of a checkout never pulled.
    async fn unpulled(&self, key: &str) -> bool {
        let record = self.records.get(key).await;
        record.is_some_and(|(_, outcome)| cache::unpulled(&outcome))
    }

    /// Whether the pull record at `key` says to pull, as python's
    /// `CacheEntry.pull` reads it.
    async fn stale(&self, key: &str) -> bool {
        let timeout = self.settings.timeout;
        match self.records.get(key).await {
            None => true,
            Some((_, outcome)) if cache::unpulled(&outcome) => false,
            Some(_) if timeout < 0 => false,
            Some((when, _)) => timeout == 0 || now() - when > timeout as f64,
        }
    }

    /// `handle`'s checkout as its pulls need it.
    async fn tracked(&self, handle: &SyncedRepo) -> Option<Arc<Tracked>> {
        let id = handle.worktree_id();
        if let Some(tracked) = self.tracked.lock().expect("tracked").get(&id) {
            return Some(tracked.clone());
        }
        let worktree = handle.get_worktree().await.ok()?;
        let s = &self.settings;
        let project = clone::project_of(&worktree.origin, &s.cloud_server)
            .unwrap_or_else(|| worktree.origin.clone());
        let path = handle.repo_path();
        let under = |base: &str| {
            s.clone_root
                .as_ref()
                .is_some_and(|root| path.starts_with(root.join(base)))
        };
        let tracked = Arc::new(Tracked {
            key: cache::pull_key(&s.cache_key_prefix, &project, path),
            url: format!("{}/{project}.git", s.cloud_server.trim_end_matches('/')),
            cloned: under("public") || under("private"),
            private: under("private"),
            origin: worktree.origin,
            branch: worktree.branch,
            pulling: Default::default(),
        });
        let mut all = self.tracked.lock().expect("tracked");
        Some(all.entry(id).or_insert(tracked).clone())
    }

    /// Pull `handle`'s checkout under its git lock -- unless `forced`, only
    /// if it's still stale then -- and record the pull.
    async fn pull(
        &self,
        tracked: &Tracked,
        handle: &SyncedRepo,
        credentials: Option<Credentials>,
        forced: bool,
    ) {
        let path = handle.repo_path();
        let lock = match gitlock::hold(self.settings.locks.as_ref(), path, gitlock::TTL).await {
            Ok(lock) => lock,
            Err(e) => {
                tracing::info!(checkout = %path.display(), error = e.as_str(), "not pulled");
                return;
            }
        };
        // python may have pulled it while this waited
        if forced || self.stale(&tracked.key).await {
            self.pulls.fetch_add(1, Ordering::SeqCst);
            let outcome = self.refresh(tracked, handle, credentials).await;
            self.records.set(&tracked.key, outcome).await;
        }
        gitlock::release(lock).await;
    }

    /// [`clone::refresh`] of `handle`'s checkout, with the request's
    /// credentials or a grant, and a rescan if its HEAD moved: the pull
    /// record's outcome.
    async fn refresh(
        &self,
        tracked: &Tracked,
        handle: &SyncedRepo,
        credentials: Option<Credentials>,
    ) -> &'static str {
        let credentials = match (credentials, &self.settings.grants) {
            (Some(credentials), _) => Some(credentials),
            (None, Some(grants)) if tracked.private => grants
                .latest_credentials(&tracked.origin)
                .await
                .ok()
                .flatten(),
            _ => None,
        };
        let refresh = clone::Refresh {
            path: handle.repo_path(),
            url: &tracked.url,
            reclone: tracked.cloned,
            branch: &tracked.branch,
            credentials: credentials.as_ref(),
            trust: self.settings.trust,
            // where it can't tell, as if it held some
            uncommitted: handle.has_uncommitted().await.unwrap_or(true),
        };
        match clone::refresh(&refresh).await {
            Ok((refreshed, moved)) => {
                if moved {
                    self.rescan(handle).await;
                }
                refreshed.outcome()
            }
            Err(e) => {
                tracing::warn!(
                    checkout = %handle.repo_path().display(),
                    error = %e,
                    "can't pull this cloudmap checkout; serving it as it is"
                );
                if e.refused() {
                    self.forget(credentials.as_ref()).await;
                }
                "failed"
            }
        }
    }

    /// Forget the grant `credentials` came from, if any: its remote
    /// refused it, as startup forgets one.
    async fn forget(&self, credentials: Option<&Credentials>) {
        let (Some(grants), Some(credentials)) = (&self.settings.grants, credentials) else {
            return;
        };
        if credentials.grant.is_empty() {
            return;
        }
        tracing::info!(
            grant = credentials.grant.as_str(),
            "git refused a grant; forgotten"
        );
        if let Err(e) = grants.forget(&credentials.grant).await {
            tracing::warn!(error = %e, "can't forget a grant git refused");
        }
    }

    /// Take in what a pull brought, and finish the exports it left.
    async fn rescan(&self, handle: &SyncedRepo) {
        let path = handle.repo_path();
        let scan = ScanOptions {
            recover_missing: true,
            ..Default::default()
        };
        match handle.update_from_working_dir(scan).await {
            Ok(outcome) if !(self.accept)(&outcome) => tracing::warn!(
                checkout = %path.display(),
                "this cloudmap checkout's scan after a pull reports problems; still served"
            ),
            Ok(_) => {}
            Err(e) => tracing::warn!(
                checkout = %path.display(),
                error = %e,
                "can't scan this cloudmap checkout after a pull"
            ),
        }
        if let Err(e) = handle.finish_exports().await {
            tracing::warn!(
                checkout = %path.display(),
                error = %e,
                "an export a pull left unfinished couldn't be finished"
            );
        }
    }
}
