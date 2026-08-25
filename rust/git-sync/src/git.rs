// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Thin gix helpers used by the rest of the crate.
//!
//! Wraps the parts of [`gix`] we need: open a working tree, list its
//! tracked files, derive `(origin, branch, head_oid)`, walk
//! `git log -- <path>` lazily ([`last_commits_for_paths`]), and create
//! a commit by overlaying blobs on top of the current HEAD tree.
use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::{Path, PathBuf};

use gix::bstr::ByteSlice;

use crate::error::{Error, Result};

fn git_err(e: impl std::fmt::Display) -> Error {
    Error::Git(e.to_string())
}

/// Open a git repository on disk.
pub fn open_repo(path: &Path) -> Result<gix::Repository> {
    gix::open(path).map_err(git_err)
}

/// Reduce a git URL to a stable identity for the repository it names.
///
/// Port of `normalize_git_url_hard` in `unfurl/repo.py` — that is,
/// `normalize_git_url(url, hard=3)` with the scheme and any fragment
/// stripped. The two implementations must agree: python keys its server
/// cache and repository identity on this, and a repository that reads as
/// two identities gets two of everything downstream.
///
/// Every spelling of one repository collapses to `host[:port]/path`:
///
/// ```
/// use unfurl_git_sync::git::normalize_git_url_hard as n;
/// let id = "unfurl.cloud/onecommons/cloudmap";
/// assert_eq!(n("https://unfurl.cloud/onecommons/cloudmap.git"), id);
/// assert_eq!(n("ssh://git@unfurl.cloud/onecommons/cloudmap.git"), id);
/// assert_eq!(n("git@unfurl.cloud:onecommons/cloudmap.git"), id);
/// assert_eq!(n("https://user:tok@unfurl.cloud/onecommons/cloudmap/"), id);
/// ```
///
/// So the scheme, any credentials, a trailing `/`, a `.git` suffix and a
/// `#revision:path` fragment are all dropped, scp-style syntax is
/// understood, and a non-default port is kept because it distinguishes
/// hosts. The host folds to lower case (DNS is case-insensitive) but the
/// **path does not**: forges preserve path case, and a case-sensitive
/// backend can serve `/Foo/bar` and `/foo/bar` as different
/// repositories — merging two repositories under one identity is a worse
/// failure than not merging one.
///
/// The result is idempotent, so a value normalized twice still matches
/// one normalized once.
pub fn normalize_git_url_hard(url: &str) -> String {
    // `git-local://<digest>[:<rest>]` identifies a repo by commit digest;
    // python truncates the netloc there and moves everything else into a
    // fragment, which the fragment strip then discards.
    if let Some(rest) = url.strip_prefix("git-local://") {
        let netloc = rest.split(['/', '?', '#']).next().unwrap_or("");
        return netloc.split(':').next().unwrap_or("").to_string();
    }

    // Absolute and home-relative paths become `file://` URLs, and python
    // returns them before the `hard` processing runs — so only the scheme
    // strip below applies to them.
    if !url.contains("://") {
        if let Some(path) = url
            .strip_prefix('~')
            .map(|rest| format!("{}{rest}", home_dir()))
            .or_else(|| url.starts_with('/').then(|| url.to_string()))
            .or_else(|| {
                url.strip_prefix("file:")
                    .map(|rest| rest.replacen('~', &home_dir(), 1))
            })
        {
            return lexical_abspath(&path);
        }
        // scp-style `user@host:path` is git syntax no URL parser accepts.
        if url.contains('@') {
            return normalize_parsed(&format!("ssh://{}", url.replacen(':', "/", 1)));
        }
    }
    normalize_parsed(url)
}

