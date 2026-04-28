// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! gix helpers: open repos, list tracked files, derive `(origin, branch)`,
//! create commits.
//!
//! For commit creation we deliberately avoid mutating the gix index
//! (the API surface for that is verbose in 0.66). Instead we walk the
//! current `HEAD` tree, replace blobs for the modified paths, and write
//! a new tree directly via gix's object database.

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

/// Resolve `(origin, branch, head_oid)` for a freshly-opened repo. Falls
/// back to the working-dir path when no `origin` remote is configured.
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
