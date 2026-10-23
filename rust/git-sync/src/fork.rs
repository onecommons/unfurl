// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! A new worktree placed in its family: forks and splits
//! (docs/branch-segments.md §4.5–§4.7, C.13, C.16, C.17).

use crate::commit::CommitId;
use std::collections::{BTreeMap, BTreeSet};

use crate::db::store::{Filter, Scope, Store};
use crate::db::tables::{At, FileRow, NewRecordRow, Place, RecordRow, SegmentEnd};
use crate::db::{self, on_pool, Db};
use crate::error::{Error, Result};
use crate::format::FormatRegistry;
use crate::git;
use crate::ids::History;
use crate::model::CommitRollup;
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

/// Worktree `w`'s file rows at commit `new_head`. The scan that follows parses
/// every file again: a reset that kept the working tree leaves it holding
/// what the new chain doesn't, and the draft is classified again over the
/// new chain (§4.8, step 5).
async fn restamp_files<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    w: i64,
    new_head: CommitId,
    files: &[HeadFile],
) -> Result<()> {
    for f in files {
        DB::upsert_file(
            tx,
            w,
            &FileRow {
                path: &f.rel_path,
                format: &f.format,
                commit_id: Some(Some(new_head)),
                source_oid: Some(None),
                committed_oid: Some(f.blob.as_deref()),
            },
        )
        .await?;
    }
    Ok(())
}

/// A rebuild under way: worktree `w`'s new head `head` at commit `new_head`, and
/// the rows it's built from, by place.
struct Rebuilt<'a> {
    family: i64,
    w: i64,
    head: i64,
    new_head: CommitId,
    /// What `w` showed.
    old: BTreeMap<Place, Vec<&'a RecordRow>>,
    /// The base's state.
    view: BTreeMap<Place, Vec<&'a RecordRow>>,
    draft: BTreeMap<Place, Vec<&'a RecordRow>>,
    /// Ids the new tree keeps from the base.
    taken: BTreeSet<i64>,
}

/// The new head's row at `p`, whose value at `new_head` is `want`: none where
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
        SplitRow::value(p.as_at(), want, key_id, rb.new_head),
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
    /// Inside segment `s`, whose head commit is `segment_head`, at commit
    /// `split_at`.
    Inside {
        s: i64,
        segment_head: CommitId,
        split_at: CommitId,
    },
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
    head: Option<CommitId>,
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
                let history = History::open(formats, repo, db, family).await?;
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
fn named_family(repo: &gix::Repository, head: CommitId, database: &str) -> Result<Option<String>> {
    let named = nearest_rollup(repo, head, |r| {
        r.database.as_deref() == Some(database) && r.family.is_some()
    })?;
    Ok(named.and_then(|r| r.family))
}

/// The `since_version` that reads every record written after the nearest
/// commit this database made in `head`'s first-parent history.
pub(crate) fn written_since(
    repo: &gix::Repository,
    head: CommitId,
    database: &str,
) -> Result<Option<i64>> {
    let ours = nearest_rollup(repo, head, |r| r.database.as_deref() == Some(database))?;
    Ok(ours.map(|r| r.next_version - 1))
}

/// The rollup of the nearest commit in `head`'s first-parent history that
/// has one `wanted` accepts.
fn nearest_rollup(
    repo: &gix::Repository,
    head: CommitId,
    wanted: impl Fn(&CommitRollup) -> bool,
) -> Result<Option<CommitRollup>> {
    let mut at = Some(head);
    for _ in 0..NAMED_FAMILY_DEPTH {
        let Some(c) = at else { break };
        let rollup = git::commit_message(repo, c)
            .and_then(|m| parse_commit_rollup(&m).ok().flatten())
            .filter(&wanted);
        if rollup.is_some() {
            return Ok(rollup);
        }
        at = git::commit_parents(repo, c)?.into_iter().next();
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
    head: CommitId,
) -> Result<Option<i64>> {
    let mut tx = pool.begin().await?;
    // before any read: SQLite can't take the write lock after one
    DB::lock_family(&mut tx, family).await?;
    let id = fork_in(&mut tx, &mut history, family, origin, branch, head).await?;
    if id.is_some() {
        tx.commit().await?;
    }
    Ok(id)
}

/// [`fork_into`] in the caller's transaction, which holds the family lock.
pub(crate) async fn fork_in<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    history: &mut History<'_>,
    family: i64,
    origin: &str,
    branch: &str,
    head: CommitId,
) -> Result<Option<i64>> {
    let base = match place(tx, history, family, head).await? {
        None => return Ok(None),
        Some(Placement::At(seg)) => seg,
        Some(Placement::Inside {
            s,
            segment_head,
            split_at,
        }) => {
            split(tx, history, family, s, split_at, segment_head).await?;
            s
        }
    };
    Ok(Some(
        DB::fork_at(tx, family, base, origin, branch, head).await?,
    ))
}

