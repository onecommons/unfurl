// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Thin gix helpers used by the rest of the crate.
//!
//! Wraps the parts of [`gix`] we need: open a working tree, list its
//! tracked files, derive `(origin, branch, head_oid)`, walk
//! `git log -- <path>` lazily ([`last_commits_for_paths`]), and create
//! a commit by overlaying blobs on top of the current HEAD tree.
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
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

/// [`WorktreeMeta::branch`] of a detached HEAD.
pub const DETACHED: &str = "HEAD";

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
        None => DETACHED.to_string(),
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

/// The blob OID git would record for `bytes`.
///
/// Takes the bytes rather than a path so the caller hashes exactly what
/// it read: hashing a second read of the same file would let the content
/// change in between, yielding an OID that describes bytes nobody
/// parsed — precisely wrong for an OID recording *which* content a
/// database's rows came from.
///
/// A pure hash, unlike `Repository::write_blob`, which stores a loose
/// object as a side effect. Answering "has this file changed?" should
/// not leave unreferenced objects behind.
pub fn blob_oid_for_bytes(repo: &gix::Repository, bytes: &[u8]) -> gix::ObjectId {
    gix::objs::compute_hash(repo.object_hash(), gix::object::Kind::Blob, bytes)
}

/// Stage `paths` (relative to the work dir) and create a commit on HEAD
/// with the given message. Returns the new commit OID.
pub fn commit_paths(
    repo: &gix::Repository,
    paths: &[String],
    message: &str,
) -> Result<gix::ObjectId> {
    let head = repo.head_id().ok().map(|id| id.detach());
    commit_paths_onto(repo, paths, message, head)
}

