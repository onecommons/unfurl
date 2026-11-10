// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! The files a handle opened with pathspecs takes in and writes.

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
}

impl Scope {
    /// `pathspecs` relative to the working tree at `root`, parsed with git's
    /// defaults rather than the environment's, so the same strings always
    /// match the same files; `None` for none, which is every file.
    pub(crate) fn new(pathspecs: &[&str], root: &Path) -> Result<Option<Self>> {
        if pathspecs.is_empty() {
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
        let search = Search::from_specs(patterns, None, root)
            .map_err(|e| invalid(&pathspecs.join(" "), e))?;
        Ok(Some(Scope {
            search: Mutex::new(search),
        }))
    }

    /// Whether the pathspecs match the working-tree-relative file `path`.
    pub(crate) fn contains(&self, path: &str) -> bool {
        let mut search = self.search.lock().unwrap_or_else(|e| e.into_inner());
        search
            .pattern_matching_relative_path(
                gix::bstr::BStr::new(path),
                Some(false),
                &mut |_, _, _, _| false,
            )
            .is_some_and(|m| !m.is_excluded())
    }
}

fn invalid(pathspec: &str, message: impl Display) -> Error {
    Error::InvalidPathspec {
        pathspec: pathspec.to_string(),
        message: message.to_string(),
    }
}