/// §4.6: where `head` falls in the family, else where its latest
/// merge-base with the tracked heads does; the first scan brings a head
/// placed there up to `head`.
async fn place<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    history: &mut History<'_>,
    family: i64,
    head: CommitId,
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
    let mut best: Option<CommitId> = None;
    for tracked in DB::family_heads(tx, family).await? {
        let Some(h) = tracked.head_commit else {
            continue;
        };
        let Some(base) = git::merge_base(history.repo(), head, h)? else {
            continue;
        };
        let descends = match best {
            Some(b) => git::is_ancestor(history.repo(), b, base)?,
            None => true,
        };
        if descends {
            best = Some(base);
        }
    }
    match best {
        Some(base) => place_commit(tx, history, family, base).await,
        None => Ok(None),
    }
}

/// C.16: a segment ending at `commit`, else the one `commit` falls inside, down the
/// chain of a tracked head `commit` is an ancestor of; one whose first-parent
/// history holds `commit` first.
async fn place_commit<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    history: &mut History<'_>,
    family: i64,
    commit: CommitId,
) -> Result<Option<Placement>> {
    if let Some(seg) = DB::segment_ending_at(tx, family, commit).await? {
        return Ok(Some(Placement::At(seg)));
    }
    let mut top: Option<(i64, bool)> = None;
    for tracked in DB::family_heads(tx, family).await? {
        let Some(h) = tracked.head_commit else {
            continue;
        };
        if !git::is_ancestor(history.repo(), commit, h)? {
            continue;
        }
        let first = git::first_parent_contains(history.repo(), h, commit)?;
        if top.is_none_or(|(_, f)| first && !f) {
            top = Some((tracked.id, first));
        }
    }
    let Some((top, _)) = top else {
        return Ok(None);
    };
    let chain = DB::segment_chain(tx, top).await?;
    for (i, seg) in chain.iter().enumerate() {
        let Some(h) = seg.head_commit else {
            continue;
        };
        if !git::is_ancestor(history.repo(), commit, h)? {
            continue;
        }
        let below = chain.get(i + 1).and_then(|b| b.head_commit);
        let in_below = match below {
            Some(b) => git::is_ancestor(history.repo(), commit, b)?,
            None => false,
        };
        if !in_below {
            return Ok(Some(Placement::Inside {
                s: seg.id,
                segment_head: h,
                split_at: commit,
            }));
        }
    }
    Ok(None)
}

/// A commit a scan brings in of another worktree's line: `merged`, which
/// `merge`, a commit of HEAD's first-parent line, brought in, and the
/// worktree whose first-parent line holds it, if one does.
pub(crate) struct Merged {
    merge: CommitId,
    merged: CommitId,
    line: Option<i64>,
}

/// The commits a scan from `recorded` up to `head` brings in of other
/// worktrees' lines. A commit another worktree's line holds brings in
/// itself; otherwise a merge brings in its other parents.
pub(crate) async fn merged_in<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    history: &mut History<'_>,
    family: i64,
    w: i64,
    recorded: CommitId,
    head: CommitId,
) -> Result<Vec<Merged>> {
    let own_head = DB::segs(tx, w).await?.head;
    let own: BTreeSet<CommitId> = DB::segment_chain(tx, own_head)
        .await?
        .into_iter()
        .filter_map(|seg| seg.head_commit)
        .collect();
    let others: Vec<(i64, CommitId)> = DB::family_lines(tx, family)
        .await?
        .into_iter()
        .filter(|&(u, _)| u != w)
        .filter_map(|(u, h)| h.map(|h| (u, h)))
        .collect();
    merged_on_line(history.repo(), &own, &others, recorded, head)
}

