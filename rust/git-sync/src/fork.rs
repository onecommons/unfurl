// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! A new worktree placed in its family: forks and splits
//! (docs/branch-segments.md §4.5–§4.7, C.13, C.16, C.17).

use std::collections::{BTreeMap, BTreeSet};

use crate::db::store::{Filter, Scope, Store};
use crate::db::tables::{At, FileRow, NewRecordRow, Place, RecordRow};
use crate::db::{self, on_pool, Db};
use crate::error::Result;
use crate::format::FormatRegistry;
use crate::git;
use crate::ids::History;
use crate::rollup::parse_commit_rollup;
use crate::scan::HeadFile;
use crate::segments;

/// The records of the tree at a commit, by place, less those their file
/// rejects or skips.
fn head_tree(files: &[HeadFile]) -> BTreeMap<Place, &serde_json::Value> {
    files
        .iter()
        .flat_map(|f| {
            f.records
                .iter()
                .filter(|(path, key, _)| !f.skips(path, key))
                .map(|(path, key, v)| (Place::new(&f.rel_path, path, key), v))
        })
        .collect()
}

/// Worktree `w`'s file rows at commit `n`. The scan that follows parses
/// every file again: a reset that kept the working tree leaves it holding
/// what the new chain doesn't, and the draft is classified again over the
/// new chain (§4.8, step 5).
async fn restamp_files<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    w: i64,
    n: &str,
    files: &[HeadFile],
) -> Result<()> {
    for f in files {
        DB::upsert_file(
            tx,
            w,
            &FileRow {
                path: &f.rel_path,
                format: &f.format,
                commit_id: Some(Some(n)),
                source_oid: Some(None),
                committed_oid: Some(f.blob.as_deref()),
            },
        )
        .await?;
    }
    Ok(())
}

/// A rebuild under way: worktree `w`'s new head `head` at commit `n`, and
/// the rows it's built from, by place.
struct Rebuilt<'a> {
    family: i64,
    w: i64,
    head: i64,
    n: &'a str,
    /// What `w` showed.
    old: BTreeMap<Place, Vec<&'a RecordRow>>,
    /// The base's state.
    view: BTreeMap<Place, Vec<&'a RecordRow>>,
    draft: BTreeMap<Place, Vec<&'a RecordRow>>,
    /// Ids the new tree keeps from the base.
    taken: BTreeSet<i64>,
}

/// The new head's row at `p`, whose value at `n` is `want`: none where
/// the base already holds it, and a tombstone where both lack it only
/// while `w` showed a record there another worktree's draft holds.
async fn rebuild_place<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    rb: &Rebuilt<'_>,
    p: &Place,
    want: Option<&serde_json::Value>,
) -> Result<()> {
    let below = live(&rb.view, p);
    if want == below.map(|r| &r.json)
        && (want.is_some()
            || shown(&rb.old, p).is_none()
            || !DB::held_elsewhere(tx, rb.w, p.as_at()).await?)
    {
        return Ok(());
    }
    // no rollup: the record the base shows continues, else the one `w`
    // showed, else the draft's; for a deletion, the one it deletes
    let key_id = match want {
        Some(_) => below.map(|r| r.key_id).or_else(|| {
            [live(&rb.old, p), shown(&rb.draft, p)]
                .into_iter()
                .flatten()
                .map(|r| r.key_id)
                .find(|id| !rb.taken.contains(id))
        }),
        None => shown(&rb.old, p).or(shown(&rb.view, p)).map(|r| r.key_id),
    };
    let (id, key_id) = insert(
        tx,
        rb.family,
        rb.head,
        SplitRow::value(p.as_at(), want, key_id, rb.n),
    )
    .await?;
    // other drafts' entries on what `w` showed there, where the content
    // is the same: an edit made over it was made over the new row too
    for r in rows_at(&rb.old, p) {
        if (!r.deleted).then_some(&r.json) != want {
            continue;
        }
        for (seg, tag) in DB::draft_entries_on(tx, r.id, rb.w).await? {
            DB::entry(tx, id, seg, tag).await?;
        }
    }
    for x in rows_at(&rb.view, p) {
        DB::entry(tx, x.id, rb.head, key_id).await?;
    }
    Ok(())
}