/// The `hard = 3` body of python's `normalize_git_url`, followed by its
/// scheme and fragment strip. Split out because the scp branch above
/// re-enters it after rewriting the URL.
fn normalize_parsed(url: &str) -> String {
    // Mirrors `urlparse`: a scheme is `alpha *( alnum / "+" / "-" / "." )`
    // followed by ":", and a netloc exists only when "//" follows it.
    let (scheme, rest) = match url.find(':') {
        Some(i)
            if i > 0
                && url[..i].starts_with(|c: char| c.is_ascii_alphabetic())
                && url[..i]
                    .chars()
                    .all(|c| c.is_ascii_alphanumeric() || matches!(c, '+' | '-' | '.')) =>
        {
            (Some(&url[..i]), &url[i + 1..])
        }
        _ => (None, url),
    };
    let (netloc, remainder) = match rest.strip_prefix("//") {
        Some(after) => {
            let end = after.find(['/', '?', '#']).unwrap_or(after.len());
            (Some(&after[..end]), &after[end..])
        }
        None => (None, rest),
    };

    let (before_frag, _) = split_once_at(remainder, '#');
    let (path, query) = split_once_at(before_frag, '?');

    // Drop a trailing "/" then a ".git", in that order — `a/b.git/`
    // normalizes the same as `a/b.git`.
    let path = path.trim_end_matches('/');
    let path = path.strip_suffix(".git").unwrap_or(path);

    let mut out = match netloc {
        // Credentials are identity-irrelevant and often secret.
        Some(netloc) => match netloc.rsplit_once('@') {
            Some((_, host)) => host.to_ascii_lowercase(),
            None => netloc.to_ascii_lowercase(),
        },
        // No netloc: python's `geturl()` keeps `scheme:path`, and the
        // scheme strip below finds no "://" to cut.
        None => match scheme {
            Some(scheme) => format!("{scheme}:"),
            None => String::new(),
        },
    };
    out.push_str(path);
    if let Some(query) = query {
        out.push('?');
        out.push_str(query);
    }
    out
}

/// `(before, after)` around the first `sep`; `after` is `None` when absent.
fn split_once_at(s: &str, sep: char) -> (&str, Option<&str>) {
    match s.split_once(sep) {
        Some((a, b)) => (a, Some(b)),
        None => (s, None),
    }
}

fn home_dir() -> String {
    std::env::var("HOME").unwrap_or_default()
}

/// Python's `os.path.abspath`: make absolute against the current
/// directory, then resolve `.` and `..` textually. Deliberately does not
/// touch the filesystem, so it does not follow symlinks either.
fn lexical_abspath(path: &str) -> String {
    let joined = if path.starts_with('/') {
        path.to_string()
    } else {
        let cwd = std::env::current_dir().unwrap_or_default();
        format!("{}/{path}", cwd.to_string_lossy())
    };
    let mut parts: Vec<&str> = Vec::new();
    for segment in joined.split('/') {
        match segment {
            "" | "." => {}
            ".." => {
                parts.pop();
            }
            other => parts.push(other),
        }
    }
    format!("/{}", parts.join("/"))
}

/// Resolve `(origin, branch, head_oid)` for a freshly-opened repo. Falls
/// back to the working-dir path when no remote is configured.
///
/// The origin is run through [`normalize_git_url_hard`], so the same
/// repository cloned over https by one user and ssh by another resolves
/// to one identity instead of two. `remote_names` returns a sorted set,
/// so a repository with several remotes yields whichever name sorts
/// first — a caller needing a specific one has to say so rather than
/// let it be guessed here.
pub fn worktree_meta(repo: &gix::Repository) -> Result<WorktreeMeta> {
    let origin = repo
        .remote_names()
        .iter()
        .find_map(|name| {
            let remote = repo.find_remote(name.as_ref()).ok()?;
            let url = remote.url(gix::remote::Direction::Fetch)?;
            Some(url.to_bstring().to_string())
        })
        .or_else(|| repo.work_dir().map(|p| p.to_string_lossy().to_string()))
        .map(|raw| normalize_git_url_hard(&raw))
        .unwrap_or_default();

    let branch = match repo.head().map_err(git_err)?.referent_name() {
        Some(r) => r.shorten().to_string(),
        None => "HEAD".to_string(),
    };

    let head_oid = repo.head_id().ok().map(|id| id.detach());

    Ok(WorktreeMeta {
        origin,
        branch,
        head_oid,
    })
}

#[derive(Debug, Clone)]
pub struct WorktreeMeta {
    pub origin: String,
    pub branch: String,
    pub head_oid: Option<gix::ObjectId>,
}

/// Iterate every tracked path in the gix index, paired with the
/// absolute on-disk location and the OID recorded in the index.
pub fn tracked_files(repo: &gix::Repository) -> Result<Vec<TrackedFile>> {
    let work_dir = repo
        .work_dir()
        .ok_or_else(|| Error::Git("repository has no working tree".to_string()))?
        .to_path_buf();
    let index = repo.index_or_load_from_head().map_err(git_err)?;

    let mut out = Vec::new();
    for entry in index.entries() {
        let path = entry.path(&index);
        let rel: String = match path.to_str() {
            Ok(s) => s.to_string(),
            Err(_) => continue,
        };
        let abs = work_dir.join(&rel);
        out.push(TrackedFile {
            rel_path: rel,
            abs_path: abs,
            head_blob_oid: entry.id,
        });
    }
    Ok(out)
}

