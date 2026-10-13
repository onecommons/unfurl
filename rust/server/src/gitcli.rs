// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Running git in a checkout the server may not trust: with only the
//! environment it needs, its own config overridden where it would run a
//! program or send a credential elsewhere, credentials for the cloud
//! server in its environment, never stored, and redacted from its errors;
//! and the lock file python holds while it clones.

use std::path::{Path, PathBuf};
use std::time::Duration;

use unfurl_git_sync::git::Trust;

use crate::grants::Credentials;

/// Whether git's error `error` is a refusal of the credentials it had, or
/// of none: the remote wants credentials (401), others (403), or answers
/// as if the repository weren't there (404), as GitLab and GitHub answer
/// credentials that can't see it. Not a missing branch, which credentials
/// that work meet too.
pub(crate) fn refused(error: &str) -> bool {
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
pub(crate) struct Access {
    protocol: String,
    env: Vec<(String, String)>,
    /// How far the checkout's own git config is trusted.
    pub(crate) trust: Trust,
}

impl Access {
    pub(crate) fn anonymous(url: &str) -> Self {
        Access {
            protocol: protocol_of(url),
            env: Vec::new(),
            trust: Trust::Full,
        }
    }

    pub(crate) fn trusting(mut self, trust: Trust) -> Self {
        self.trust = trust;
        self
    }

    /// `credentials` for `url`'s whole host: a `url.<...>.insteadOf`
    /// rewrite, as python's `credentials_config`.
    pub(crate) fn with(url: &str, credentials: &Credentials) -> Self {
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

/// `work`, holding the lock file python's `lock_file` holds while it
/// clones `path`: created exclusively, with this process's pid in it.
pub(crate) async fn holding_lock_file<R>(
    path: &Path,
    work: impl std::future::Future<Output = R>,
) -> Result<R, String> {
    let lock = lock_path(path);
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
    // removed however `work` ends, dropped unfinished included
    let _held = LockFile(lock);
    Ok(work.await)
}

/// A lock file this process created, removed when dropped.
struct LockFile(PathBuf);

impl Drop for LockFile {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
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

/// What every git command overrides in a checkout's own config, when it
/// isn't trusted: what runs a program, or sends a credential elsewhere.
const DISTRUSTED: [&str; 14] = [
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "credential.helper=",
    "-c",
    "http.sslVerify=true",
    "-c",
    "http.proxy=",
    // the clone leaves submodules out; a pull mustn't bring them
    "-c",
    "submodule.recurse=false",
    "-c",
    "fetch.recurseSubmodules=false",
];

/// Refuse the checkout at `path` if it redirects git, by its own config
/// (local or per worktree) in a way an override can't undo, or by files:
/// a `.git` file pointing elsewhere, alternate object stores.
pub(crate) async fn refuse_untrusted_config(path: &Path, access: &Access) -> Result<(), String> {
    let refuse = |what: &str| Err(format!("{}: {what}, which isn't trusted", path.display()));
    let git_dir = path.join(".git");
    if !git_dir.is_dir() {
        return refuse(".git isn't a directory");
    }
    for alternates in ["alternates", "http-alternates"] {
        let file = git_dir.join("objects/info").join(alternates);
        if std::fs::metadata(&file).is_ok_and(|m| m.len() > 0) {
            return refuse(&format!("it has objects/info/{alternates}"));
        }
    }
    let listed = git(
        path,
        access,
        &["config", "--name-only", "--list", "--show-scope"],
    )
    .await?;
    // ours come from the command line; the rest are the checkout's own
    let own = listed
        .lines()
        .filter_map(|line| line.split_once('\t'))
        .filter(|(scope, _)| *scope != "command");
    match own.map(|(_, name)| name).find(|name| untrusted(name)) {
        Some(name) => refuse(&format!("its git config sets {name}")),
        None => Ok(()),
    }
}

/// Whether config key `name`, as `git config --name-only` lists it, is
/// one [`refuse_untrusted_config`] refuses.
fn untrusted(name: &str) -> bool {
    let name = name.to_ascii_lowercase();
    // any http setting: a url-specific one wins over the overrides
    ["filter.", "diff.", "include.", "includeif.", "http."]
        .iter()
        .any(|section| name.starts_with(section))
        || (name.starts_with("url.")
            && (name.ends_with(".insteadof") || name.ends_with(".pushinsteadof")))
        || (name.starts_with("remote.")
            && [".proxy", ".uploadpack", ".receivepack", ".vcs"]
                .iter()
                .any(|key| name.ends_with(key)))
        || [
            "core.worktree",
            "core.askpass",
            "core.sshcommand",
            "core.gitproxy",
        ]
        .contains(&name.as_str())
}

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
pub(crate) async fn git(dir: &Path, access: &Access, args: &[&str]) -> Result<String, String> {
    let distrusted: &[&str] = match access.trust {
        Trust::Reduced => &DISTRUSTED,
        _ => &[],
    };
    let mut command = tokio::process::Command::new("git");
    command.env_clear();
    for name in KEPT_ENV {
        if let Some(value) = std::env::var_os(name) {
            command.env(name, value);
        }
    }
    // never a repository above `dir`: its config would apply
    if let Some(above) = dir
        .canonicalize()
        .ok()
        .and_then(|d| d.parent().map(Path::to_path_buf))
    {
        command.env("GIT_CEILING_DIRECTORIES", above);
    }
    let run = command
        .args(distrusted)
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

    /// The lock file goes with its work, however that ends: dropped
    /// unfinished, as a request a client gave up on is.
    #[tokio::test]
    async fn a_lock_file_goes_when_its_work_is_dropped() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let path = tmp.path().join("checkout");
        let held = holding_lock_file(&path, std::future::pending::<()>());
        let dropped = tokio::time::timeout(std::time::Duration::from_millis(50), held).await;
        assert!(dropped.is_err(), "still working when dropped");
        assert!(!lock_path(&path).exists());
    }

    #[test]
    fn config_that_redirects_git_is_untrusted() {
        for name in [
            "filter.lfs.smudge",
            "diff.evil.textconv",
            "include.path",
            "includeIf.gitdir:/x/.path",
            "http.https://h/.extraHeader",
            "url.https://evil/.insteadOf",
            "url.https://evil/.pushInsteadOf",
            "core.worktree",
            "http.https://unfurl.cloud/.proxy",
            "http.sslCAInfo",
            "http.curloptResolve",
            "core.askPass",
            "core.sshCommand",
            "remote.origin.proxy",
            "remote.origin.uploadpack",
        ] {
            assert!(untrusted(name), "{name}");
        }
        for name in [
            "remote.origin.url",
            "remote.origin.fetch",
            "user.name",
            "core.symlinks",
            "core.bare",
            "branch.main.remote",
            "remote.origin.pushurl",
        ] {
            assert!(!untrusted(name), "{name}");
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
}