/// [`merged_in`]'s walk of `head`'s first-parent line down to `recorded`
/// or a commit ending a segment of the scanning worktree's chain, `own`.
fn merged_on_line(
    repo: &gix::Repository,
    own: &BTreeSet<CommitId>,
    others: &[(i64, CommitId)],
    recorded: CommitId,
    head: CommitId,
) -> Result<Vec<Merged>> {
    let lines = other_lines(repo, own, others)?;
    let mut merged = Vec::new();
    let mut at = head;
    while at != recorded && !own.contains(&at) {
        if let Some(&u) = lines.get(&at) {
            merged.push(Merged {
                merge: at,
                merged: at,
                line: Some(u),
            });
            break;
        }
        let parents = git::commit_parents(repo, at)?;
        merged.extend(
            parents
                .iter()
                .skip(1)
                .filter(|p| !own.contains(p))
                .map(|&p| Merged {
                    merge: at,
                    merged: p,
                    line: None,
                }),
        );
        match parents.first() {
            Some(&p) => at = p,
            None => break,
        }
    }
    Ok(merged)
}

/// The commits on each of `others`' first-parent lines, each walked from
/// its head down to the first commit ending a segment of `own`, with the
/// worktree whose line holds it, the lowest id first.
fn other_lines(
    repo: &gix::Repository,
    own: &BTreeSet<CommitId>,
    others: &[(i64, CommitId)],
) -> Result<BTreeMap<CommitId, i64>> {
    let mut lines = BTreeMap::new();
    for &(u, h) in others {
        if !git::has_commit(repo, h)? {
            continue;
        }
        let mut at = h;
        // below `own`, an ancestor of what the scan brings in, nothing it
        // brings in is held
        while !own.contains(&at) {
            lines.entry(at).or_insert(u);
            match git::commit_parents(repo, at)?.first() {
                Some(&p) => at = p,
                None => break,
            }
        }
    }
    Ok(lines)
}

/// The commits a scan of worktree `w` up to `head` brings in of other
/// worktrees' lines ([`merged_in`]), each made to end a segment.
pub(crate) async fn end_merged_lines<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    history: Option<&mut History<'_>>,
    family: i64,
    w: i64,
    head: CommitId,
) -> Result<Vec<Merged>> {
    let (Some(recorded), Some(history)) = (DB::head_commit(tx, w).await?, history) else {
        return Ok(Vec::new());
    };
    if recorded == head {
        return Ok(Vec::new());
    }
    let merged = merged_in(tx, history, family, w, recorded, head).await?;
    end_merged(tx, history, family, &merged).await?;
    Ok(merged)
}

/// Make each commit `merged` brought in end a segment, as a fork placed
/// there would: closing the head ending there, or splitting the segment
/// it falls inside.
async fn end_merged<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    history: &mut History<'_>,
    family: i64,
    merged: &[Merged],
) -> Result<()> {
    for m in merged {
        let placed = match m.line {
            Some(u) => place_on_line(tx, history, family, u, m.merged).await?,
            None => place_commit(tx, history, family, m.merged).await?,
        };
        match placed {
            None => {}
            Some(Placement::At(seg)) => DB::close_head(tx, family, seg).await?,
            Some(Placement::Inside {
                s,
                segment_head,
                split_at,
            }) => split(tx, history, family, s, split_at, segment_head).await?,
        }
    }
    Ok(())
}

/// [`place_commit`] of `c`, which worktree `u`'s first-parent line holds:
/// a segment ending at `c`, else the one of `u`'s chain `c` falls inside.
async fn place_on_line<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    history: &mut History<'_>,
    family: i64,
    u: i64,
    c: CommitId,
) -> Result<Option<Placement>> {
    if let Some(seg) = DB::segment_ending_at(tx, family, c).await? {
        return Ok(Some(Placement::At(seg)));
    }
    let head = DB::segs(tx, u).await?.head;
    let chain = DB::segment_chain(tx, head).await?;
    line_segment(history.repo(), &chain, c)
}