#[derive(Debug, Clone)]
pub struct TrackedFile {
    pub rel_path: String,
    pub abs_path: PathBuf,
    /// OID recorded in the git index for this path.
    pub head_blob_oid: gix::ObjectId,
}

/// Compute the blob OID gix would record for the bytes currently in
/// `abs_path`.
pub fn blob_oid_for_disk_file(repo: &gix::Repository, abs_path: &Path) -> Result<gix::ObjectId> {
    let bytes = std::fs::read(abs_path)?;
    Ok(repo.write_blob(&bytes).map_err(git_err)?.detach())
}

/// Stage `paths` (relative to the work dir) and create a commit on HEAD
/// with the given message. Returns the new commit OID.
pub fn commit_paths(
    repo: &gix::Repository,
    paths: &[String],
    message: &str,
) -> Result<gix::ObjectId> {
    let work_dir = repo
        .work_dir()
        .ok_or_else(|| Error::Git("repository has no working tree".to_string()))?
        .to_path_buf();

    // For each path: read disk bytes, write a blob, capture (segments, oid).
    let mut updates: Vec<(Vec<String>, gix::ObjectId)> = Vec::new();
    for rel in paths {
        let abs = work_dir.join(rel);
        let bytes = std::fs::read(&abs)?;
        let blob_oid = repo.write_blob(&bytes).map_err(git_err)?.detach();
        let segments: Vec<String> = rel.split('/').map(|s| s.to_string()).collect();
        if segments.iter().any(|s| s.is_empty()) {
            return Err(Error::Other(format!("invalid path for commit: {rel}")));
        }
        updates.push((segments, blob_oid));
    }

    // Determine parent commit + base tree.
    let parents: Vec<gix::ObjectId> = match repo.head_id().ok() {
        Some(id) => vec![id.detach()],
        None => Vec::new(),
    };
    let head_tree_oid = match parents.first() {
        Some(cid) => Some(
            repo.find_commit(*cid)
                .map_err(git_err)?
                .tree_id()
                .map_err(git_err)?
                .detach(),
        ),
        None => None,
    };

    let new_tree_oid = build_tree_with_updates(repo, head_tree_oid, &updates)?;

    let id = repo
        .commit("HEAD", message, new_tree_oid, parents)
        .map_err(git_err)?;

    // Refresh the index to match the tree we just committed. Building the tree
    // directly (above) never touches the index, so without this it still holds
    // the pre-commit blobs: `git status` would report the just-committed files
    // as both staged-modified and worktree-modified, and a later `git commit -a`
    // by another tool could revert them. `index_from_tree` walks the whole tree,
    // which is fine for the small documents this crate syncs.
    let mut index = repo.index_from_tree(&new_tree_oid).map_err(git_err)?;
    index
        .write(gix::index::write::Options::default())
        .map_err(git_err)?;

    Ok(id.detach())
}

