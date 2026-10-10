// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! The lock python's server holds around git work in one of its clones
//! (`git_lock` in `unfurl/server/serve.py`), taken here around the git work
//! this server does in a clone python may also pull, write or clear.

use std::path::{Path, PathBuf};
use std::time::Duration;

use once_cell::sync::Lazy;
use redis::aio::MultiplexedConnection;

/// How long a lock lasts if its holder dies without releasing it: python's
/// `_cache_inflight_timeout`.
pub const TTL: Duration = Duration::from_secs(120);

/// How often a wait for the lock checks it again.
const POLL: Duration = Duration::from_millis(200);

/// Delete the lock only if its holder still holds it.
static RELEASE: Lazy<redis::Script> = Lazy::new(|| {
    redis::Script::new(
        r"
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
",
    )
});

/// Extend the lock, if its holder still holds it.
static RENEW: Lazy<redis::Script> = Lazy::new(|| {
    redis::Script::new(
        r"
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
",
    )
});

/// Where git locks are kept: python's cache, under its key prefix.
#[derive(Clone)]
pub struct GitLocks {
    conn: MultiplexedConnection,
    prefix: String,
    ttl: Duration,
}

/// A held git lock, renewed until [`GitLock::release`] gives it up.
pub struct GitLock {
    conn: MultiplexedConnection,
    key: String,
    token: String,
    renewer: tokio::task::JoinHandle<()>,
    released: bool,
}

impl GitLocks {
    pub fn new(conn: MultiplexedConnection, prefix: &str) -> Self {
        GitLocks {
            conn,
            prefix: prefix.to_string(),
            ttl: TTL,
        }
    }

    /// These locks, lapsing `ttl` after they were last renewed.
    pub fn with_ttl(mut self, ttl: Duration) -> Self {
        self.ttl = ttl;
        self
    }

    /// Take the lock on the clone at `dir`, waiting up to `wait` for its
    /// holder: `None` if it's still held then.
    pub async fn lock(&self, dir: &Path, wait: Duration) -> redis::RedisResult<Option<GitLock>> {
        let key = format!("{}_git_lock::{}", self.prefix, real_path(dir).display());
        // an int, as python's holders write theirs, unique to this holder
        let token = std::hash::BuildHasher::hash_one(
            &std::collections::hash_map::RandomState::new(),
            (std::process::id(), &key),
        )
        .to_string();
        let mut conn = self.conn.clone();
        let deadline = tokio::time::Instant::now() + wait;
        loop {
            let taken: Option<String> = redis::cmd("SET")
                .arg(&key)
                .arg(&token)
                .arg("NX")
                .arg("PX")
                .arg(self.ttl.as_millis() as u64)
                .query_async(&mut conn)
                .await?;
            if taken.is_some() {
                let renewer =
                    tokio::spawn(renew(conn.clone(), key.clone(), token.clone(), self.ttl));
                return Ok(Some(GitLock {
                    conn,
                    key,
                    token,
                    renewer,
                    released: false,
                }));
            }
            if tokio::time::Instant::now() >= deadline {
                return Ok(None);
            }
            tokio::time::sleep(POLL).await;
        }
    }
}

/// Extend the lock `key`, held by `token`, every third of its `ttl`, while
/// it's held: git work can outlast the TTL.
async fn renew(mut conn: MultiplexedConnection, key: String, token: String, ttl: Duration) {
    loop {
        tokio::time::sleep(ttl / 3).await;
        let renewed: redis::RedisResult<i32> = RENEW
            .key(&key)
            .arg(&token)
            .arg(ttl.as_millis() as u64)
            .invoke_async(&mut conn)
            .await;
        if let Err(e) = renewed {
            tracing::warn!(
                key = key.as_str(),
                error = e.to_string().as_str(),
                "can't renew a git lock"
            );
        }
    }
}

/// Take the lock on the clone at `dir` from `locks`, waiting up to `wait`:
/// `Ok(None)` without locks, `Err` if it's still held then. A lock that
/// can't be read is logged, and the work goes ahead without it.
pub async fn hold(
    locks: Option<&GitLocks>,
    dir: &Path,
    wait: Duration,
) -> Result<Option<GitLock>, String> {
    let Some(locks) = locks else {
        return Ok(None);
    };
    match locks.lock(dir, wait).await {
        Ok(Some(lock)) => Ok(Some(lock)),
        Ok(None) => Err(format!(
            "git work in {} is still in progress",
            dir.display()
        )),
        Err(e) => {
            tracing::warn!(error = e.to_string().as_str(), "can't take a git lock");
            Ok(None)
        }
    }
}

/// Give up `lock`, if one was taken.
pub async fn release(lock: Option<GitLock>) {
    if let Some(lock) = lock {
        lock.release().await;
    }
}

impl GitLock {
    pub async fn release(mut self) {
        self.renewer.abort();
        self.released = true;
        release_key(self.conn.clone(), self.key.clone(), self.token.clone()).await;
    }
}

impl Drop for GitLock {
    /// One dropped unreleased, as a cancelled request's is, is released in
    /// the background.
    fn drop(&mut self) {
        self.renewer.abort();
        if self.released {
            return;
        }
        if let Ok(runtime) = tokio::runtime::Handle::try_current() {
            runtime.spawn(release_key(
                self.conn.clone(),
                self.key.clone(),
                self.token.clone(),
            ));
        }
    }
}

/// Delete the lock `key` if `token` still holds it.
async fn release_key(mut conn: MultiplexedConnection, key: String, token: String) {
    let released: redis::RedisResult<i32> =
        RELEASE.key(&key).arg(&token).invoke_async(&mut conn).await;
    if let Err(e) = released {
        tracing::warn!(
            key = key.as_str(),
            error = e.to_string().as_str(),
            "can't release a git lock; it expires on its own"
        );
    }
}

/// `dir` with symlinks resolved, as python's `os.path.realpath` does: in
/// the part of it that exists, with the rest as it is.
pub fn real_path(dir: &Path) -> PathBuf {
    let mut rest = Vec::new();
    let mut at = dir;
    loop {
        if let Ok(real) = at.canonicalize() {
            return rest.iter().rev().fold(real, |p, part| p.join(part));
        }
        match (at.parent(), at.file_name()) {
            (Some(parent), Some(name)) => {
                rest.push(name.to_owned());
                at = parent;
            }
            _ => return dir.to_path_buf(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_path_is_resolved_where_it_exists() {
        let tmp = tempfile::tempdir().expect("tmp");
        let real = tmp.path().canonicalize().expect("real");
        assert_eq!(real_path(tmp.path()), real);
        assert_eq!(real_path(&tmp.path().join("a/b")), real.join("a/b"));
    }
}