/// The segment of `chain`, top down, holding `c`, walking the first-parent
/// line from its head.
fn line_segment(
    repo: &gix::Repository,
    chain: &[SegmentEnd],
    c: CommitId,
) -> Result<Option<Placement>> {
    let Some(mut at) = chain.first().and_then(|seg| seg.head_commit) else {
        return Ok(None);
    };
    let mut i = 0;
    loop {
        while chain.get(i + 1).and_then(|seg| seg.head_commit) == Some(at) {
            i += 1;
        }
        if at == c {
            let seg = &chain[i];
            return Ok(Some(match seg.head_commit {
                Some(h) if h != c => Placement::Inside {
                    s: seg.id,
                    segment_head: h,
                    split_at: c,
                },
                _ => Placement::At(seg.id),
            }));
        }
        match git::commit_parents(repo, at)?.first() {
            Some(&p) => at = p,
            None => return Ok(None),
        }
    }
}

/// An edge from worktree `w`'s head to the segment ending at each commit
/// `merged` brought in.
pub(crate) async fn record_merges<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    family: i64,
    w: i64,
    merged: &[Merged],
) -> Result<()> {
    let head = DB::segs(tx, w).await?.head;
    for m in merged {
        if let Some(seg) = DB::segment_ending_at(tx, family, m.merged).await? {
            DB::add_merge(tx, head, m.merge, seg).await?;
        }
    }
    Ok(())
}

/// What a split knows of one place (§4.7): its value at `split_at`, the rows
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

    /// Whether `s` needs a row of its own at `split_at`: where the value at `split_at`
    /// isn't what's below (§4.7).
    fn recreated(&self) -> bool {
        self.v_c.as_ref() != self.below_live().map(|b| &b.json) || self.collides
    }
}

/// A split under way (§4.7): `s`, whose head commit is `segment_head`, ends at `split_at`,
/// and `s2` above it holds the rest.
struct Split {
    family: i64,
    s: i64,
    s2: i64,
    split_at: CommitId,
    segment_head: CommitId,
    /// `s` and its ancestors.
    lower: BTreeSet<i64>,
    /// The segment below `s`.
    parent: Option<i64>,
    /// The head commit of the segment below `s`.
    below_commit: Option<CommitId>,
}

/// §4.7, C.17: segment `s`, whose head commit is `segment_head`, ends at `split_at` instead,
/// and a new segment above it holds the rest. Every view is unchanged.
async fn split<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    history: &mut History<'_>,
    family: i64,
    s: i64,
    split_at: CommitId,
    segment_head: CommitId,
) -> Result<()> {
    let chain = DB::segment_chain(tx, s).await?;
    let own = DB::rows_in(tx, s, None, false).await?;
    let late = written_after(history, &own, split_at)?;
    let merges = DB::merges_in(tx, s).await?;
    let later = merges_after(history, merges, split_at)?;
    let s2 = DB::split_segment(tx, s, split_at).await?;
    DB::split_merges(tx, s, s2, &later).await?;
    let sp = Split {
        family,
        s,
        s2,
        split_at,
        segment_head,
        lower: chain.iter().map(|seg| seg.id).collect(),
        parent: chain.get(1).map(|seg| seg.id),
        below_commit: chain.get(1).and_then(|seg| seg.head_commit),
    };
    let places = split_places(history, &late, split_at, segment_head)?;
    // the ids the tree at `split_at` keeps at the other places: taken
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
            // what's below is the value at `split_at`: `s2` takes `s`'s entries
            None => DB::move_entries(tx, s, sp.s2, p.as_at()).await?,
        }
    }
    Ok(())
}

/// The merge commits of `merges` after `split_at`, which `split_at` is an
/// ancestor of.
fn merges_after(
    history: &History<'_>,
    merges: Vec<CommitId>,
    split_at: CommitId,
) -> Result<Vec<CommitId>> {
    let mut later = Vec::new();
    for m in merges {
        if m != split_at && git::is_ancestor(history.repo(), split_at, m)? {
            later.push(m);
        }
    }
    Ok(later)
}

