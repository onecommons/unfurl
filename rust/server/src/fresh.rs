// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Keeping the checkouts served as fresh as python keeps its clones, by
//! the pull record python keeps for each, and pushing their commits: see
//! "Freshness" and "Pushing" in the README.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use redis::AsyncCommands;
use unfurl_git_sync::git::Trust;
use unfurl_git_sync::{ScanOptions, SyncOutcome, SyncedRepo};

use crate::cache;
use crate::clone::{self, RefreshError, RequestAccess};
use crate::gitlock::{self, GitLocks};
use crate::grants::{Credentials, GrantStore};

/// Where pull records, branch heads and push outcomes are kept: python's
/// cache, or this process alone.
enum PullRecords {
    Redis(redis::aio::MultiplexedConnection),
    Memory {
        pulls: std::sync::Mutex<HashMap<String, (f64, String)>>,
        values: std::sync::Mutex<HashMap<String, String>>,
    },
}

impl PullRecords {
    async fn get(&self, key: &str) -> Option<(f64, String)> {
        match self {
            PullRecords::Redis(conn) => cache::pull_record_at(&mut conn.clone(), key).await,
            PullRecords::Memory { pulls, .. } => pulls.lock().expect("records").get(key).cloned(),
        }
    }

    /// Set `key` to `value`, to expire after `ttl` seconds, or never with 0.
    async fn put(&self, key: &str, value: &str, ttl: u64) {
        match self {
            PullRecords::Redis(conn) => {
                let mut conn = conn.clone();
                let set: Result<(), _> = match ttl {
                    0 => conn.set(key, value).await,
                    ttl => conn.set_ex(key, value, ttl).await,
                };
                if let Err(e) = set {
                    tracing::warn!(key, error = %e, "can't record a push");
                }
            }
            PullRecords::Memory { values, .. } => {
                let mut values = values.lock().expect("records");
                values.insert(key.to_string(), value.to_string());
            }
        }
    }