/// `rows` by place, in their order.
fn by_place(rows: &[RecordRow]) -> BTreeMap<Place, Vec<&RecordRow>> {
    let mut at: BTreeMap<Place, Vec<&RecordRow>> = BTreeMap::new();
    for r in rows {
        at.entry(Place::from(r)).or_default().push(r);
    }
    at
}

fn rows_at<'i, 'a>(
    index: &'i BTreeMap<Place, Vec<&'a RecordRow>>,
    p: &Place,
) -> &'i [&'a RecordRow] {
    index.get(p).map_or(&[], Vec::as_slice)
}

/// The first row at `p`: what the view shows there, a tombstone included.
fn shown<'a>(index: &BTreeMap<Place, Vec<&'a RecordRow>>, p: &Place) -> Option<&'a RecordRow> {
    rows_at(index, p).first().copied()
}

fn live<'a>(index: &BTreeMap<Place, Vec<&'a RecordRow>>, p: &Place) -> Option<&'a RecordRow> {
    rows_at(index, p).iter().copied().find(|r| !r.deleted)
}

/// Where a new worktree's HEAD falls in its family (§4.6).
enum Placement {
    /// At a segment whose state is HEAD's.
    At(i64),
    /// Inside segment `s`, whose head commit is `h`, at commit `c`.
    Inside { s: i64, h: String, c: String },
}

