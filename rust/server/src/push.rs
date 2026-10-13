// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Pushing a cloudmap checkout's commits to its remote: see "Pushing" in
//! the server's README.

use std::path::Path;

use unfurl_git_sync::git::Trust;

use crate::clone::RefreshError;
use crate::gitcli::{
    git, holding_lock_file, redact, refuse_other_origin, refuse_untrusted_config, run_git, Access,
};
use crate::grants::Credentials;

/// What a push of a checkout came to.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Pushed {
    /// It had nothing its remote lacks.
    Nothing,
    /// Its remote took its branch, now at `head`.
    Pushed { head: String },
    /// Its remote's branch had moved on, and merged cleanly: the merge,
    /// `head`, is checked out and pushed.
    Merged { head: String },
    /// Its remote's branch had moved on and doesn't merge cleanly, or
    /// rejected them (a protected branch, a hook): its commits went to a
    /// new `branch` there, and it was reset to its remote.
    Diverted { branch: String },
    /// Its remote rejected them on a new branch too: they were kept on a
    /// local `branch`, which is never pushed, and it was reset to its
    /// remote.
    Refused { branch: String, error: String },
}

impl Pushed {
    /// The status `/events` reports for a commit it was about.
    pub fn status(&self) -> Option<&'static str> {
        match self {
            Pushed::Nothing => None,
            Pushed::Pushed { .. } | Pushed::Merged { .. } => Some("pushed"),
            Pushed::Diverted { .. } => Some("diverted"),
            Pushed::Refused { .. } => Some("refused"),
        }
    }
}

/// A checkout to push, and how to reach its remote.
pub struct Push<'a> {
    pub path: &'a Path,
    /// What its credentials are for: its project's url.
    pub url: &'a str,
    /// The branch it's on.
    pub branch: &'a str,
    pub credentials: Option<&'a Credentials>,
    pub trust: Trust,
}

impl Push<'_> {
    fn access(&self) -> Access {
        match self.credentials {
            Some(credentials) => Access::with(self.url, credentials),
            None => Access::anonymous(self.url),
        }
        .trusting(self.trust)
    }
}

/// Push the checkout's branch, and the `git-sync/` branches made in it
/// that its remote lacks; its caller holds its git lock. Python's lock
/// file is held only while the working tree changes. With the commits on its branch the push was about, newest
/// first. `Err` where its remote couldn't be reached or refused its
/// credentials, and nothing changed.
pub async fn push(p: &Push<'_>) -> Result<(Pushed, Vec<String>), RefreshError> {
    push_with(p, &p.access()).await
}

async fn push_with(p: &Push<'_>, access: &Access) -> Result<(Pushed, Vec<String>), RefreshError> {
    pushable(p, access).await?;
    // none without an upstream: on no branch, there's nothing to push
    let ahead: Vec<String> = git(p.path, access, &["rev-list", "@{upstream}..HEAD"])
        .await
        .map(|out| out.lines().map(str::to_string).collect())
        .unwrap_or_default();
    let sides = side_branches(p.path, access).await?;
    let Some(head) = ahead.first() else {
        if !sides.is_empty() {
            push_refs(p, access, None, &sides).await?;
        }
        return Ok((Pushed::Nothing, ahead));
    };
    let main = format!("refs/heads/{}", p.branch);
    let (stdout, stderr) = push_refs(p, access, Some(&main), &sides).await?;
    let pushed = match ref_status(&stdout, &main) {
        // refused credentials, or no answer: left for the next push
        None => return Err(RefreshError::Remote(format!("git push: {stderr}"))),
        Some(('!', summary)) if moved_on(summary) => merge_or_divert(p, access, &main).await?,
        Some(('!', _)) => divert(p, access).await?,
        Some(_) => Pushed::Pushed { head: head.clone() },
    };
    Ok((pushed, ahead))
}