/// `own`'s rows written after `split_at`: by a commit `split_at` is an ancestor of.
fn written_after(
    history: &History<'_>,
    own: &[RecordRow],
    split_at: CommitId,
) -> Result<Vec<RecordRow>> {
    let mut late = Vec::new();
    for r in own {
        if let Some(rc) = r.commit_id {
            if rc != split_at && git::is_ancestor(history.repo(), split_at, rc)? {
                late.push(r.clone());
            }
        }
    }
    Ok(late)
}

/// The places that can differ between `split_at` and `segment_head`: in files that differ,
/// and rows written after `split_at`, whatever their value, since a tombstone
/// kept for another draft's key changes nothing in git.
fn split_places(
    history: &mut History<'_>,
    late: &[RecordRow],
    split_at: CommitId,
    segment_head: CommitId,
) -> Result<BTreeSet<Place>> {
    let mut places: BTreeSet<Place> = late.iter().map(Place::from).collect();
    for file in git::changed_paths(history.repo(), split_at, segment_head)? {
        let (at_c, at_h) = (
            history.records(split_at, &file)?,
            history.records(segment_head, &file)?,
        );
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
    sp: &Split,
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
        let v_c = if history.skips(sp.split_at, &p.file_path, &p.path, &p.key)? {
            // a scan at `split_at` left the row it had: `s`'s own from by then,
            // else the one below
            match own.iter().find(|r| !late.iter().any(|l| l.id == r.id)) {
                Some(r) => (!r.deleted).then(|| r.json.clone()),
                None => below.iter().find(|r| !r.deleted).map(|r| r.json.clone()),
            }
        } else {
            history.value(sp.split_at, &p.file_path, &p.path, &p.key)?
        };
        let mut key = Key {
            v_c,
            below,
            own,
            collides: false,
        };
        // a changed key whose value at `split_at` the row below already holds,
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
    sp: &Split,
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
                1 => history.id(sp.split_at, &p.file_path, &p.path, &p.key)?,
                2 => moved_id(late, p, key),
                3 => match &key.own {
                    Some(r)
                        if history.continuous(
                            sp.split_at,
                            sp.segment_head,
                            &p.file_path,
                            &p.path,
                            &p.key,
                        )? =>
                    {
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

/// The id of a row written after `split_at` in another file with the value at
/// `split_at`: the record moved.
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
/// didn't last from the segment below to `split_at`.
fn below_id(
    sp: &Split,
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
        Some(bc) => history.continuous(*bc, sp.split_at, &p.file_path, &p.path, &p.key)?,
        None => true,
    };
    Ok((!moved && held).then_some(b.key_id))
}

/// A place `s` has a row at: the row moves to `s2`, and `s` gets one
/// with the value at `split_at` where it differs.
async fn rewrite_own<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    sp: &Split,
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
        SplitRow::value(a, key.v_c.as_ref(), id, sp.split_at),
    )
    .await?;
    DB::entry(tx, new.0, sp.s2, own.key_id).await?;
    Ok(())
}

/// A place changed before `split_at` and back after it: `s` gets the value at
/// `split_at`, and `s2` restores the value at `segment_head`.
async fn restore_below<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    sp: &Split,
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
        SplitRow::value(a, key.v_c.as_ref(), id, sp.split_at),
    )
    .await?;
    for x in &key.below {
        DB::entry(tx, x.id, sp.s, new.1).await?;
    }
    // `s` may hide the row below by an entry alone (a fold took the
    // tombstone), so `s2` restores git's value at its end
    let v_h = history.value(sp.segment_head, &p.file_path, &p.path, &p.key)?;
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
                commit: sp.segment_head,
            };
            insert(tx, sp.family, sp.s2, row).await?
        }
        None => {
            let id = history
                .id(sp.segment_head, &p.file_path, &p.path, &p.key)?
                .or(key.below.first().map(|b| b.key_id));
            insert(
                tx,
                sp.family,
                sp.s2,
                SplitRow::value(a, v_h.as_ref(), id, sp.segment_head),
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
    commit: CommitId,
}

impl<'a> SplitRow<'a> {
    /// A row holding `value`, a tombstone where that's `None`.
    fn value(
        at: At<'a>,
        value: Option<&'a serde_json::Value>,
        key_id: Option<i64>,
        commit: CommitId,
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

/// A committed view: each live record's content, and the commit it was
/// committed in, by place.
/// `None` content for a tombstone.
type View = BTreeMap<(String, String, String), (Option<serde_json::Value>, Option<CommitId>)>;

/// Worktree `w`'s committed view.
async fn committed_view<DB: Store>(tx: &mut sqlx::Transaction<'_, DB>, w: i64) -> Result<View> {
    Ok(DB::visible(tx, w, Scope::Chain, Filter::All)
        .await?
        .into_iter()
        .map(|r| {
            let json = (!r.deleted).then_some(r.json);
            ((r.file_path, r.path, r.key), (json, r.commit_id))
        })
        .collect())
}

/// Whether a record committed as `was`, `None` for a deletion, is lost
/// where a view now shows `now`.
fn loses(was: Option<&serde_json::Value>, now: Option<&serde_json::Value>) -> bool {
    match was {
        Some(was) => now != Some(was),
        None => now.is_some(),
    }
}

/// How many records `before` shows that a commit the repository doesn't
/// have made, and `after` doesn't show: what a rebuild from `before` to
/// `after` loses. A record upstream changed isn't one.
fn lost(repo: &gix::Repository, before: &View, after: &View) -> Result<usize> {
    let mut missing: BTreeMap<CommitId, bool> = BTreeMap::new();
    let mut lost = 0;
    for (at, (json, commit)) in before {
        let now = after.get(at).and_then(|(now, _)| now.as_ref());
        if !loses(json.as_ref(), now) {
            continue;
        }
        let Some(commit) = *commit else {
            continue;
        };
        if let std::collections::btree_map::Entry::Vacant(e) = missing.entry(commit) {
            e.insert(!git::has_commit(repo, commit)?);
        }
        if missing[&commit] {
            lost += 1;
        }
    }
    Ok(lost)
}

/// What a rebuild after a rewrite takes: §4.8, C.18.
pub(crate) struct Rebuild<'a> {
    pub(crate) w: i64,
    pub(crate) family: i64,
    /// The new HEAD, and every file of its tree.
    pub(crate) new_head: CommitId,
    pub(crate) files: &'a [HeadFile],
    /// The commit `w`'s chain holds, when the repository doesn't have it:
    /// a rebuild whose committed view loses a record is refused.
    pub(crate) keep: Option<CommitId>,
    /// Where those records go instead of being refused.
    pub(crate) recover: Option<Recover<'a>>,
}

/// A branch the records commits the repository doesn't have made go to,
/// forked at `base`, the commit they were made on, with every file of its
/// tree: worktree `(origin, branch)`, exported from `from`.
pub(crate) struct Recover<'a> {
    pub(crate) origin: &'a str,
    pub(crate) branch: &'a str,
    pub(crate) from: &'a str,
    pub(crate) base: CommitId,
    pub(crate) files: &'a [HeadFile],
}

/// Worktree `w`'s HEAD `new_head` doesn't descend from the commit its chain
/// holds, after a rebase, reset or force-push: build its head again on the
/// base placement finds, from `files`, every file of the tree at `new_head`. With
/// `keep`, a rebuild whose committed view loses a record is refused, with
/// nothing changed, unless `recover` takes them to a branch: then the
/// branch's worktree, its edits in its draft.
pub(crate) async fn rebuild(
    db: &Db,
    repo: gix::Repository,
    formats: &FormatRegistry,
    rb: &Rebuild<'_>,
) -> Result<Option<i64>> {
    let (w, family, new_head) = (rb.w, rb.family, rb.new_head);
    let mut history = History::open(formats, repo, db, family).await?;
    on_pool!(db, pool => {
        let mut tx = pool.begin().await?;
        Store::lock_family(&mut tx, family).await?;
        // a scan running alongside rebuilt first
        if let Some(recorded) = Store::head_commit(&mut tx, w).await? {
            if recorded == new_head || git::is_ancestor(history.repo(), recorded, new_head)? {
                return Ok(None);
            }
        }
        let before = match rb.keep {
            Some(_) => committed_view(&mut tx, w).await?,
            None => BTreeMap::new(),
        };
        let recovered = match &rb.recover {
            Some(to) => recover(&mut tx, &mut history, rb, to).await?,
            None => None,
        };
        onto(&mut tx, &mut history, family, w, new_head, rb.files).await?;
        if let (Some(commit), None) = (rb.keep, recovered) {
            let after = committed_view(&mut tx, w).await?;
            let lost = lost(history.repo(), &before, &after)?;
            if lost > 0 {
                // the transaction rolls back as it drops
                return Err(Error::CommitMissing {
                    commit: commit.to_string(),
                    lost,
                    since: None,
                });
            }
        }
        if let Some(b) = recovered {
            segments::relink(&mut tx, b).await?;
        }
        // `w`'s old exclusive segments are in no chain now (§4.13)
        Store::compact(&mut tx, family).await?;
        tx.commit().await?;
        Ok(recovered)
    })
}

/// C.16 to C.18: build `w`'s head again on the base placement finds for
/// `new_head`, every file of whose tree is `files`. The draft stays.
pub(crate) async fn onto<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    history: &mut History<'_>,
    family: i64,
    w: i64,
    new_head: CommitId,
    files: &[HeadFile],
) -> Result<()> {
    let base = match place(tx, history, family, new_head).await? {
        None => None,
        Some(Placement::At(seg)) => DB::prepare_base(tx, family, seg).await?,
        Some(Placement::Inside {
            s,
            segment_head,
            split_at,
        }) => {
            split(tx, history, family, s, split_at, segment_head).await?;
            Some(s)
        }
    };
    rebuild_on(tx, family, w, base, new_head, files).await
}