/// Build a new tree from `base_tree_oid` (or empty) with each entry in
/// `updates` overlaid (insert/replace).
fn build_tree_with_updates(
    repo: &gix::Repository,
    base_tree_oid: Option<gix::ObjectId>,
    updates: &[(Vec<String>, gix::ObjectId)],
) -> Result<gix::ObjectId> {
    use gix::objs::tree::{Entry, EntryKind, EntryMode};

    // Group updates by the first path component.
    let mut here_updates: BTreeMap<String, gix::ObjectId> = BTreeMap::new();
    let mut sub_updates: BTreeMap<String, Vec<(Vec<String>, gix::ObjectId)>> = BTreeMap::new();
    for (segments, oid) in updates {
        let mut iter = segments.iter().cloned();
        let head: String = iter.next().expect("non-empty segments");
        let rest: Vec<String> = iter.collect();
        if rest.is_empty() {
            here_updates.insert(head, *oid);
        } else {
            sub_updates.entry(head).or_default().push((rest, *oid));
        }
    }

    // Read existing tree entries (if any).
    let mut entries: BTreeMap<String, Entry> = BTreeMap::new();
    if let Some(tree_oid) = base_tree_oid {
        let tree = repo.find_tree(tree_oid).map_err(git_err)?;
        let decoded = tree.decode().map_err(git_err)?;
        for e in decoded.entries.iter() {
            let name: String = match e.filename.to_str() {
                Ok(s) => s.to_string(),
                Err(_) => continue,
            };
            entries.insert(
                name.clone(),
                Entry {
                    mode: e.mode,
                    filename: e.filename.into(),
                    oid: e.oid.into(),
                },
            );
        }
    }

    // Apply blob replacements at this level.
    for (name, oid) in here_updates {
        entries.insert(
            name.clone(),
            Entry {
                mode: EntryMode::from(EntryKind::Blob),
                filename: name.into(),
                oid,
            },
        );
    }

    // Recurse into subdirectories.
    for (subdir, sub_ups) in sub_updates {
        let existing_subtree = entries
            .get(&subdir)
            .filter(|e| e.mode.is_tree())
            .map(|e| e.oid);
        let new_subtree_oid = build_tree_with_updates(repo, existing_subtree, &sub_ups)?;
        entries.insert(
            subdir.clone(),
            Entry {
                mode: EntryMode::from(EntryKind::Tree),
                filename: subdir.into(),
                oid: new_subtree_oid,
            },
        );
    }

    let mut sorted: Vec<Entry> = entries.into_values().collect();
    sorted.sort();

    let tree = gix::objs::Tree { entries: sorted };
    let id = repo.write_object(&tree).map_err(git_err)?.detach();
    Ok(id)
}

/// Resolve the most recent commit oid that touched each path in
/// `paths`, walking ancestors of HEAD in reverse-chronological order.
///
/// Implements the lazy-batch algorithm: a single backwards walk maintains
/// a `path → commit_oid` map. Each commit is diffed against its first
/// parent (or the empty tree at the root); any path in the diff that is
/// still pending and that the caller asked about is recorded with this
/// commit's oid. The walk stops as soon as every requested path has
/// been resolved. Paths that are never seen (e.g. a file that exists
/// only in the working tree, never committed) are absent from the
/// returned map.
///
/// Cost: O(commits walked × tree-diff). Walks once for any number of
/// requested paths — far cheaper than resolving each path independently.
pub fn last_commits_for_paths(
    repo: &gix::Repository,
    paths: &[String],
) -> Result<HashMap<String, String>> {
    use gix::object::tree::diff::{Action, Change};

    if paths.is_empty() {
        return Ok(HashMap::new());
    }

    let head = match repo.head_id() {
        Ok(id) => id,
        // Unborn / empty repo: nothing to attribute.
        Err(_) => return Ok(HashMap::new()),
    };

    let mut pending: HashSet<String> = paths.iter().cloned().collect();
    let mut result: HashMap<String, String> = HashMap::new();

    let walker = head.ancestors().all().map_err(git_err)?;
    for info in walker {
        let info = info.map_err(git_err)?;
        let commit_id = info.id;
        let commit = repo.find_commit(commit_id).map_err(git_err)?;
        let tree = commit.tree().map_err(git_err)?;

        // Diff against first parent. Root commit has no parent → diff
        // against the empty tree (everything in `tree` is an addition).
        let first_parent_tree = match commit.parent_ids().next() {
            Some(p) => Some(
                repo.find_commit(p)
                    .map_err(git_err)?
                    .tree()
                    .map_err(git_err)?,
            ),
            None => None,
        };

        let oid_str = commit_id.to_string();

        // The visitor records every changed path that the caller asked
        // about and removes it from the pending set. We never abort
        // mid-commit; a single commit may resolve multiple paths.
        let mut visit =
            |change: Change<'_, '_, '_>| -> std::result::Result<Action, std::convert::Infallible> {
                if let Ok(path_str) = change.location.to_str() {
                    if pending.remove(path_str as &str) {
                        result.insert(path_str.to_string(), oid_str.clone());
                    }
                }
                Ok(Action::Continue)
            };

        let empty;
        let source: &gix::Tree<'_> = match first_parent_tree {
            Some(ref pt) => pt,
            None => {
                empty = repo.empty_tree();
                &empty
            }
        };
        let mut platform = source.changes().map_err(git_err)?;
        // Without `track_path`, `change.location` is always empty.
        platform.track_path();
        // Rename detection isn't useful for "last commit that touched
        // this path" attribution and just costs blob reads.
        platform.track_rewrites(None);
        platform
            .for_each_to_obtain_tree(&tree, &mut visit)
            .map_err(git_err)?;

        if pending.is_empty() {
            break;
        }
    }

    Ok(result)
}