/// Merge the remote's branch, which has moved on, into the checkout and
/// push the merge, if it merges cleanly; otherwise divert.
async fn merge_or_divert(
    p: &Push<'_>,
    access: &Access,
    main: &str,
) -> Result<Pushed, RefreshError> {
    let Some(merge) = merge_upstream(p, access).await? else {
        return divert(p, access).await;
    };
    let (stdout, stderr) = push_refs(p, access, Some(main), &[]).await?;
    match ref_status(&stdout, main) {
        None => Err(RefreshError::Remote(format!("git push: {stderr}"))),
        // moved on again, or rejected
        Some(('!', _)) => divert(p, access).await,
        Some(_) => Ok(Pushed::Merged { head: merge }),
    }
}

/// Fetch, and merge the remote's branch into HEAD if it merges cleanly:
/// the merge, made in memory, committed onto both and the checkout
/// fast-forwarded to it. `None` where it doesn't merge cleanly, which
/// leaves the checkout as it is.
async fn merge_upstream(p: &Push<'_>, access: &Access) -> Result<Option<String>, RefreshError> {
    let local = async |args: &[&str]| git(p.path, access, args).await.map_err(RefreshError::Local);
    let mut fetch = vec!["fetch", "-q", "origin"];
    // a merge needs the history a shallow clone of python's lacks
    if local(&["rev-parse", "--is-shallow-repository"]).await? == "true" {
        fetch.push("--unshallow");
    }
    git(p.path, access, &fetch)
        .await
        .map_err(RefreshError::Remote)?;
    let merged = run_git(
        p.path,
        access,
        &["merge-tree", "--write-tree", "HEAD", "@{upstream}"],
    )
    .await
    .map_err(RefreshError::Local)?;
    match merged.status.code() {
        Some(0) => {}
        // conflicts
        Some(1) => return Ok(None),
        // can't merge them at all: unrelated histories, or a git without
        // `merge-tree --write-tree` (2.38)
        _ => {
            tracing::warn!(
                checkout = %p.path.display(),
                error = redact(String::from_utf8_lossy(&merged.stderr).trim()).as_str(),
                "can't merge this cloudmap checkout with its remote"
            );
            return Ok(None);
        }
    }
    let stdout = String::from_utf8_lossy(&merged.stdout);
    let tree = stdout.lines().next().unwrap_or_default().to_string();
    let upstream = local(&["rev-parse", "@{upstream}"]).await?;
    let message = format!("Merge {upstream} into {}", p.branch);
    let committing = access.clone().with_env(identity().await);
    let args = [
        "commit-tree",
        &tree,
        "-p",
        "HEAD",
        "-p",
        &upstream,
        "-m",
        &message,
    ];
    let commit = git(p.path, &committing, &args)
        .await
        .map_err(RefreshError::Local)?;
    let fast_forward = ["merge", "-q", "--ff-only", &commit];
    holding_lock_file(p.path, local(&fast_forward))
        .await
        .map_err(RefreshError::Local)??;
    Ok(Some(commit))
}

/// The identity the server commits a merge as: its own git identity, as
/// git-sync's commits take it, or `unfurl-server`.
async fn identity() -> &'static [(String, String)] {
    static IDENTITY: tokio::sync::OnceCell<Vec<(String, String)>> =
        tokio::sync::OnceCell::const_new();
    IDENTITY
        .get_or_init(async || {
            // outside any repository, whose config would say otherwise
            let ident = tokio::process::Command::new("git")
                .args(["var", "GIT_COMMITTER_IDENT"])
                .current_dir("/")
                .output()
                .await
                .ok()
                .filter(|out| out.status.success())
                .map(|out| String::from_utf8_lossy(&out.stdout).trim().to_string());
            let (name, email) = ident
                .as_deref()
                .and_then(|ident| {
                    let (name, rest) = ident.split_once(" <")?;
                    Some((name.to_string(), rest.split_once('>')?.0.to_string()))
                })
                .unwrap_or_else(|| ("unfurl-server".into(), "unfurl-server@localhost".into()));
            ["AUTHOR", "COMMITTER"]
                .iter()
                .flat_map(|who| {
                    [
                        (format!("GIT_{who}_NAME"), name.clone()),
                        (format!("GIT_{who}_EMAIL"), email.clone()),
                    ]
                })
                .collect()
        })
        .await
}