/// When HEAD lacks a record the commits the repository doesn't have made,
/// take `w` back to the commit they were made on, fork the branch there,
/// and put those records in its draft. `None`, with nothing done, when
/// HEAD has them all.
async fn recover<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    history: &mut History<'_>,
    rb: &Rebuild<'_>,
    to: &Recover<'_>,
) -> Result<Option<i64>> {
    let rows = DB::visible(tx, rb.w, Scope::Chain, Filter::All).await?;
    let ours = missing_rows(history.repo(), rows)?;
    if !any_lost(&ours, rb.files) {
        return Ok(None);
    }
    claim(history.repo(), to.branch, to.base)?;
    // `w` at the base first, keeping its records' ids, so the branch forks
    // from its head there and `w`'s rebuild onto HEAD places on it
    rebuild_on(tx, rb.family, rb.w, None, to.base, to.files).await?;
    let Some(b) = fork_in(tx, history, rb.family, to.origin, to.branch, to.base).await? else {
        // nowhere to fork it: the rebuild refuses, or loses them, as
        // without recovery
        git::delete_branch_if_at(history.repo(), to.branch, to.base)?;
        return Ok(None);
    };
    DB::set_exporting_from(tx, b, to.from).await?;
    DB::no_clone_on_start(tx, b).await?;
    let at_base: BTreeMap<Place, RecordRow> = DB::visible(tx, b, Scope::Chain, Filter::All)
        .await?
        .into_iter()
        .filter(|r| !r.deleted)
        .map(|r| (Place::from(&r), r))
        .collect();
    let edits: Vec<RecordRow> = ours
        .into_iter()
        .filter_map(|r| {
            let below = at_base.get(&Place::from(&r)).map(|b| b.json.clone());
            // a record the base has already, or a deletion of one it lacks
            let same = match r.deleted {
                true => below.is_none(),
                false => below.as_ref() == Some(&r.json),
            };
            (!same).then(|| RecordRow {
                commit_id: None,
                base_commit_id: Some(to.base),
                base_json: below,
                settled: Vec::new(),
                conflict: None,
                ..r
            })
        })
        .collect();
    crate::export::copy_edits(tx, b, &edits).await?;
    Ok(Some(b))
}