/// Find or create the worktree `(origin, branch)`, whose HEAD is `head`:
/// a new one joins a family and forks where HEAD falls in it (§4.5), or
/// else starts a family of its own. Its family is another branch of
/// `origin`'s, else the one the nearest git-sync commit in its history
/// names: a fork of another origin's.
pub(crate) async fn open(
    db: &Db,
    repo: gix::Repository,
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
        let mut family = Store::family_of_origin(&mut tx, origin).await?;
        tx.commit().await?;
        // the walk is git's alone, so no transaction is held open over it
        if let (None, Some(head)) = (family, head) {
            if let Some(named) = named_family(&repo, head, &database)? {
                // the root's origin, so its own family
                let mut tx = pool.begin().await?;
                family = Store::family_of_origin(&mut tx, &named).await?;
                tx.commit().await?;
            }
        }
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

/// The family the nearest commit this database made in `head`'s
/// first-parent history names.
fn named_family(repo: &gix::Repository, head: &str, database: &str) -> Result<Option<String>> {
    let mut at = Some(head.to_string());
    for _ in 0..NAMED_FAMILY_DEPTH {
        let Some(c) = at else { break };
        let family = git::commit_message(repo, &c)
            .and_then(|m| parse_commit_rollup(&m).ok().flatten())
            .filter(|r| r.database.as_deref() == Some(database))
            .and_then(|r| r.family);
        if family.is_some() {
            return Ok(family);
        }
        at = git::commit_parents(repo, &c)?.into_iter().next();
    }
    Ok(None)
}

/// How far back along its first parents a new branch looks for the
/// family trailer: on a repository git-sync never committed to, the whole
/// history would be read on its first open.
const NAMED_FAMILY_DEPTH: usize = 1000;

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
    let base = match place(&mut tx, &mut history, family, head).await? {
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
    history: &mut History<'_>,
    family: i64,
    head: &str,
) -> Result<Option<Placement>> {
    if let Some(placed) = place_commit(tx, history, family, head).await? {
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
        let Some(base) = git::merge_base(history.repo(), head, &h)? else {
            continue;
        };
        let descends = match &best {
            Some(b) => git::is_ancestor(history.repo(), b, &base)?,
            None => true,
        };
        if descends {
            best = Some(base);
        }
    }
    match best {
        Some(base) => place_commit(tx, history, family, &base).await,
        None => Ok(None),
    }
}

/// C.16: a segment ending at `c`, else the one `c` falls inside, down the
/// chain of a tracked head `c` is an ancestor of; one whose first-parent
/// history holds `c` first.
async fn place_commit<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    history: &mut History<'_>,
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
        if !git::is_ancestor(history.repo(), c, &h)? {
            continue;
        }
        let first = git::first_parent_contains(history.repo(), &h, c)?;
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
        if !git::is_ancestor(history.repo(), c, h)? {
            continue;
        }
        let below = chain.get(i + 1).and_then(|b| b.head_commit.as_deref());
        let in_below = match below {
            Some(b) => git::is_ancestor(history.repo(), c, b)?,
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
    below: Vec<RecordRow>,
    own: Option<RecordRow>,
    collides: bool,
}

impl Key {
    fn below_live(&self) -> Option<&RecordRow> {
        self.below.iter().find(|r| !r.deleted)
    }

    /// Whether `s` needs a row of its own at `c`: where the value at `c`
    /// isn't what's below (§4.7).
    fn recreated(&self) -> bool {
        self.v_c.as_ref() != self.below_live().map(|b| &b.json) || self.collides
    }
}

/// A split under way (§4.7): `s`, whose head commit is `h`, ends at `c`,
/// and `s2` above it holds the rest.
struct Split<'a> {
    family: i64,
    s: i64,
    s2: i64,
    c: &'a str,
    h: &'a str,
    /// `s` and its ancestors.
    lower: BTreeSet<i64>,
    /// The segment below `s`.
    parent: Option<i64>,
    /// The head commit of the segment below `s`.
    below_commit: Option<String>,
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
    let chain = DB::segment_chain(tx, s).await?;
    let own = DB::rows_in(tx, s, None, false).await?;
    let late = written_after(history, &own, c)?;
    let sp = Split {
        family,
        s,
        s2: DB::split_segment(tx, s, c).await?,
        c,
        h,
        lower: chain.iter().map(|seg| seg.id).collect(),
        parent: chain.get(1).map(|seg| seg.id),
        below_commit: chain.get(1).and_then(|seg| seg.head_commit.clone()),
    };
    let places = split_places(history, &late, c, h)?;
    // the ids the tree at `c` keeps at the other places: taken
    let state = DB::visible(tx, s, Scope::State, Filter::All).await?;
    let kept: Vec<&RecordRow> = state
        .iter()
        .filter(|r| !r.deleted && !places.contains(&Place::from(*r)))
        .collect();
    let mut taken: BTreeSet<i64> = kept.iter().map(|r| r.key_id).collect();
    let keys = split_keys(tx, &sp, history, &own, &late, &places, &mut taken).await?;
    let recovered = recover_ids(&sp, history, &keys, &late, &kept, &mut taken)?;
    for (p, key) in &keys {
        let id = recovered.get(p).copied();
        match &key.own {
            Some(own) => rewrite_own(tx, &sp, p, key, own, id).await?,
            None if key.recreated() => restore_below(tx, &sp, history, p, key, id).await?,
            // what's below is the value at `c`: `s2` takes `s`'s entries
            None => DB::move_entries(tx, s, sp.s2, p.as_at()).await?,
        }
    }
    Ok(())
}

/// `own`'s rows written after `c`: by a commit `c` is an ancestor of.
fn written_after(history: &History<'_>, own: &[RecordRow], c: &str) -> Result<Vec<RecordRow>> {
    let mut late = Vec::new();
    for r in own {
        if let Some(rc) = r.commit_id.as_deref() {
            if rc != c && git::is_ancestor(history.repo(), c, rc)? {
                late.push(r.clone());
            }
        }
    }
    Ok(late)
}