/// Refuse a checkout that isn't one, whose own config isn't trusted,
/// whose remote isn't its project, or that's on another branch than its
/// own: its HEAD would be pushed onto its branch.
async fn pushable(p: &Push<'_>, access: &Access) -> Result<(), RefreshError> {
    if !p.path.join(".git").exists() {
        let missing = format!("{} isn't a git checkout", p.path.display());
        return Err(RefreshError::Local(missing));
    }
    if access.trust == Trust::Reduced {
        refuse_untrusted_config(p.path, access)
            .await
            .map_err(RefreshError::Untrusted)?;
    }
    refuse_other_origin(p.path, access, p.url)
        .await
        .map_err(RefreshError::Untrusted)?;
    let on = git(p.path, access, &["symbolic-ref", "-q", "--short", "HEAD"])
        .await
        .unwrap_or_default();
    if on != p.branch {
        let other = format!("{} is on {on:?}, not {}", p.path.display(), p.branch);
        return Err(RefreshError::Untrusted(other));
    }
    Ok(())
}

/// Push HEAD to `main`, if given, and the `sides`, each to its own name:
/// git's porcelain output and its errors. A side branch refused is logged.
async fn push_refs(
    p: &Push<'_>,
    access: &Access,
    main: Option<&str>,
    sides: &[String],
) -> Result<(String, String), RefreshError> {
    let head = main.map(|main| format!("HEAD:{main}"));
    let refspecs: Vec<String> = head
        .into_iter()
        .chain(sides.iter().map(|b| format!("{b}:{b}")))
        .collect();
    let mut args = vec!["push", "--porcelain", "origin"];
    args.extend(refspecs.iter().map(String::as_str));
    let out = run_git(p.path, access, &args)
        .await
        .map_err(RefreshError::Remote)?;
    let stdout = String::from_utf8_lossy(&out.stdout).to_string();
    let stderr = redact(String::from_utf8_lossy(&out.stderr).trim());
    for side in sides {
        if let Some(('!', summary)) = ref_status(&stdout, side) {
            tracing::warn!(
                branch = side.as_str(),
                summary,
                "a git-sync branch wasn't pushed"
            );
        }
    }
    Ok((stdout, stderr))
}

/// Whether a ref's push was rejected because the remote's moved on.
fn moved_on(summary: &str) -> bool {
    summary.contains("non-fast-forward") || summary.contains("fetch first")
}

/// The local `git-sync/` branches its remote hasn't got as they are, but
/// for those kept from a refused push, which are never pushed.
async fn side_branches(path: &Path, access: &Access) -> Result<Vec<String>, RefreshError> {
    let list = [
        "for-each-ref",
        "--format=%(refname)",
        "refs/heads/git-sync/",
    ];
    let refs = git(path, access, &list)
        .await
        .map_err(RefreshError::Local)?;
    let mut sides = Vec::new();
    for name in refs
        .lines()
        .filter(|r| !r.starts_with("refs/heads/git-sync/refused-"))
    {
        let tracking = name.replacen("refs/heads/", "refs/remotes/origin/", 1);
        let local = git(path, access, &["rev-parse", name]).await.ok();
        let remote = git(path, access, &["rev-parse", "-q", "--verify", &tracking])
            .await
            .ok();
        if local.is_some() && local != remote {
            sides.push(name.to_string());
        }
    }
    Ok(sides)
}

/// The flag and summary `git push --porcelain` reported for the remote ref
/// `to`.
fn ref_status<'a>(stdout: &'a str, to: &str) -> Option<(char, &'a str)> {
    stdout.lines().find_map(|line| {
        let mut fields = line.split('\t');
        let flag = fields.next()?.chars().next()?;
        let (_, dest) = fields.next()?.split_once(':')?;
        (dest == to).then(|| (flag, fields.next().unwrap_or_default()))
    })
}