/// Of `rows`, those commits the repository doesn't have made.
fn missing_rows(repo: &gix::Repository, rows: Vec<RecordRow>) -> Result<Vec<RecordRow>> {
    let mut missing: BTreeMap<CommitId, bool> = BTreeMap::new();
    let mut ours = Vec::new();
    for r in rows {
        let Some(commit) = r.commit_id else {
            continue;
        };
        if let std::collections::btree_map::Entry::Vacant(e) = missing.entry(commit) {
            e.insert(!git::has_commit(repo, commit)?);
        }
        if missing[&commit] {
            ours.push(r);
        }
    }
    Ok(ours)
}

/// Whether HEAD, every file of whose tree is `files`, lacks a record of
/// `ours`, or still has one it deleted.
fn any_lost(ours: &[RecordRow], files: &[HeadFile]) -> bool {
    let head = head_tree(files);
    ours.iter().any(|r| {
        let was = (!r.deleted).then_some(&r.json);
        loses(was, head.get(&Place::from(r)).copied())
    })
}

/// Whether rebuilding from `rows`, a worktree's committed view, onto a
/// HEAD whose tree is `files` loses a record a commit the repository
/// doesn't have made.
pub(crate) fn would_lose(
    repo: &gix::Repository,
    rows: Vec<RecordRow>,
    files: &[HeadFile],
) -> Result<bool> {
    Ok(any_lost(&missing_rows(repo, rows)?, files))
}