/// The places that can differ between `c` and `h`: in files that differ,
/// and rows written after `c`, whatever their value, since a tombstone
/// kept for another draft's key changes nothing in git.
fn split_places(
    history: &mut History<'_>,
    late: &[RecordRow],
    c: &str,
    h: &str,
) -> Result<BTreeSet<Place>> {
    let mut places: BTreeSet<Place> = late.iter().map(Place::from).collect();
    for file in git::changed_paths(history.repo(), c, h)? {
        let (at_c, at_h) = (history.records(c, &file)?, history.records(h, &file)?);
        for (path, key) in at_c.keys().chain(at_h.keys()) {
            let k = (path.clone(), key.clone());
            if at_c.get(&k) != at_h.get(&k) {
                places.insert(Place::new(&file, &k.0, &k.1));
            }
        }
    }
    Ok(places)
}

/// What the split knows of each place.
async fn split_keys<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    sp: &Split<'_>,
    history: &mut History<'_>,
    own: &[RecordRow],
    late: &[RecordRow],
    places: &BTreeSet<Place>,
    taken: &mut BTreeSet<i64>,
) -> Result<BTreeMap<Place, Key>> {
    let mut keys = BTreeMap::new();
    for p in places {
        let below = match sp.parent {
            Some(parent) => DB::visible(tx, parent, Scope::State, Filter::At(p.as_at())).await?,
            None => Vec::new(),
        };
        let own = own.iter().find(|r| Place::from(*r) == *p).cloned();
        let v_c = if history.skips(sp.c, &p.file_path, &p.path, &p.key)? {
            // a scan at `c` left the row it had: `s`'s own from by then,
            // else the one below
            match own.iter().find(|r| !late.iter().any(|l| l.id == r.id)) {
                Some(r) => (!r.deleted).then(|| r.json.clone()),
                None => below.iter().find(|r| !r.deleted).map(|r| r.json.clone()),
            }
        } else {
            history.value(sp.c, &p.file_path, &p.path, &p.key)?
        };
        let mut key = Key {
            v_c,
            below,
            own,
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
    Ok(keys)
}

/// §4.7: the re-created rows' ids, strongest evidence first across all
/// of them, each id once: rollup, move, continuing record, row below.
fn recover_ids(
    sp: &Split<'_>,
    history: &mut History<'_>,
    keys: &BTreeMap<Place, Key>,
    late: &[RecordRow],
    kept: &[&RecordRow],
    taken: &mut BTreeSet<i64>,
) -> Result<BTreeMap<Place, i64>> {
    let mut recovered: BTreeMap<Place, i64> = BTreeMap::new();
    for tier in 1..=4 {
        for (p, key) in keys.iter().filter(|(_, k)| k.recreated()) {
            if recovered.contains_key(p) {
                continue;
            }
            let id = match tier {
                1 => history.id(sp.c, &p.file_path, &p.path, &p.key)?,
                2 => moved_id(late, p, key),
                3 => match &key.own {
                    Some(r) if history.continuous(sp.c, sp.h, &p.file_path, &p.path, &p.key)? => {
                        Some(r.key_id)
                    }
                    _ => None,
                },
                _ => below_id(sp, history, keys, kept, p, key)?,
            };
            if let Some(id) = id.filter(|id| !taken.contains(id)) {
                taken.insert(id);
                recovered.insert(p.clone(), id);
            }
        }
    }
    Ok(recovered)
}

/// The id of a row written after `c` in another file with the value at
/// `c`: the record moved.
fn moved_id(late: &[RecordRow], p: &Place, key: &Key) -> Option<i64> {
    late.iter()
        .find(|r| {
            r.file_path != p.file_path
                && (&r.path, &r.key) == (&p.path, &p.key)
                && !r.deleted
                && Some(&r.json) == key.v_c.as_ref()
        })
        .map(|r| r.key_id)
}

/// The row below's id, unless its value moved elsewhere or the record
/// didn't last from the segment below to `c`.
fn below_id(
    sp: &Split<'_>,
    history: &mut History<'_>,
    keys: &BTreeMap<Place, Key>,
    kept: &[&RecordRow],
    p: &Place,
    key: &Key,
) -> Result<Option<i64>> {
    let Some(b) = key.below.first().filter(|b| !b.deleted) else {
        return Ok(None);
    };
    let moved = kept
        .iter()
        .any(|r| Place::from(*r) != *p && r.json == b.json)
        || keys
            .iter()
            .any(|(q, k)| q != p && k.v_c.as_ref() == Some(&b.json));
    let held = match &sp.below_commit {
        Some(bc) => history.continuous(bc, sp.c, &p.file_path, &p.path, &p.key)?,
        None => true,
    };
    Ok((!moved && held).then_some(b.key_id))
}

/// A place `s` has a row at: the row moves to `s2`, and `s` gets one
/// with the value at `c` where it differs.
async fn rewrite_own<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    sp: &Split<'_>,
    p: &Place,
    key: &Key,
    own: &RecordRow,
    id: Option<i64>,
) -> Result<()> {
    let a = p.as_at();
    DB::move_row(tx, own.id, sp.s2).await?;
    if !key.recreated() {
        DB::move_entries(tx, sp.s, sp.s2, a).await?;
        return Ok(());
    }
    let new = insert(
        tx,
        sp.family,
        sp.s,
        SplitRow::value(a, key.v_c.as_ref(), id, sp.c),
    )
    .await?;
    DB::entry(tx, new.0, sp.s2, own.key_id).await?;
    Ok(())
}

/// A place changed before `c` and back after it: `s` gets the value at
/// `c`, and `s2` restores the value at `h`.
async fn restore_below<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    sp: &Split<'_>,
    history: &mut History<'_>,
    p: &Place,
    key: &Key,
    id: Option<i64>,
) -> Result<()> {
    let a = p.as_at();
    let new = insert(
        tx,
        sp.family,
        sp.s,
        SplitRow::value(a, key.v_c.as_ref(), id, sp.c),
    )
    .await?;
    for x in &key.below {
        DB::entry(tx, x.id, sp.s, new.1).await?;
    }
    // `s` may hide the row below by an entry alone (a fold took the
    // tombstone), so `s2` restores git's value at its end
    let v_h = history.value(sp.h, &p.file_path, &p.path, &p.key)?;
    let copy = key
        .below
        .first()
        .filter(|b| (!b.deleted).then_some(&b.json) == v_h.as_ref());
    let restored = match copy {
        Some(b) => {
            let row = SplitRow {
                at: a,
                json: &b.json,
                deleted: b.deleted,
                key_id: Some(b.key_id),
                commit: sp.h,
            };
            insert(tx, sp.family, sp.s2, row).await?
        }
        None => {
            let id = history
                .id(sp.h, &p.file_path, &p.path, &p.key)?
                .or(key.below.first().map(|b| b.key_id));
            insert(
                tx,
                sp.family,
                sp.s2,
                SplitRow::value(a, v_h.as_ref(), id, sp.h),
            )
            .await?
        }
    };
    // it stands for a version older than anything a segment outside `s`
    // and its ancestors holds there: each supersedes it
    let mut newer: BTreeMap<i64, i64> = BTreeMap::new();
    for r in DB::rows_at(tx, sp.family, a).await? {
        if r.segment_id != sp.s2 && !sp.lower.contains(&r.segment_id) {
            newer.entry(r.segment_id).or_insert(r.key_id);
        }
    }
    for (seg, key_id) in newer {
        DB::entry(tx, restored.0, seg, key_id).await?;
    }
    DB::entry(tx, new.0, sp.s2, restored.1).await?;
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
        NewRecordRow::new(
            row.at,
            row.key_id,
            Some(row.commit),
            row.json,
            row.deleted,
            version,
        ),
    )
    .await
}