/// [`commit_paths`] onto `parent` (`None` for an unborn branch), failing
/// with [`Error::HeadMoved`] unless `HEAD` is still there when the commit
/// is made.
pub fn commit_paths_onto(
    repo: &gix::Repository,
    paths: &[String],
    message: &str,
    parent: Option<gix::ObjectId>,
) -> Result<gix::ObjectId> {
    head_is_at(repo, parent)?;
    let work_dir = repo
        .work_dir()
        .ok_or_else(|| Error::Git("repository has no working tree".to_string()))?
        .to_path_buf();

    // For each path: read disk bytes, write a blob, capture (segments,
    // oid) -- or `None` for a path that is gone, which the commit
    // removes.
    let mut updates: Vec<TreeUpdate> = Vec::new();
    for rel in paths {
        let abs = work_dir.join(rel);
        let blob_oid = match std::fs::read(&abs) {
            Ok(bytes) => Some(repo.write_blob(&bytes).map_err(git_err)?.detach()),
            // Only an absent file means "remove this". Any other read
            // failure is a real problem, and letting it read as a
            // deletion would quietly drop the path from history.
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => None,
            Err(e) => return Err(Error::Io(e)),
        };
        let segments: Vec<String> = rel.split('/').map(|s| s.to_string()).collect();
        if segments.iter().any(|s| s.is_empty()) {
            return Err(Error::Other(format!("invalid path for commit: {rel}")));
        }
        updates.push((segments, blob_oid));
    }

    let parents: Vec<gix::ObjectId> = parent.into_iter().collect();
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

    // gix updates HEAD only if it still names `parent`
    let id = match repo.commit("HEAD", message, new_tree_oid, parents) {
        Ok(id) => id,
        Err(err) => {
            head_is_at(repo, parent)?;
            return Err(git_err(err));
        }
    };

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

/// [`Error::HeadMoved`] unless `HEAD` names `expected`.
fn head_is_at(repo: &gix::Repository, expected: Option<gix::ObjectId>) -> Result<()> {
    let found = repo.head_id().ok().map(|id| id.detach());
    if found == expected {
        return Ok(());
    }
    Err(Error::HeadMoved {
        expected: expected.map(|id| id.to_string()),
        found: found.map(|id| id.to_string()),
    })
}

/// One overlay onto a tree: the path split into segments, and the blob
/// to put there -- or `None` to remove it.
type TreeUpdate = (Vec<String>, Option<gix::ObjectId>);

/// Build a new tree from `base_tree_oid` (or empty) with each entry in
/// `updates` overlaid: `Some(oid)` inserts or replaces the blob,
/// `None` removes the path.
///
/// A directory that loses its last entry is dropped rather than written
/// as an empty tree, which is what git does -- it tracks files, so a
/// directory exists only while something is in it.
fn build_tree_with_updates(
    repo: &gix::Repository,
    base_tree_oid: Option<gix::ObjectId>,
    updates: &[TreeUpdate],
) -> Result<gix::ObjectId> {
    use gix::objs::tree::{Entry, EntryKind, EntryMode};

    // Group updates by the first path component.
    let mut here_updates: BTreeMap<String, Option<gix::ObjectId>> = BTreeMap::new();
    let mut sub_updates: BTreeMap<String, Vec<TreeUpdate>> = BTreeMap::new();
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

    // Apply blob replacements and removals at this level.
    for (name, oid) in here_updates {
        match oid {
            Some(oid) => {
                entries.insert(
                    name.clone(),
                    Entry {
                        mode: EntryMode::from(EntryKind::Blob),
                        filename: name.into(),
                        oid,
                    },
                );
            }
            None => {
                entries.remove(&name);
            }
        }
    }

    // Recurse into subdirectories.
    for (subdir, sub_ups) in sub_updates {
        let existing_subtree = entries
            .get(&subdir)
            .filter(|e| e.mode.is_tree())
            .map(|e| e.oid);
        let new_subtree_oid = build_tree_with_updates(repo, existing_subtree, &sub_ups)?;
        if repo
            .find_tree(new_subtree_oid)
            .map_err(git_err)?
            .decode()
            .map_err(git_err)?
            .entries
            .is_empty()
        {
            entries.remove(&subdir);
            continue;
        }
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
/// `paths`, walking ancestors of `head` in reverse-chronological order.
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
    head: Option<gix::ObjectId>,
    paths: &[String],
) -> Result<HashMap<String, String>> {
    use gix::object::tree::diff::{Action, Change};
    use gix::prelude::ObjectIdExt;

    // Unborn / empty repo: nothing to attribute.
    let Some(head) = head.filter(|_| !paths.is_empty()) else {
        return Ok(HashMap::new());
    };
    let head = head.attach(repo);

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

/// Every blob in `commit`'s tree: path → OID. Empty when `commit` isn't a
/// resolvable commit.
pub fn tree_blobs(repo: &gix::Repository, commit: &str) -> Result<HashMap<String, gix::ObjectId>> {
    use gix::object::tree::diff::{change::Event, Action, Change};

    let Ok(oid) = gix::ObjectId::from_hex(commit.as_bytes()) else {
        return Ok(HashMap::new());
    };
    let Ok(commit) = repo.find_commit(oid) else {
        return Ok(HashMap::new());
    };
    let tree = commit.tree().map_err(git_err)?;
    let mut out = HashMap::new();
    let mut visit =
        |change: Change<'_, '_, '_>| -> std::result::Result<Action, std::convert::Infallible> {
            if let (Event::Addition { entry_mode, id }, Ok(path)) =
                (&change.event, change.location.to_str())
            {
                if entry_mode.is_blob() {
                    out.insert(path.to_string(), id.detach());
                }
            }
            Ok(Action::Continue)
        };
    let empty = repo.empty_tree();
    let mut platform = empty.changes().map_err(git_err)?;
    platform.track_path();
    platform.track_rewrites(None);
    platform
        .for_each_to_obtain_tree(&tree, &mut visit)
        .map_err(git_err)?;
    Ok(out)
}

/// The bytes of blob `oid`.
pub fn read_blob(repo: &gix::Repository, oid: &str) -> Result<Vec<u8>> {
    let oid = gix::ObjectId::from_hex(oid.as_bytes()).map_err(git_err)?;
    Ok(repo.find_object(oid).map_err(git_err)?.data.clone())
}

/// Bytes of `rel_path`'s blob in `commit`.
///
/// `Ok(None)` when `commit` isn't a resolvable commit oid or its tree
/// doesn't carry the path — the sync path reads a pending edit's base
/// content through this, and an unreadable base is handled there (as
/// "assume diverged"), not here.
pub fn read_blob_at_commit(
    repo: &gix::Repository,
    commit: &str,
    rel_path: &str,
) -> Result<Option<Vec<u8>>> {
    let Ok(oid) = gix::ObjectId::from_hex(commit.as_bytes()) else {
        return Ok(None);
    };
    let Ok(commit) = repo.find_commit(oid) else {
        return Ok(None);
    };
    let mut tree = commit.tree().map_err(git_err)?;
    let Some(entry) = tree.peel_to_entry_by_path(rel_path).map_err(git_err)? else {
        return Ok(None);
    };
    let object = entry.object().map_err(git_err)?;
    Ok(Some(object.data.clone()))
}

/// The raw message of `commit`, or `None` when it isn't a resolvable
/// commit oid. Read by the scan to look for the trailer a file-side
/// author uses to resolve conflicts (see
/// [`crate::SyncedRepo::update_from_working_dir_with`]).
pub fn commit_message(repo: &gix::Repository, commit: &str) -> Option<String> {
    let oid = gix::ObjectId::from_hex(commit.as_bytes()).ok()?;
    let commit = repo.find_commit(oid).ok()?;
    Some(commit.message_raw().ok()?.to_string())
}

/// `commit`, when this repository has it as a commit.
fn present(repo: &gix::Repository, commit: &str) -> Result<Option<gix::ObjectId>> {
    let Ok(oid) = gix::ObjectId::from_hex(commit.as_bytes()) else {
        return Ok(None);
    };
    Ok(repo
        .try_find_object(oid)
        .map_err(git_err)?
        .filter(|o| o.kind == gix::object::Kind::Commit)
        .map(|_| oid))
}

fn parents(repo: &gix::Repository, oid: gix::ObjectId) -> Result<Vec<gix::ObjectId>> {
    Ok(repo
        .find_commit(oid)
        .map_err(git_err)?
        .parent_ids()
        .map(|p| p.detach())
        .collect())
}

/// `commit`'s parents, first parent first.
pub fn commit_parents(repo: &gix::Repository, commit: &str) -> Result<Vec<String>> {
    let oid = gix::ObjectId::from_hex(commit.as_bytes()).map_err(git_err)?;
    Ok(parents(repo, oid)?
        .into_iter()
        .map(|p| p.to_string())
        .collect())
}

/// Every commit reachable from `oid`, itself included.
fn reachable(repo: &gix::Repository, oid: gix::ObjectId) -> Result<HashSet<gix::ObjectId>> {
    let mut seen = HashSet::from([oid]);
    let mut todo = vec![oid];
    while let Some(c) = todo.pop() {
        for p in parents(repo, c)? {
            if seen.insert(p) {
                todo.push(p);
            }
        }
    }
    Ok(seen)
}

/// Whether `ancestor` is `commit` or one of its ancestors. A commit this
/// repository doesn't have is neither.
pub fn is_ancestor(repo: &gix::Repository, ancestor: &str, commit: &str) -> Result<bool> {
    let (Some(a), Some(c)) = (present(repo, ancestor)?, present(repo, commit)?) else {
        return Ok(false);
    };
    let mut seen = HashSet::from([c]);
    let mut todo = vec![c];
    while let Some(at) = todo.pop() {
        if at == a {
            return Ok(true);
        }
        for p in parents(repo, at)? {
            if seen.insert(p) {
                todo.push(p);
            }
        }
    }
    Ok(false)
}

/// Whether `commit` is on `head`'s first-parent history, `head` included.
pub fn first_parent_contains(repo: &gix::Repository, head: &str, commit: &str) -> Result<bool> {
    let (Some(mut at), Some(c)) = (present(repo, head)?, present(repo, commit)?) else {
        return Ok(false);
    };
    loop {
        if at == c {
            return Ok(true);
        }
        match parents(repo, at)?.first() {
            Some(&p) => at = p,
            None => return Ok(false),
        }
    }
}

/// The best common ancestor of `a` and `b`: one no other common ancestor
/// descends from, the newest where there are several (a criss-cross
/// merge). `None` when they share no history, or this repository lacks
/// either.
pub fn merge_base(repo: &gix::Repository, a: &str, b: &str) -> Result<Option<String>> {
    let (Some(a), Some(b)) = (present(repo, a)?, present(repo, b)?) else {
        return Ok(None);
    };
    let of_a = reachable(repo, a)?;
    // the common ancestors nearest b: stop at each, since what's below
    // one isn't best
    let mut found = Vec::new();
    let mut seen = HashSet::from([b]);
    let mut todo = std::collections::VecDeque::from([b]);
    while let Some(c) = todo.pop_front() {
        if of_a.contains(&c) {
            found.push(c);
            continue;
        }
        for p in parents(repo, c)? {
            if seen.insert(p) {
                todo.push_back(p);
            }
        }
    }
    let mut best = Vec::new();
    for &c in &found {
        let mut below_another = false;
        for &other in found.iter().filter(|&&o| o != c) {
            if reachable(repo, other)?.contains(&c) {
                below_another = true;
                break;
            }
        }
        if !below_another {
            let time = repo
                .find_commit(c)
                .map_err(git_err)?
                .time()
                .map_err(git_err)?;
            best.push((time.seconds, c));
        }
    }
    Ok(best.into_iter().max().map(|(_, c)| c.to_string()))
}

/// The paths of files that differ between commits `from` and `to`.
pub fn changed_paths(repo: &gix::Repository, from: &str, to: &str) -> Result<Vec<String>> {
    use gix::object::tree::diff::{change::Event, Action, Change};
    let tree = |commit: &str| -> Result<gix::Tree<'_>> {
        let oid = gix::ObjectId::from_hex(commit.as_bytes()).map_err(git_err)?;
        repo.find_commit(oid)
            .map_err(git_err)?
            .tree()
            .map_err(git_err)
    };
    let (old, new) = (tree(from)?, tree(to)?);
    let mut paths = BTreeSet::new();
    let mut visit =
        |change: Change<'_, '_, '_>| -> std::result::Result<Action, std::convert::Infallible> {
            let file = match change.event {
                Event::Addition { entry_mode, .. } | Event::Deletion { entry_mode, .. } => {
                    !entry_mode.is_tree()
                }
                Event::Modification {
                    previous_entry_mode,
                    entry_mode,
                    ..
                } => !entry_mode.is_tree() || !previous_entry_mode.is_tree(),
                Event::Rewrite { .. } => true,
            };
            if file {
                paths.insert(change.location.to_string());
            }
            Ok(Action::Continue)
        };
    let mut platform = old.changes().map_err(git_err)?;
    platform.track_path();
    platform.track_rewrites(None);
    platform
        .for_each_to_obtain_tree(&new, &mut visit)
        .map_err(git_err)?;
    Ok(paths.into_iter().collect())
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

#[cfg(test)]
mod commit_tests {
    use super::*;

    /// A staged path whose file is gone is a removal -- and a directory
    /// that loses its last file goes with it, since git tracks files and
    /// a tree entry pointing at an empty tree is not what it writes.
    #[test]
    fn commit_paths_stages_a_removal() {
        let tmp = tempfile::tempdir().expect("tempdir");
        init_with_files(
            tmp.path(),
            &[
                ("keep.yaml".to_string(), b"a: 1\n".to_vec()),
                ("nested/gone.yaml".to_string(), b"b: 2\n".to_vec()),
            ],
            "initial",
        )
        .expect("init");
        std::fs::remove_file(tmp.path().join("nested/gone.yaml")).expect("remove");

        let repo = open_repo(tmp.path()).expect("open");
        let oid =
            commit_paths(&repo, &["nested/gone.yaml".to_string()], "drop it").expect("commit");
        let oid_str = oid.to_string();

        assert!(
            read_blob_at_commit(&repo, &oid_str, "nested/gone.yaml")
                .expect("read")
                .is_none(),
            "the commit no longer carries the path"
        );
        assert!(
            read_blob_at_commit(&repo, &oid_str, "keep.yaml")
                .expect("read")
                .is_some(),
            "its neighbour is untouched"
        );
        let commit = repo.find_commit(oid).expect("commit");
        let mut tree = commit.tree().expect("tree");
        assert!(
            tree.peel_to_entry_by_path("nested")
                .expect("peel")
                .is_none(),
            "the emptied directory went too"
        );
    }
}

#[cfg(test)]
mod history_tests {
    use super::*;

    fn run(dir: &Path, args: &[&str]) -> String {
        let out = std::process::Command::new("git")
            .args([
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@t",
                "-c",
                "commit.gpgsign=false",
            ])
            .args(args)
            .current_dir(dir)
            .output()
            .expect("git");
        assert!(out.status.success(), "git {args:?}: {out:?}");
        String::from_utf8(out.stdout)
            .expect("utf8")
            .trim()
            .to_string()
    }

    fn commit(dir: &Path, file: &str, text: &str) -> String {
        if let Some(parent) = Path::new(file).parent() {
            std::fs::create_dir_all(dir.join(parent)).expect("mkdir");
        }
        std::fs::write(dir.join(file), text).expect("write");
        run(dir, &["add", "-A"]);
        run(dir, &["commit", "-q", "-m", text]);
        run(dir, &["rev-parse", "HEAD"])
    }

    /// main: root - m1 - merge(m1, f1); feature: root - f1; other: an
    /// orphan with no shared history.
    struct History {
        _tmp: tempfile::TempDir,
        repo: gix::Repository,
        root: String,
        m1: String,
        f1: String,
        merge: String,
        orphan: String,
    }

    fn history() -> History {
        let tmp = tempfile::tempdir().expect("tempdir");
        let d = tmp.path();
        run(d, &["init", "-q", "-b", "main"]);
        let root = commit(d, "a.yaml", "root");
        run(d, &["checkout", "-q", "-b", "feature"]);
        let f1 = commit(d, "b.yaml", "f1");
        run(d, &["checkout", "-q", "main"]);
        let m1 = commit(d, "a.yaml", "m1");
        run(d, &["merge", "-q", "--no-ff", "-m", "merge", "feature"]);
        let merge = run(d, &["rev-parse", "HEAD"]);
        run(d, &["checkout", "-q", "--orphan", "other"]);
        let orphan = commit(d, "c.yaml", "orphan");
        let repo = open_repo(d).expect("open");
        History {
            _tmp: tmp,
            repo,
            root,
            m1,
            f1,
            merge,
            orphan,
        }
    }

    const MISSING: &str = "0123456789abcdef0123456789abcdef01234567";

    #[test]
    fn ancestry() {
        let h = history();
        assert!(is_ancestor(&h.repo, &h.root, &h.merge).unwrap());
        assert!(is_ancestor(&h.repo, &h.f1, &h.merge).unwrap());
        assert!(is_ancestor(&h.repo, &h.merge, &h.merge).unwrap());
        assert!(!is_ancestor(&h.repo, &h.f1, &h.m1).unwrap());
        assert!(!is_ancestor(&h.repo, &h.root, &h.orphan).unwrap());
        assert!(!is_ancestor(&h.repo, MISSING, &h.merge).unwrap());
        assert!(!is_ancestor(&h.repo, &h.root, MISSING).unwrap());
    }

    #[test]
    fn first_parents() {
        let h = history();
        assert!(first_parent_contains(&h.repo, &h.merge, &h.m1).unwrap());
        assert!(first_parent_contains(&h.repo, &h.merge, &h.root).unwrap());
        assert!(!first_parent_contains(&h.repo, &h.merge, &h.f1).unwrap());
        assert!(!first_parent_contains(&h.repo, &h.merge, MISSING).unwrap());
    }

    #[test]
    fn merge_bases() {
        let h = history();
        assert_eq!(
            merge_base(&h.repo, &h.m1, &h.f1).unwrap(),
            Some(h.root.clone())
        );
        assert_eq!(
            merge_base(&h.repo, &h.merge, &h.f1).unwrap(),
            Some(h.f1.clone())
        );
        assert_eq!(
            merge_base(&h.repo, &h.f1, &h.merge).unwrap(),
            Some(h.f1.clone())
        );
        assert_eq!(merge_base(&h.repo, &h.merge, &h.orphan).unwrap(), None);
        assert_eq!(merge_base(&h.repo, &h.merge, MISSING).unwrap(), None);
    }

    #[test]
    fn changed_files() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let d = tmp.path();
        run(d, &["init", "-q", "-b", "main"]);
        commit(d, "keep.yaml", "same");
        commit(d, "dir/edit.yaml", "before");
        let from = commit(d, "dir/gone.yaml", "gone");
        std::fs::remove_file(d.join("dir/gone.yaml")).expect("rm");
        commit(d, "dir/edit.yaml", "after");
        let to = commit(d, "new/added.yaml", "added");
        let repo = open_repo(d).expect("open");
        assert_eq!(
            changed_paths(&repo, &from, &to).unwrap(),
            vec!["dir/edit.yaml", "dir/gone.yaml", "new/added.yaml"]
        );
        assert!(changed_paths(&repo, &to, &to).unwrap().is_empty());
    }
}