/// Claim branch `branch` at `base`, or take it as claimed where a recovery
/// that failed before naming it in the database left it there.
fn claim(repo: &gix::Repository, branch: &str, base: CommitId) -> Result<()> {
    match git::create_branch(repo, branch, base) {
        Err(Error::BranchExists { .. }) if git::branch_tip(repo, branch)? == Some(base) => Ok(()),
        claimed => claimed,
    }
}

/// Worktree `w`'s committed view's rows, tombstones included.
pub(crate) async fn chain_rows(db: &Db, w: i64) -> Result<Vec<RecordRow>> {
    on_pool!(db, pool => {
        let mut tx = pool.begin().await?;
        Store::visible(&mut tx, w, Scope::Chain, Filter::All).await
    })
}

/// The newest commit in `new_head`'s first-parent history `known` holds, a
/// worktree's [`chain_commits`]: the one the commits since, which the
/// repository doesn't have, were made on.
pub(crate) fn recovery_base(
    repo: &gix::Repository,
    new_head: CommitId,
    known: &BTreeSet<CommitId>,
) -> Result<Option<CommitId>> {
    let mut at = Some(new_head);
    for _ in 0..NAMED_FAMILY_DEPTH {
        let Some(c) = at else { break };
        if known.contains(&c) {
            return Ok(Some(c));
        }
        at = git::commit_parents(repo, c)?.into_iter().next();
    }
    Ok(None)
}

/// Every commit worktree `w`'s chain has records or a segment at.
pub(crate) async fn chain_commits(db: &Db, w: i64) -> Result<BTreeSet<CommitId>> {
    on_pool!(db, pool => chain_commits_in(pool, w).await)
}

async fn chain_commits_in<DB: Store>(pool: &sqlx::Pool<DB>, w: i64) -> Result<BTreeSet<CommitId>> {
    let mut tx = pool.begin().await?;
    let mut known: BTreeSet<CommitId> = DB::visible(&mut tx, w, Scope::Chain, Filter::All)
        .await?
        .into_iter()
        .filter_map(|r| r.commit_id)
        .collect();
    let head = DB::segs(&mut tx, w).await?.head;
    known.extend(
        DB::segment_chain(&mut tx, head)
            .await?
            .into_iter()
            .filter_map(|s| s.head_commit),
    );
    Ok(known)
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

/// The new head's rows: where the tree at `new_head` differs from `base`'s state,
/// and a tombstone where `w` showed a record another draft still holds.
async fn rebuild_on<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    family: i64,
    w: i64,
    base: Option<i64>,
    new_head: CommitId,
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
    let head = DB::new_head(tx, family, base, new_head, w).await?;
    let rb = Rebuilt {
        family,
        w,
        head,
        new_head,
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
    DB::rechain(tx, family, w, base, head, new_head).await?;
    restamp_files(tx, w, new_head, files).await?;
    segments::follow_records(tx, w).await?;
    DB::drop_stale_entries(tx, w, d).await?;
    segments::relink(tx, w).await?;
    Ok(())
}
