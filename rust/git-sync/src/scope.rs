// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! The files a handle opened with pathspecs takes in and writes.

use std::collections::BTreeSet;
use std::fmt::Display;
use std::path::Path;
use std::sync::Mutex;

use gix::pathspec::{Defaults, Search};

use crate::error::{Error, Result};

/// The files git pathspecs match, as [`crate::SyncedRepo::open_with`] takes
/// them.
pub(crate) struct Scope {
    /// Matching takes `&mut` for its caches.
    search: Mutex<Search>,
    /// Files on disk that the index doesn't have are taken in too.
    pub(crate) untracked: bool,
    /// The paths pathspecs name without a wildcard, which `.gitignore`
    /// doesn't keep out.
    literal: Vec<String>,
}

impl Scope {
    /// `pathspecs` relative to the working tree at `root`, parsed with git's
    /// defaults rather than the environment's, so the same strings always
    /// match the same files; `None` for none, which is every file.
    pub(crate) fn new(pathspecs: &[&str], root: &Path, untracked: bool) -> Result<Option<Self>> {
        if pathspecs.is_empty() {
            if untracked {
                return Err(invalid(
                    "",
                    "untracked files are taken in only with pathspecs",
                ));
            }
            return Ok(None);
        }
        let patterns = pathspecs
            .iter()
            .map(|spec| {
                let pattern = gix::pathspec::parse(spec.as_bytes(), Defaults::default())
                    .map_err(|e| invalid(spec, e))?;
                // matching one needs `.gitattributes`, which isn't read
                if !pattern.attributes.is_empty() {
                    return Err(invalid(spec, "attribute pathspecs aren't supported"));
                }
                Ok(pattern)
            })
            .collect::<Result<Vec<_>>>()?;
        let literal = patterns.iter().filter_map(literal_path).collect();
        let search = Search::from_specs(patterns, None, root)
            .map_err(|e| invalid(&pathspecs.join(" "), e))?;
        Ok(Some(Scope {
            search: Mutex::new(search),
            untracked,
            literal,
        }))
    }

    fn search(&self) -> std::sync::MutexGuard<'_, Search> {
        self.search.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// Whether a pathspec names `path` without a wildcard.
    pub(crate) fn names(&self, path: &str) -> bool {
        self.literal.iter().any(|l| l == path)
    }

    /// Whether the pathspecs match the working-tree-relative file `path`.
    pub(crate) fn contains(&self, path: &str) -> bool {
        self.search()
            .pattern_matching_relative_path(
                gix::bstr::BStr::new(path),
                Some(false),
                &mut |_, _, _, _| false,
            )
            .is_some_and(|m| !m.is_excluded())
    }

    /// The files under `root` the pathspecs match, relative to it and
    /// sorted, outside `.git` and nested repositories, less those the
    /// `.gitignore` files from `root` down exclude, unless a pathspec names
    /// one literally. A symlink to a file is one; a symlinked directory
    /// isn't entered.
    pub(crate) fn files_on_disk(&self, root: &Path) -> Result<Vec<String>> {
        let mut found = BTreeSet::new();
        let mut dirs = vec![(String::new(), gix::ignore::Search::default())];
        while let Some((dir, ignore)) = dirs.pop() {
            self.visit(root, &dir, ignore, &mut dirs, &mut found)?;
        }
        for rel in &self.literal {
            if listed(&root.join(rel)) && self.contains(rel) {
                found.insert(rel.clone());
            }
        }
        Ok(found.into_iter().collect())
    }

