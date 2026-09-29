// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! A new worktree placed in its family: forks and splits
//! (docs/branch-segments.md §4.5–§4.7, C.13, C.16, C.17).

use std::collections::{BTreeMap, BTreeSet};

use crate::db::store::{At, Filter, NewRow, Row, Scope, Store};
use crate::db::{self, on_pool, Db};
use crate::error::Result;
use crate::format::FormatRegistry;
use crate::git;
use crate::ids::History;

/// A place in a file, owned.
type Place = (String, String, String);

fn place_of(r: &Row) -> Place {
    (r.file_path.clone(), r.path.clone(), r.key.clone())
}

fn at(p: &Place) -> At<'_> {
    At {
        file_path: &p.0,
        path: &p.1,
        key: &p.2,
    }
}

/// Where a new worktree's HEAD falls in its family (§4.6).
enum Placement {
    /// At a segment whose state is HEAD's.
    At(i64),
    /// Inside segment `s`, whose head commit is `h`, at commit `c`.
    Inside { s: i64, h: String, c: String },
}

/// Find or create the worktree `(origin, branch)`, whose HEAD is `head`:
/// a new one joins the family of another branch of `origin` and forks
/// where HEAD falls in it (§4.5), or else starts a family of its own.
pub(crate) async fn open(
    db: &Db,
    repo: &gix::Repository,
    formats: &FormatRegistry,
    origin: &str,
    branch: &str,
    head: Option<&str>,
) -> Result<i64> {
    if let Some(id) = db::worktree::find(db, origin, branch).await? {
        return Ok(id);
    }
    let database = db::commit::database_id(db).await?;
    on_pool!(db, pool => {
        let mut tx = pool.begin().await?;
        let family = Store::family_of_origin(&mut tx, origin).await?;
        tx.commit().await?;
        let id = match (family, head) {
            (Some(family), Some(head)) => {
                let history = History::new(formats, repo, database);
                fork_into(pool, history, family, origin, branch, head).await?
            }
            _ => None,
        };
        match id {
            Some(id) => Ok(id),
            None => {
                let mut tx = pool.begin().await?;
                let id = Store::create_worktree(&mut tx, origin, branch).await?;
                tx.commit().await?;
                Ok(id)
            }
        }
    })
}

/// Fork `(origin, branch)` at `head` in `family`, splitting the segment
/// it falls inside; `None` where it falls nowhere in the family.
async fn fork_into<DB: Store>(
    pool: &sqlx::Pool<DB>,
    mut history: History<'_>,
    family: i64,
    origin: &str,
    branch: &str,
    head: &str,
) -> Result<Option<i64>> {
    let mut tx = pool.begin().await?;
    // before any read: SQLite can't take the write lock after one
    DB::lock_family(&mut tx, family).await?;
    let base = match place(&mut tx, history.repo(), family, head).await? {
        None => return Ok(None),
        Some(Placement::At(seg)) => seg,
        Some(Placement::Inside { s, h, c }) => {
            split(&mut tx, &mut history, family, s, &c, &h).await?;
            s
        }
    };
    let id = DB::fork_at(&mut tx, family, base, origin, branch, head).await?;
    tx.commit().await?;
    Ok(Some(id))
}

/// §4.6: where `head` falls in the family, else where its latest
/// merge-base with the tracked heads does; the first scan brings a head
/// placed there up to `head`.
async fn place<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    repo: &gix::Repository,
    family: i64,
    head: &str,
) -> Result<Option<Placement>> {
    if let Some(placed) = place_commit(tx, repo, family, head).await? {
        return Ok(Some(placed));
    }
    // Any common ancestor is a correct base: the first scan writes
    // whatever differs from it. The best is the latest, since more of
    // `head`'s history is then already the family's rows: the fork shares
    // more of them, and its first scan writes less. So keep the merge-base
    // with each tracked head that descends from the others found: the
    // latest, on one line of history; across lines a merge joined, the
    // first found, as correct if perhaps less shared.
    let mut best: Option<String> = None;
    for tracked in DB::family_heads(tx, family).await? {
        let Some(h) = tracked.head_commit else {
            continue;
        };
        let Some(base) = git::merge_base(repo, head, &h)? else {
            continue;
        };
        let descends = match &best {
            Some(b) => git::is_ancestor(repo, b, &base)?,
            None => true,
        };
        if descends {
            best = Some(base);
        }
    }
    match best {
        Some(base) => place_commit(tx, repo, family, &base).await,
        None => Ok(None),
    }
}