/// Push HEAD, which its remote's branch won't take, to a new branch there,
/// `git-sync/diverted-<commit>`, and reset to the remote; if that's
/// rejected too, set it aside.
async fn divert(p: &Push<'_>, access: &Access) -> Result<Pushed, RefreshError> {
    // before anything is pushed or kept: a reset would lose it
    resettable(p, access).await?;
    let head = git(p.path, access, &["rev-parse", "HEAD"])
        .await
        .map_err(RefreshError::Local)?;
    let branch = format!("git-sync/diverted-{}", &head[..12.min(head.len())]);
    let to = format!("refs/heads/{branch}");
    let refspec = format!("HEAD:{to}");
    let out = run_git(p.path, access, &["push", "--porcelain", "origin", &refspec])
        .await
        .map_err(RefreshError::Remote)?;
    let stdout = String::from_utf8_lossy(&out.stdout).to_string();
    let stderr = redact(String::from_utf8_lossy(&out.stderr).trim());
    match ref_status(&stdout, &to) {
        None => return Err(RefreshError::Remote(format!("git push: {stderr}"))),
        Some(('!', summary)) => {
            return set_aside(p, access, &head, &format!("{summary} {stderr}")).await;
        }
        Some(_) => {}
    }
    git(p.path, access, &["fetch", "-q", "origin"])
        .await
        .map_err(RefreshError::Remote)?;
    reset_to_upstream(p, access).await?;
    tracing::warn!(
        checkout = %p.path.display(),
        branch = branch.as_str(),
        "its remote's branch won't take this cloudmap checkout's commits: they were pushed \
         to a new branch to merge, and it was reset to its remote"
    );
    Ok(Pushed::Diverted { branch })
}

/// Keep the commits a push of the checkout was refused for on a local
/// branch, `git-sync/refused-<commit>`, never pushed, and reset it to its
/// remote, so no later push carries them with other credentials.
async fn set_aside(
    p: &Push<'_>,
    access: &Access,
    head: &str,
    error: &str,
) -> Result<Pushed, RefreshError> {
    let branch = format!("git-sync/refused-{}", &head[..12.min(head.len())]);
    resettable(p, access).await?;
    git(p.path, access, &["branch", "-f", &branch, "HEAD"])
        .await
        .map_err(RefreshError::Local)?;
    reset_to_upstream(p, access).await?;
    tracing::error!(
        checkout = %p.path.display(),
        branch = branch.as_str(),
        error,
        "a push of this cloudmap checkout was rejected, even to a new branch: its commits were \
         kept on a local branch, and it was reset to its remote"
    );
    Ok(Pushed::Refused {
        branch,
        error: error.to_string(),
    })
}

/// Refuse a checkout whose working tree holds changes a reset would lose.
/// Records the database has staged aren't such: the rescan after a reset
/// keeps them, to commit onto what it's reset to.
async fn resettable(p: &Push<'_>, access: &Access) -> Result<(), RefreshError> {
    let status = ["status", "--porcelain", "--untracked-files=all"];
    let changes = git(p.path, access, &status)
        .await
        .map_err(RefreshError::Local)?;
    if !changes.is_empty() {
        let kept = format!(
            "{} holds changes a reset would lose: not reset",
            p.path.display()
        );
        return Err(RefreshError::Local(kept));
    }
    Ok(())
}

/// Reset the checkout to its upstream, as [`resettable`] found it.
async fn reset_to_upstream(p: &Push<'_>, access: &Access) -> Result<(), RefreshError> {
    let reset = git(p.path, access, &["reset", "-q", "--hard", "@{upstream}"]);
    holding_lock_file(p.path, reset)
        .await
        .map_err(RefreshError::Local)?
        .map(|_| ())
        .map_err(RefreshError::Local)
}

#[cfg(test)]
mod tests {
    use super::ref_status;

    #[test]
    fn a_ref_s_status_is_read_from_porcelain_output() {
        let out = concat!(
            "To file:///r.git\n",
            " \tHEAD:refs/heads/main\tabc..def\n",
            "!\tHEAD:refs/heads/other\t[rejected] (non-fast-forward)\n",
            "=\trefs/heads/git-sync/x:refs/heads/git-sync/x\t[up to date]\n",
            "Done\n",
        );
        assert_eq!(ref_status(out, "refs/heads/main"), Some((' ', "abc..def")));
        assert_eq!(
            ref_status(out, "refs/heads/other"),
            Some(('!', "[rejected] (non-fast-forward)"))
        );
        assert_eq!(
            ref_status(out, "refs/heads/git-sync/x").map(|s| s.0),
            Some('=')
        );
        assert_eq!(ref_status(out, "refs/heads/none"), None);
    }
}