    /// Directory `dir` of [`Self::files_on_disk`]'s walk, with the ignore
    /// patterns of the directories above it: its files onto `found`, its
    /// subdirectories onto `dirs`.
    fn visit(
        &self,
        root: &Path,
        dir: &str,
        mut ignore: gix::ignore::Search,
        dirs: &mut Vec<(String, gix::ignore::Search)>,
        found: &mut BTreeSet<String>,
    ) -> Result<()> {
        let abs = root.join(dir);
        let entries = match std::fs::read_dir(&abs) {
            Ok(entries) => entries,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(()),
            Err(e) => return Err(Error::Io(e)),
        };
        add_gitignore(&mut ignore, root, &abs)?;
        for entry in entries {
            let entry = entry?;
            let Some(name) = entry.file_name().to_str().map(str::to_string) else {
                continue;
            };
            let rel = match dir.is_empty() {
                true => name.clone(),
                false => format!("{dir}/{name}"),
            };
            let is_dir = entry.file_type()?.is_dir();
            if name == ".git" || ignored(&ignore, &rel, is_dir) {
                continue;
            }
            if is_dir && self.may_hold(root, &rel) {
                dirs.push((rel, ignore.clone()));
            } else if !is_dir && listed(&entry.path()) && self.contains(&rel) {
                found.insert(rel);
            }
        }
        Ok(())
    }

    /// Whether directory `rel` under `root` can hold a file the pathspecs
    /// match and isn't another repository's working tree.
    fn may_hold(&self, root: &Path, rel: &str) -> bool {
        self.search()
            .can_match_relative_path(gix::bstr::BStr::new(rel), Some(true))
            && !root.join(rel).join(".git").exists()
    }
}

/// The path `pattern` names without a wildcard, when it includes it.
fn literal_path(pattern: &gix::pathspec::Pattern) -> Option<String> {
    use gix::pathspec::{MagicSignature, SearchMode};
    let path = std::str::from_utf8(pattern.path()).ok()?;
    let glob = path.contains(['*', '?', '[', '\\']) && pattern.search_mode != SearchMode::Literal;
    let excluded = pattern.signature.contains(MagicSignature::EXCLUDE);
    (!path.is_empty() && !glob && !excluded).then(|| path.to_string())
}

/// A regular file, through any symlinks, or a symlink that resolves to
/// nothing: the scan resolves or refuses the links. A link to a directory
/// isn't one.
fn listed(path: &Path) -> bool {
    match std::fs::metadata(path) {
        Ok(m) => m.is_file(),
        Err(_) => std::fs::symlink_metadata(path).is_ok_and(|m| m.file_type().is_symlink()),
    }
}

/// Whether the file or a directory on the way to it from `root` is a
/// symlink: `rel`'s components are checked up to the first that doesn't
/// exist.
pub(crate) fn through_link(root: &Path, rel: &str) -> bool {
    let mut path = root.to_path_buf();
    for part in Path::new(rel).components() {
        path.push(part);
        match std::fs::symlink_metadata(&path) {
            Ok(m) if m.file_type().is_symlink() => return true,
            Ok(_) => {}
            Err(_) => return false,
        }
    }
    false
}

/// `ignore` with the patterns of `dir`'s `.gitignore`, if it has one;
/// `dir` is under `root`, which they're relative to.
fn add_gitignore(ignore: &mut gix::ignore::Search, root: &Path, dir: &Path) -> Result<()> {
    let source = dir.join(".gitignore");
    let bytes = match std::fs::read(&source) {
        Ok(bytes) => bytes,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(e) => return Err(Error::Io(e)),
    };
    ignore.add_patterns_buffer(&bytes, source, Some(root), Default::default())?;
    Ok(())
}

/// Whether `ignore` excludes `rel`, a directory where `is_dir`.
fn ignored(ignore: &gix::ignore::Search, rel: &str, is_dir: bool) -> bool {
    ignore
        .pattern_matching_relative_path(
            gix::bstr::BStr::new(rel),
            Some(is_dir),
            gix::glob::pattern::Case::Sensitive,
        )
        .is_some_and(|m| !m.pattern.is_negative())
}

fn invalid(pathspec: &str, message: impl Display) -> Error {
    Error::InvalidPathspec {
        pathspec: pathspec.to_string(),
        message: message.to_string(),
    }
}