    async fn value(&self, key: &str) -> Option<String> {
        match self {
            PullRecords::Redis(conn) => conn.clone().get(key).await.ok().flatten(),
            PullRecords::Memory { values, .. } => values.lock().expect("records").get(key).cloned(),
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
            PullRecords::Memory { pulls, .. } => {
                let mut pulls = pulls.lock().expect("records");
                pulls.insert(key.to_string(), (now, outcome.to_string()));
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
    /// Its project on the cloud server, or its origin.
    project: String,
    origin: String,
    /// Its project's url on the cloud server.
    url: String,
    branch: String,
    /// In the clone root: cloned again if broken.
    cloned: bool,
    /// Under `private/`: pulled with a grant without a request's
    /// credentials.
    private: bool,
    /// Held by its pull or push: one at a time.
    pulling: Arc<tokio::sync::Mutex<()>>,
    /// When it was last pushed, whatever came of it.
    last_push: std::sync::Mutex<Option<std::time::Instant>>,
    /// Whether a push of it is waiting to run.
    push_scheduled: std::sync::atomic::AtomicBool,
    /// Its pushes that failed since the last that didn't, which back its
    /// retries off.
    push_failures: std::sync::atomic::AtomicU32,
}

/// Whether a push is tried again, and with what.
enum Again {
    No,
    /// Its remote couldn't be reached: with the same credentials.
    Same,
    /// Its credentials were refused: with a grant.
    WithAGrant,
}

/// The least a push waits to be tried again, whatever the interval.
const RETRY_DELAY: std::time::Duration = std::time::Duration::from_secs(1);

/// The most a push waits to be tried again, however often it failed.
const RETRY_MAX: std::time::Duration = std::time::Duration::from_secs(3600);

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
    /// The fewest seconds between two pushes of a checkout.
    pub push_interval: u64,
    /// How long a branch head or push outcome is kept, in seconds; 0 for
    /// ever.
    pub record_ttl: u64,
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
    pushes: AtomicUsize,
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
                None => PullRecords::Memory {
                    pulls: Default::default(),
                    values: Default::default(),
                },
            },
            settings,
            accept: Box::new(accept),
            tracked: Default::default(),
            pulls: AtomicUsize::new(0),
            pushes: AtomicUsize::new(0),
        }
    }

    /// How many pulls have started.
    pub fn pulls_started(&self) -> usize {
        self.pulls.load(Ordering::SeqCst)
    }

    /// How many pushes have started.
    pub fn pushes_started(&self) -> usize {
        self.pushes.load(Ordering::SeqCst)
    }

    /// What became of pushing `commit`, a commit of `handle`'s checkout:
    /// its push record, as `/events` reports it.
    pub async fn push_outcome(&self, handle: &SyncedRepo, commit: &str) -> Option<String> {
        let tracked = self.tracked(handle).await?;
        let key = self.push_key(&tracked, commit);
        self.records.value(&key).await
    }

    fn push_key(&self, tracked: &Tracked, commit: &str) -> String {
        let prefix = &self.settings.cache_key_prefix;
        crate::config::push_key(prefix, &tracked.project, &tracked.branch, commit)
    }

    /// After `handle`'s checkout committed `commit` for a request with
    /// `access`: its push recorded as pending, and made in the background,
    /// no sooner than the push interval after the last, with the request's
    /// credentials.
    pub async fn committed(
        self: &Arc<Self>,
        handle: &Arc<SyncedRepo>,
        commit: &str,
        access: &RequestAccess,
    ) {
        let Some(tracked) = self.tracked(handle).await else {
            return;
        };
        // the configured checkout, the operator's own, isn't pushed
        if !tracked.cloned {
            return;
        }
        // pushed at startup if this process stops before it is
        if let Err(e) = handle.set_push_pending(true).await {
            tracing::warn!(error = %e, "can't record a push as pending");
        }
        let pending = serde_json::json!({"status": "pending"});
        self.record_push(&tracked, &[commit.to_string()], &pending)
            .await;
        let credentials = access.credentials.clone();
        self.schedule_push(tracked, handle.clone(), credentials);
    }

    /// Push `tracked`'s checkout in the background, unless a push of it is
    /// waiting already, which takes whatever it has by then.
    fn schedule_push(
        self: &Arc<Self>,
        tracked: Arc<Tracked>,
        handle: Arc<SyncedRepo>,
        credentials: Option<Credentials>,
    ) {
        if tracked.push_scheduled.swap(true, Ordering::SeqCst) {
            return;
        }
        let me = self.clone();
        tokio::spawn(async move {
            // waited for holding its lock, so that a push in progress has
            // recorded its time
            let _held = loop {
                let held = tracked.pulling.clone().lock_owned().await;
                match me.push_wait(&tracked) {
                    wait if wait.is_zero() => break held,
                    wait => {
                        drop(held);
                        tokio::time::sleep(wait).await;
                    }
                }
            };
            // a commit from now on needs a push of its own
            tracked.push_scheduled.store(false, Ordering::SeqCst);
            let path = handle.repo_path();
            let lock = gitlock::hold(me.settings.locks.as_ref(), path, gitlock::TTL).await;
            let again = match lock {
                Ok(lock) => {
                    let again = me.push_locked(&tracked, &handle, credentials.clone()).await;
                    gitlock::release(lock).await;
                    again
                }
                Err(e) => {
                    tracing::info!(checkout = %path.display(), error = e.as_str(), "not pushed");
                    Again::Same
                }
            };
            drop(_held);
            // tried again after the interval, but never at once
            let credentials = match again {
                Again::No => return,
                Again::Same => credentials,
                Again::WithAGrant => None,
            };
            tokio::time::sleep(me.retry_delay(&tracked)).await;
            me.schedule_push(tracked, handle, credentials);
        });
    }

    /// Push `handle`'s checkout in the background, as after a commit, with
    /// a grant: one whose push this process, or one before it, hadn't made.
    pub async fn resume_push(self: &Arc<Self>, handle: &Arc<SyncedRepo>) {
        if let Some(tracked) = self.tracked(handle).await {
            self.schedule_push(tracked, handle.clone(), None);
        }
    }

    /// How long a failed push of `tracked`'s checkout waits to be tried
    /// again: the push interval, a second at the least, doubled for each
    /// failure since the last push that didn't, up to an hour.
    fn retry_delay(&self, tracked: &Tracked) -> std::time::Duration {
        let failures = tracked.push_failures.load(Ordering::SeqCst);
        backoff(self.settings.push_interval, failures)
    }

    /// How long until `tracked`'s checkout may be pushed again.
    fn push_wait(&self, tracked: &Tracked) -> std::time::Duration {
        let interval = std::time::Duration::from_secs(self.settings.push_interval);
        let last = *tracked.last_push.lock().expect("last push");
        last.map_or(std::time::Duration::ZERO, |last| {
            last.checked_add(interval)
                .map_or(std::time::Duration::MAX, |due| {
                    due.saturating_duration_since(std::time::Instant::now())
                })
        })
    }

    /// Push `handle`'s checkout, holding its git lock: with `credentials`,
    /// a request's, or else a grant for it. A commit set aside or diverted
    /// leaves the checkout reset to its remote, and rescanned. Whether to
    /// try again: it couldn't be pushed, and may be later.
    async fn push_locked(
        &self,
        tracked: &Tracked,
        handle: &SyncedRepo,
        credentials: Option<Credentials>,
    ) -> Again {
        // the configured checkout, the operator's own, isn't pushed
        if !tracked.cloned {
            return Again::No;
        }
        let credentials = match (credentials, &self.settings.grants) {
            (Some(credentials), _) => Some(credentials),
            (None, Some(grants)) => grants
                .latest_credentials(&tracked.origin)
                .await
                .ok()
                .flatten(),
            _ => None,
        };
        let push = crate::push::Push {
            path: handle.repo_path(),
            url: &tracked.url,
            branch: &tracked.branch,
            credentials: credentials.as_ref(),
            trust: self.settings.trust,
        };
        let pushed = match crate::push::push(&push).await {
            Ok((crate::push::Pushed::MustDivert { head }, commits)) => {
                let diverted = self.divert(handle, &push, &head).await;
                diverted.map(|pushed| (pushed, commits))
            }
            pushed => pushed,
        };
        // one that failed counts too, so a failing remote isn't hammered;
        // one with nothing to push doesn't
        if !matches!(pushed, Ok((crate::push::Pushed::Nothing, _))) {
            *tracked.last_push.lock().expect("last push") = Some(std::time::Instant::now());
            self.pushes.fetch_add(1, Ordering::SeqCst);
        }
        match pushed {
            Ok((pushed, commits)) => {
                tracked.push_failures.store(0, Ordering::SeqCst);
                if let Err(e) = handle.set_push_pending(false).await {
                    tracing::warn!(error = %e, "can't record a push as made");
                }
                self.pushed(tracked, handle, &pushed, &commits).await;
                Again::No
            }
            // credentials refused -- the gateway let a developer write,
            // so whose were stale -- or no answer: another grant may push
            // it; a grant is kept, which may still read
            Err(e) => {
                tracing::warn!(
                    checkout = %handle.repo_path().display(),
                    error = %e,
                    "can't push this cloudmap checkout; pushed again later"
                );
                tracked.push_failures.fetch_add(1, Ordering::SeqCst);
                match e {
                    // until someone changes it: a write or pull pushes again
                    RefreshError::Untrusted(_) => Again::No,
                    _ if e.refused() => Again::WithAGrant,
                    _ => Again::Same,
                }
            }
        }
    }

    /// Divert `handle`'s checkout, whose branch its remote won't take from
    /// `head`: first a worktree of the branch the commits go to, forked at
    /// the commit the database has, so their records stay readable, by id,
    /// once the checkout is reset. The fork is a convenience only: a
    /// checkout of the branch takes the ids from its rollups anyway, so
    /// one that fails is no reason not to divert.
    async fn divert(
        &self,
        handle: &SyncedRepo,
        push: &crate::push::Push<'_>,
        head: &str,
    ) -> Result<crate::push::Pushed, RefreshError> {
        crate::push::can_divert(push).await?;
        let branch = crate::push::diverted_branch(head);
        // only a fork this call made is its own to delete
        let forked = match head.parse() {
            Ok(pushed) => handle.fork_branch(&branch, pushed).await,
            Err(e) => Err(e),
        };
        let forked = match forked {
            Ok(forked) => forked.created,
            Err(e) => {
                tracing::warn!(
                    checkout = %handle.repo_path().display(),
                    branch = branch.as_str(),
                    error = %e,
                    "can't keep the records of commits a push diverts readable on their branch"
                );
                false
            }
        };
        let diverted = crate::push::divert(push).await;
        // rejected on the new branch too: no branch there for the row to be
        // of. (One that couldn't be pushed keeps it: the push is tried again.)
        if forked && matches!(diverted, Ok(crate::push::Pushed::Refused { .. })) {
            if let Err(e) = handle.delete_branch(&branch).await {
                tracing::warn!(branch = branch.as_str(), error = %e, "can't delete a branch's fork");
            }
        }
        diverted
    }

    /// Record what came of pushing `commits`: the branch's head, if
    /// pushed, or else a rescan of the checkout reset to its remote.
    async fn pushed(
        &self,
        tracked: &Tracked,
        handle: &SyncedRepo,
        pushed: &crate::push::Pushed,
        commits: &[String],
    ) {
        use crate::push::Pushed;
        let Some(status) = pushed.status() else {
            return;
        };
        let mut outcome = serde_json::json!({"status": status});
        match pushed {
            Pushed::Pushed { head } | Pushed::Merged { head } => {
                let prefix = &self.settings.cache_key_prefix;
                let key = crate::config::head_key(prefix, &tracked.project, &tracked.branch);
                self.records.put(&key, head, self.settings.record_ttl).await;
                outcome["new_commit"] = head.as_str().into();
            }
            Pushed::Diverted { branch } => outcome["to_branch"] = branch.as_str().into(),
            Pushed::Refused { branch, error } => {
                outcome["to_branch"] = branch.as_str().into();
                outcome["message"] = error.as_str().into();
            }
            Pushed::Nothing | Pushed::MustDivert { .. } => {}
        }
        // HEAD moved: merged, or reset to the remote
        if !matches!(pushed, Pushed::Pushed { .. } | Pushed::MustDivert { .. }) {
            self.rescan(handle).await;
        }
        self.record_push(tracked, commits, &outcome).await;
    }

    /// Record `outcome` as what became of pushing each of `commits`.
    async fn record_push(
        &self,
        tracked: &Tracked,
        commits: &[String],
        outcome: &serde_json::Value,
    ) {
        let value = outcome.to_string();
        for commit in commits {
            let key = self.push_key(tracked, commit);
            self.records
                .put(&key, &value, self.settings.record_ttl)
                .await;
        }
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

    /// The lock `handle`'s checkout's pulls and pushes hold.
    pub async fn checkout_lock(&self, handle: &SyncedRepo) -> Option<Arc<tokio::sync::Mutex<()>>> {
        Some(self.tracked(handle).await?.pulling.clone())
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
            project: project.clone(),
            url: format!("{}/{project}.git", s.cloud_server.trim_end_matches('/')),
            cloned: under("public") || under("private"),
            private: under("private"),
            origin: worktree.origin,
            branch: worktree.branch,
            pulling: Default::default(),
            last_push: Default::default(),
            push_scheduled: Default::default(),
            push_failures: Default::default(),
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
            // its commits the remote lacks first, with a grant only: they
            // may be another's
            if self.push_wait(tracked).is_zero() {
                self.push_locked(tracked, handle, None).await;
            }
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

/// [`Freshener::retry_delay`], after `failures` failed pushes, with a push
/// interval of `interval` seconds.
fn backoff(interval: u64, failures: u32) -> std::time::Duration {
    let base = std::time::Duration::from_secs(interval).max(RETRY_DELAY);
    let doubled = base.saturating_mul(1 << failures.saturating_sub(1).min(16));
    doubled.min(RETRY_MAX)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn retries_back_off_from_the_interval_to_an_hour() {
        let secs = |interval, failures| backoff(interval, failures).as_secs();
        assert_eq!(secs(0, 1), 1, "a second at the least");
        assert_eq!(secs(0, 2), 2);
        assert_eq!(secs(60, 1), 60);
        assert_eq!(secs(60, 3), 240);
        assert_eq!(secs(60, 40), 3600, "an hour at the most");
        assert_eq!(secs(u64::MAX, 1), 3600);
    }
}