/// §4.8, C.18: worktree `w`'s HEAD `n` doesn't descend from the commit its
/// chain holds, after a rebase, reset or force-push: build its head again
/// on the base placement finds, from `files`, every file of the tree at `n`.
pub(crate) async fn rebuild(
    db: &Db,
    repo: gix::Repository,
    formats: &FormatRegistry,
    w: i64,
    family: i64,
    n: &str,
    files: &[HeadFile],
) -> Result<()> {
    let database = db::commit::database_id(db).await?;
    on_pool!(db, pool => {
        let mut tx = pool.begin().await?;
        Store::lock_family(&mut tx, family).await?;
        let mut history = History::new(formats, repo, database);
        // a scan running alongside rebuilt first
        if let Some(recorded) = Store::head_commit(&mut tx, w).await? {
            if recorded == n || git::is_ancestor(history.repo(), &recorded, n)? {
                return Ok(());
            }
        }
        let base = match place(&mut tx, &mut history, family, n).await? {
            None => None,
            Some(Placement::At(seg)) => Store::prepare_base(&mut tx, family, seg, n).await?,
            Some(Placement::Inside { s, h, c }) => {
                split(&mut tx, &mut history, family, s, &c, &h).await?;
                Some(s)
            }
        };
        rebuild_on(&mut tx, family, w, base, n, files).await?;
        // `w`'s old exclusive segments are in no chain now (§4.13)
        Store::compact(&mut tx, family).await?;
        tx.commit().await?;
        Ok(())
    })
}