/// C.16: a segment ending at `c`, else the one `c` falls inside, down the
/// chain of a tracked head `c` is an ancestor of; one whose first-parent
/// history holds `c` first.
async fn place_commit<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    repo: &gix::Repository,
    family: i64,
    c: &str,
) -> Result<Option<Placement>> {
    if let Some(seg) = DB::segment_ending_at(tx, family, c).await? {
        return Ok(Some(Placement::At(seg)));
    }
    let mut top: Option<(i64, bool)> = None;
    for tracked in DB::family_heads(tx, family).await? {
        let Some(h) = tracked.head_commit else {
            continue;
        };
        if !git::is_ancestor(repo, c, &h)? {
            continue;
        }
        let first = git::first_parent_contains(repo, &h, c)?;
        if top.is_none_or(|(_, f)| first && !f) {
            top = Some((tracked.id, first));
        }
    }
    let Some((top, _)) = top else {
        return Ok(None);
    };
    let chain = DB::segment_chain(tx, top).await?;
    for (i, seg) in chain.iter().enumerate() {
        let Some(h) = &seg.head_commit else {
            continue;
        };
        if !git::is_ancestor(repo, c, h)? {
            continue;
        }
        let below = chain.get(i + 1).and_then(|b| b.head_commit.as_deref());
        let in_below = match below {
            Some(b) => git::is_ancestor(repo, c, b)?,
            None => false,
        };
        if !in_below {
            return Ok(Some(Placement::Inside {
                s: seg.id,
                h: h.clone(),
                c: c.to_string(),
            }));
        }
    }
    Ok(None)
}

/// What a split knows of one place (§4.7): its value at `c`, the rows
/// below `s` there, and `s`'s own row, if any.
struct Key {
    v_c: Option<serde_json::Value>,
    below: Vec<Row>,
    own: Option<Row>,
    collides: bool,
}

impl Key {
    fn below_live(&self) -> Option<&Row> {
        self.below.iter().find(|r| !r.deleted)
    }

    /// Whether `s` needs a row of its own at `c`.
    fn recreated(&self) -> bool {
        self.own.is_none()
            || self.v_c.as_ref() != self.below_live().map(|b| &b.json)
            || self.collides
    }
}