/// Initialise a repository at `path` with an initial commit containing
/// `files` (relative path → bytes). Returns the commit OID. Used by
/// integration tests.
pub fn init_with_files(
    path: &Path,
    files: &[(String, Vec<u8>)],
    message: &str,
) -> Result<gix::ObjectId> {
    use std::fs;

    fs::create_dir_all(path)?;
    let repo = gix::init(path).map_err(git_err)?;
    for (rel, bytes) in files {
        let abs = path.join(rel);
        if let Some(parent) = abs.parent() {
            fs::create_dir_all(parent)?;
        }
        fs::write(&abs, bytes)?;
    }
    let paths: Vec<String> = files.iter().map(|(p, _)| p.clone()).collect();
    commit_paths(&repo, &paths, message)
}

#[cfg(test)]
mod normalize_tests {
    use super::normalize_git_url_hard as n;

    /// Every expected value here was produced by running python's
    /// `unfurl.repo.normalize_git_url_hard` on the input. The two
    /// implementations key the same things, so they have to agree
    /// character for character -- regenerate with:
    ///
    /// ```text
    /// python -c "from unfurl.repo import normalize_git_url_hard as n; print(n(URL))"
    /// ```
    #[test]
    fn matches_python() {
        const ID: &str = "unfurl.cloud/onecommons/cloudmap";
        for (url, expected) in [
            ("https://unfurl.cloud/onecommons/cloudmap.git", ID),
            ("https://unfurl.cloud/onecommons/cloudmap", ID),
            ("https://unfurl.cloud/onecommons/cloudmap/", ID),
            ("ssh://git@unfurl.cloud/onecommons/cloudmap.git", ID),
            ("git@unfurl.cloud:onecommons/cloudmap.git", ID),
            ("git://unfurl.cloud/onecommons/cloudmap.git", ID),
            ("https://user:pass@unfurl.cloud/onecommons/cloudmap.git", ID),
            (
                "https://unfurl.cloud/onecommons/cloudmap.git#main:sub/dir",
                ID,
            ),
            // DNS is case-insensitive, so the host folds...
            ("https://UNFURL.cloud/onecommons/cloudmap.git", ID),
            ("HTTPS://UNFURL.CLOUD/onecommons/cloudmap.git", ID),
            // ...the path does not: two repositories merged under one
            // identity is worse than one that fails to merge.
            (
                "https://unfurl.cloud/OneCommons/CloudMap.git",
                "unfurl.cloud/OneCommons/CloudMap",
            ),
            // a non-default port distinguishes hosts, so it is kept
            (
                "https://unfurl.cloud:8443/onecommons/cloudmap.git",
                "unfurl.cloud:8443/onecommons/cloudmap",
            ),
            ("https://host/p.git?x=1", "host/p?x=1"),
            ("host:project.git", "host:project"),
            ("ssh://git@host:2222/p.git", "host:2222/p"),
            ("https://host/", "host"),
            ("https://host", "host"),
            ("git@host:~user/p.git", "host/~user/p"),
            ("git@Host:a/b/c.git", "host/a/b/c"),
            ("git-local://0123abcd:project/p", "0123abcd"),
            ("./relative/path", "./relative/path"),
            ("/tmp/local/repo", "/tmp/local/repo"),
            ("file:///tmp/local/repo", "/tmp/local/repo"),
            ("", ""),
        ] {
            assert_eq!(n(url), expected, "normalizing {url:?}");
        }
    }

    #[test]
    fn is_idempotent() {
        // A value normalized twice has to match one normalized once, or
        // an origin read back out of the database would stop matching.
        for url in [
            "https://unfurl.cloud/onecommons/cloudmap.git",
            "git@unfurl.cloud:onecommons/cloudmap.git",
            "https://unfurl.cloud:8443/onecommons/cloudmap.git",
            "/tmp/local/repo",
            "",
        ] {
            let once = n(url);
            assert_eq!(n(&once), once, "not idempotent for {url:?}");
        }
    }

    #[test]
    fn absolute_paths_are_resolved_textually() {
        assert_eq!(n("/tmp/a/../b"), "/tmp/b");
        assert_eq!(n("/tmp//a/./b/"), "/tmp/a/b");
    }
}