/// C.19: delete worktree `w` of `family`, then compact what only it used.
pub(crate) async fn delete(db: &Db, w: i64, family: i64) -> Result<()> {
    on_pool!(db, pool => {
        let mut tx = pool.begin().await?;
        Store::lock_family(&mut tx, family).await?;
        Store::delete_worktree(&mut tx, w).await?;
        // a root alone took its family with it
        if family != w {
            Store::compact(&mut tx, family).await?;
        }
        tx.commit().await?;
        Ok(())
    })
}

/// The new head's rows: where the tree at `n` differs from `base`'s state,
/// and a tombstone where `w` showed a record another draft still holds.
async fn rebuild_on<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    family: i64,
    w: i64,
    base: Option<i64>,
    n: &str,
    files: &[HeadFile],
) -> Result<()> {
    let tree = head_tree(files);
    let old = DB::visible(tx, w, Scope::Chain, Filter::All).await?;
    let view = match base {
        Some(b) => DB::visible(tx, b, Scope::State, Filter::All).await?,
        None => Vec::new(),
    };
    let d = DB::segs(tx, w).await?.draft;
    let draft = DB::rows_in(tx, d, None, false).await?;
    let head = DB::new_head(tx, family, base, n, w).await?;
    let rb = Rebuilt {
        family,
        w,
        head,
        n,
        old: by_place(&old),
        view: by_place(&view),
        draft: by_place(&draft),
        // the base's ids stay unique in the new tree
        taken: view
            .iter()
            .filter(|r| !r.deleted && tree.contains_key(&Place::from(*r)))
            .map(|r| r.key_id)
            .collect(),
    };
    // a place its file rejects keeps the base's row, as a scan leaves one
    let skipped = |p: &Place| {
        files
            .iter()
            .any(|f| f.rel_path == p.file_path && f.skips(&p.path, &p.key))
    };
    let places: BTreeSet<Place> = tree
        .keys()
        .cloned()
        .chain(view.iter().map(Place::from))
        .chain(old.iter().map(Place::from))
        .filter(|p| !skipped(p))
        .collect();
    for p in places {
        rebuild_place(tx, &rb, &p, tree.get(&p).copied()).await?;
    }
    DB::rechain(tx, family, w, base, head, n).await?;
    restamp_files(tx, w, n, files).await?;
    segments::follow_records(tx, w).await?;
    DB::drop_stale_entries(tx, w, d).await?;
    segments::relink(tx, w).await?;
    Ok(())
}