/// §4.7, C.17: segment `s`, whose head commit is `h`, ends at `c` instead,
/// and a new segment above it holds the rest. Every view is unchanged.
async fn split<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    history: &mut History<'_>,
    family: i64,
    s: i64,
    c: &str,
    h: &str,
) -> Result<()> {
    let repo = history.repo();
    let chain = DB::segment_chain(tx, s).await?;
    let parent = chain.get(1).map(|seg| seg.id);
    let below_commit = chain.get(1).and_then(|seg| seg.head_commit.clone());
    let own = DB::rows_in(tx, s, None, false).await?;
    // written after `c`: by a commit `c` is an ancestor of
    let mut late: Vec<Row> = Vec::new();
    for r in &own {
        if let Some(rc) = r.commit_id.as_deref() {
            if rc != c && git::is_ancestor(repo, c, rc)? {
                late.push(r.clone());
            }
        }
    }
    let s2 = DB::split_segment(tx, s, c).await?;

    // places that can differ: in files that differ between `c` and `h`,
    // and rows written after `c`, whatever their value, since a tombstone
    // kept for another draft's key changes nothing in git
    let mut places: BTreeSet<Place> = late.iter().map(place_of).collect();
    for file in git::changed_paths(repo, c, h)? {
        let (at_c, at_h) = (history.records(c, &file)?, history.records(h, &file)?);
        for (path, key) in at_c.keys().chain(at_h.keys()) {
            let k = (path.clone(), key.clone());
            if at_c.get(&k) != at_h.get(&k) {
                places.insert((file.clone(), k.0, k.1));
            }
        }
    }
    // the ids the tree at `c` keeps at the other places: taken
    let state = DB::visible(tx, s, Scope::State, Filter::All).await?;
    let kept: Vec<&Row> = state
        .iter()
        .filter(|r| !r.deleted && !places.contains(&place_of(r)))
        .collect();
    let mut taken: BTreeSet<i64> = kept.iter().map(|r| r.key_id).collect();
    let mut keys: BTreeMap<Place, Key> = BTreeMap::new();
    for p in &places {
        let below = match parent {
            Some(parent) => DB::visible(tx, parent, Scope::State, Filter::At(at(p))).await?,
            None => Vec::new(),
        };
        let mut key = Key {
            v_c: history.value(c, &p.0, &p.1, &p.2)?,
            below,
            own: own.iter().find(|r| place_of(r) == *p).cloned(),
            collides: false,
        };
        // a changed key whose value at `c` the row below already holds,
        // unless another key has that id: then the row below is another
        // copy of the record, and the key is re-created with its own
        if let (Some(v), Some(b)) = (&key.v_c, key.below_live()) {
            if b.json == *v && !taken.insert(b.key_id) {
                key.collides = true;
            }
        }
        keys.insert(p.clone(), key);
    }

    // §4.7: the re-created rows' ids, strongest evidence first across all
    // of them, each id once: rollup, move, continuing record, row below
    let mut recovered: BTreeMap<Place, i64> = BTreeMap::new();
    for tier in 1..=4 {
        for (p, key) in keys.iter().filter(|(_, k)| k.recreated()) {
            if recovered.contains_key(p) {
                continue;
            }
            let id = match tier {
                1 => history.id(c, &p.0, &p.1, &p.2)?,
                2 => late
                    .iter()
                    .find(|r| {
                        r.file_path != p.0
                            && (&r.path, &r.key) == (&p.1, &p.2)
                            && !r.deleted
                            && Some(&r.json) == key.v_c.as_ref()
                    })
                    .map(|r| r.key_id),
                3 => match &key.own {
                    Some(r) if history.continuous(c, h, &p.0, &p.1, &p.2)? => Some(r.key_id),
                    _ => None,
                },
                _ => match key.below.first() {
                    Some(b) if !b.deleted => {
                        let moved = kept.iter().any(|r| place_of(r) != *p && r.json == b.json)
                            || keys
                                .iter()
                                .any(|(q, k)| q != p && k.v_c.as_ref() == Some(&b.json));
                        let held = match &below_commit {
                            Some(bc) => history.continuous(bc, c, &p.0, &p.1, &p.2)?,
                            None => true,
                        };
                        (!moved && held).then_some(b.key_id)
                    }
                    _ => None,
                },
            };
            if let Some(id) = id.filter(|id| !taken.contains(id)) {
                taken.insert(id);
                recovered.insert(p.clone(), id);
            }
        }
    }

    for (p, key) in &keys {
        let a = at(p);
        match &key.own {
            Some(own) => {
                DB::move_row(tx, own.id, s2).await?;
                if key.recreated() {
                    let new = insert(
                        tx,
                        family,
                        s,
                        SplitRow::value(a, key.v_c.as_ref(), recovered.get(p).copied(), c),
                    )
                    .await?;
                    taken.insert(new.1);
                    DB::entry(tx, new.0, s2, own.key_id).await?;
                } else {
                    DB::move_entries(tx, s, s2, a).await?;
                }
            }
            None => {
                // changed before `c` and back after it
                let new = insert(
                    tx,
                    family,
                    s,
                    SplitRow::value(a, key.v_c.as_ref(), recovered.get(p).copied(), c),
                )
                .await?;
                taken.insert(new.1);
                for x in &key.below {
                    DB::entry(tx, x.id, s, new.1).await?;
                }
                // `s` may hide the row below by an entry alone (a fold took
                // the tombstone), so `s2` restores git's value at its end
                let v_h = history.value(h, &p.0, &p.1, &p.2)?;
                let copy = key
                    .below
                    .first()
                    .filter(|b| (!b.deleted).then_some(&b.json) == v_h.as_ref());
                let restored = match copy {
                    Some(b) => {
                        insert(
                            tx,
                            family,
                            s2,
                            SplitRow {
                                at: a,
                                json: &b.json,
                                deleted: b.deleted,
                                key_id: Some(b.key_id),
                                commit: h,
                            },
                        )
                        .await?
                    }
                    None => {
                        let id = history
                            .id(h, &p.0, &p.1, &p.2)?
                            .or(key.below.first().map(|b| b.key_id));
                        insert(tx, family, s2, SplitRow::value(a, v_h.as_ref(), id, h)).await?
                    }
                };
                // it stands for a version older than anything a segment
                // outside `s` and its ancestors holds there: each supersedes it
                let lower: BTreeSet<i64> = chain.iter().map(|seg| seg.id).collect();
                let mut newer: BTreeMap<i64, i64> = BTreeMap::new();
                for r in DB::rows_at(tx, family, a).await? {
                    if r.segment_id != s2 && !lower.contains(&r.segment_id) {
                        newer.entry(r.segment_id).or_insert(r.key_id);
                    }
                }
                for (seg, key_id) in newer {
                    DB::entry(tx, restored.0, seg, key_id).await?;
                }
                DB::entry(tx, new.0, s2, restored.1).await?;
            }
        }
    }
    Ok(())
}

/// A committed row a split writes.
struct SplitRow<'a> {
    at: At<'a>,
    json: &'a serde_json::Value,
    deleted: bool,
    /// Its record, a new one where `None`.
    key_id: Option<i64>,
    commit: &'a str,
}

impl<'a> SplitRow<'a> {
    /// A row holding `value`, a tombstone where that's `None`.
    fn value(
        at: At<'a>,
        value: Option<&'a serde_json::Value>,
        key_id: Option<i64>,
        commit: &'a str,
    ) -> Self {
        const GONE: serde_json::Value = serde_json::Value::Null;
        SplitRow {
            at,
            json: value.unwrap_or(&GONE),
            deleted: value.is_none(),
            key_id,
            commit,
        }
    }
}

/// Write `row` into segment `seg`: returns its id and `key_id`.
async fn insert<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    family: i64,
    seg: i64,
    row: SplitRow<'_>,
) -> Result<(i64, i64)> {
    let version = DB::next_version(tx, family, 1).await?;
    DB::insert(
        tx,
        seg,
        NewRow {
            at: row.at,
            key_id: row.key_id,
            commit_id: Some(row.commit),
            json: row.json,
            deleted: row.deleted,
            version,
            base_commit_id: None,
            base_json: None,
            settled: &[],
            conflict: None,
        },
    )
    .await
}
