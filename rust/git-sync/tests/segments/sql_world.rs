// The SQL implementation, driven through `SyncedRepo` in step with the
// in-memory one, over the mirrored repository. Exact agreement on what
// either shows: each worktree's view, its committed chain and its
// conflicts, with `key_id`s compared through a bijection.
//
// Phase 1 of docs/segments-implementation.md, the operations on one
// worktree, and phase 2's so far: forks and rebuilds.

/// Where model key `k` is: file, section, key.
fn place(k: Key) -> (String, &'static str, String) {
    (file_name(file_of(k)), "/r", record_key(k))
}

fn key_of(file_path: &str, key: &str) -> Key {
    let f: Key = file_path[1..file_path.len() - ".json".len()].parse().unwrap();
    let i: Key = key[1..].parse().unwrap();
    f * KEYS_PER_FILE + i
}

fn value(v: Ver) -> serde_json::Value {
    serde_json::json!({ "v": v })
}

fn version_of(json: &serde_json::Value) -> Ver {
    json["v"].as_u64().expect("a harness record")
}

/// What the SQL side of a step needs from before the model took it.
struct Before {
    commits: usize,
    next_ver: Ver,
    /// The worktree the op acts on, as the model picks it.
    w: Wt,
    /// The key a Resolve picks, as the model picks it.
    resolve: Option<Key>,
    /// How many worktrees the model had: an Export's branch is the next.
    wts: usize,
    /// The version a Resave writes again: what the view shows at its key.
    resave: Option<Ver>,
}

impl Before {
    fn of(world: &World, op: &Op) -> Self {
        let n = match *op {
            Op::Write(n, ..)
            | Op::Commit(n)
            | Op::External(n, _)
            | Op::Move(n, ..)
            | Op::DiskEdit(n, ..)
            | Op::Resolve(n, false, ..)
            | Op::Fork(n, _)
            | Op::Rebuild(n, ..)
            | Op::Rebase(n, ..)
            | Op::Merge(n, ..)
            | Op::Integrate(n, ..)
            | Op::MergeUpstream(n, ..)
            | Op::Resave(n, _)
            | Op::Revert(n, ..)
            | Op::Export(n) => Some(n),
            Op::FailedExport(n) => Some(n / 3),
            _ => None,
        };
        let w = n.and_then(|n| world.pick(n, false)).unwrap_or(MAIN);
        let resolve = match *op {
            Op::Resolve(_, false, i, _) => {
                let keys: Vec<Key> = world.model.wts[w].conflicts.keys().copied().collect();
                (!keys.is_empty()).then(|| keys[i as usize % keys.len()])
            }
            _ => None,
        };
        let resave = match *op {
            Op::Resave(_, k) => world.model.wts[w].view().get(&k).copied(),
            _ => None,
        };
        Before {
            commits: world.git.commits.len(),
            next_ver: world.next_ver,
            w,
            resolve,
            wts: world.model.wts.len(),
            resave,
        }
    }
}

/// The database under the SQL side, for the checks that read it directly.
enum Raw {
    Sqlite(sqlx::SqlitePool),
    /// A schema of its own, dropped with the world.
    #[cfg(feature = "postgres")]
    Postgres(sqlx::PgPool, String),
}

fn formats() -> FormatRegistry {
    let mut formats = FormatRegistry::new();
    formats.register(SegTest);
    formats
}

/// Postgres when `SEGMENTS_SQL_PG` and `UNFURL_TEST_PG_URL` are set.
fn pg_url() -> Option<String> {
    std::env::var_os("SEGMENTS_SQL_PG").and(std::env::var("UNFURL_TEST_PG_URL").ok())
}

struct SqlWorld {
    rt: tokio::runtime::Runtime,
    /// Each open worktree's handle, all on one database.
    repos: BTreeMap<Wt, SyncedRepo>,
    /// Each exported branch's worktree id: it has no checkout to open.
    exported: BTreeMap<Wt, i64>,
    /// Each worktree's unfinished export: the branch's index and name,
    /// and whether only its ref is left to move.
    stuck: BTreeMap<Wt, (Wt, String, bool)>,
    config: DbConfig,
    raw: Raw,
    _db: tempfile::TempDir,
    /// SQL `key_id` ↔ the in-memory implementation's.
    ids: BTreeMap<i64, Ver>,
    back: BTreeMap<Ver, i64>,
}

impl SqlWorld {
    fn new(mirror: &GitMirror) -> Self {
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let db = tempfile::tempdir().unwrap();
        let (main, config, raw) = rt.block_on(async {
            let (config, raw) = match pg_url() {
                #[cfg(feature = "postgres")]
                Some(base) => {
                    let schema = format!("segments_{}", uuid::Uuid::new_v4().simple());
                    let admin = sqlx::PgPool::connect(&base).await.unwrap();
                    sqlx::query(&format!("CREATE SCHEMA \"{schema}\""))
                        .execute(&admin)
                        .await
                        .unwrap();
                    let sep = if base.contains('?') { '&' } else { '?' };
                    let url = format!("{base}{sep}options=-c%20search_path%3D{schema}");
                    let pool = sqlx::PgPool::connect(&url).await.unwrap();
                    let config = DbConfig::Postgres {
                        url,
                        max_connections: None,
                    };
                    (config, Raw::Postgres(pool, schema))
                }
                _ => {
                    let url = format!("sqlite://{}?mode=rwc", db.path().join("db.sqlite").display());
                    let pool = sqlx::SqlitePool::connect(&url).await.unwrap();
                    (DbConfig::Sqlite { url }, Raw::Sqlite(pool))
                }
            };
            let main = SyncedRepo::open(&mirror.root, config.clone(), formats())
                .await
                .unwrap();
            main.update_from_working_dir(ScanOptions::default())
                .await
                .unwrap();
            (main, config, raw)
        });
        SqlWorld {
            rt,
            repos: BTreeMap::from([(MAIN, main)]),
            exported: BTreeMap::new(),
            stuck: BTreeMap::new(),
            config,
            raw,
            _db: db,
            ids: BTreeMap::new(),
            back: BTreeMap::new(),
        }
    }

    /// The SQL side of `op`, which `world` has applied and `mirror` is
    /// about to take up: a commit is the implementation's to make.
    fn apply(&mut self, op: &Op, world: &World, mirror: &mut GitMirror, before: &Before) {
        let w = before.w;
        match *op {
            Op::Write(_, k, deleted) => {
                // a write the model made draws a version: its value's
                if world.next_ver == before.next_ver {
                    return;
                }
                let d = &world.model.wts[w].draft[&k];
                let (file, path, key) = place(k);
                self.rt.block_on(async {
                    if deleted {
                        self.repos[&w]
                            .delete_record(Some(&file), path, &key, None, false)
                            .await
                            .map(|_| ())
                    } else {
                        self.repos[&w]
                            .upsert_record(Some(&file), path, &key, value(d.ver), None, false)
                            .await
                            .map(|_| ())
                    }
                })
                .unwrap();
            }
            Op::Resave(_, k) => {
                let Some(ver) = before.resave else { return };
                let (file, path, key) = place(k);
                self.rt
                    .block_on(self.repos[&w].upsert_record(
                        Some(&file),
                        path,
                        &key,
                        value(ver),
                        None,
                        false,
                    ))
                    .unwrap();
            }
            Op::Commit(_) => {
                let made = self
                    .rt
                    .block_on(self.repos[&w].commit_repository("commit", Default::default()))
                    .unwrap()
                    .commit;
                match (made, world.git.commits.len() > before.commits) {
                    (Some(oid), true) => {
                        let oid = oid.to_string();
                        mirror.adopt(&world.git, w, before.commits, &oid);
                        self.check_rollup(world, before.commits, &oid, mirror);
                    }
                    (None, false) => {}
                    (made, _) => {
                        let shown = made
                            .map(|oid| git(&mirror.root, &["show", &oid.to_string()], None))
                            .unwrap_or_default();
                        panic!(
                            "the implementation's commit: {made:?}\n{shown}\nmodel: {:?}",
                            world.model.wts[w].draft.keys().collect::<Vec<_>>()
                        )
                    }
                }
            }
            Op::External(..)
            | Op::Move(..)
            | Op::Rebuild(..)
            | Op::Rebase(..)
            | Op::Revert(..)
            | Op::Merge(..) => {
                mirror.sync(world);
                self.scan(w, false);
            }
            Op::DiskEdit(_, _, _, wins) => {
                mirror.sync(world);
                self.scan(w, matches!(wins, FileWins::Always));
            }
            Op::Fork(..) => {
                // the new worktree's checkout, opened as the family's
                mirror.sync(world);
                let f = world.model.wts.len() - 1;
                let dir = mirror.checkouts[&f].dir.clone();
                let repo = self
                    .rt
                    .block_on(SyncedRepo::open(&dir, self.config.clone(), formats()))
                    .unwrap();
                self.repos.insert(f, repo);
                self.scan(f, false);
            }
            Op::Resolve(_, _, _, ours) => {
                let Some(k) = before.resolve else { return };
                let (file, path, key) = place(k);
                let resolution = if ours {
                    Resolution::Ours
                } else {
                    Resolution::Theirs
                };
                self.rt
                    .block_on(self.repos[&w].resolve_conflict(&file, path, &key, resolution, None))
                    .unwrap();
            }
            Op::Export(_) | Op::FailedExport(_) => self.export(op, world, mirror, before),
            Op::MergeUpstream(..) => self.merge_upstream(world, mirror, before),
            Op::Integrate(..) => {
                // nothing to rebase onto: a user branch, which SQL doesn't
                // have yet, or nothing below HEAD
                if world.git.commits.len() == before.commits {
                    return;
                }
                mirror.make_commits(world);
                let upstream = mirror.oids.last().expect("upstream").clone();
                let rebased = self
                    .rt
                    .block_on(self.repos[&w].rebase_onto(upstream.parse().expect("a commit")))
                    .unwrap();
                if world.refused {
                    assert!(
                        matches!(rebased, unfurl_git_sync::Rebased::Refused(_)),
                        "the model refused; the implementation's rebase: {rebased:?}"
                    );
                    return;
                }
                assert!(
                    matches!(rebased, unfurl_git_sync::Rebased::Unfolded { .. }),
                    "the implementation's rebase: {rebased:?}"
                );
                mirror.sync(world);
                self.scan(w, false);
            }
            Op::Delete(_) => {
                let gone: Vec<Wt> = self
                    .repos
                    .keys()
                    .copied()
                    .filter(|&w| !world.model.wts[w].alive)
                    .collect();
                for w in gone {
                    let repo = self.repos.remove(&w).expect("open");
                    self.rt.block_on(repo.delete_worktree()).unwrap();
                }
            }
            _ => unreachable!("unsupported ops aren't run"),
        }
    }

    /// C.22 in `before.w`: `git merge-tree` of its HEAD and upstream, the
    /// last commit the model made before its merge, then the
    /// implementation's merge, which the mirror takes as the model's.
    fn merge_upstream(&mut self, world: &World, mirror: &mut GitMirror, before: &Before) {
        let w = before.w;
        let made = world.git.commits.len();
        if made == before.commits {
            return;
        }
        // refused: upstream is the last commit; otherwise the merge is
        let (end, l, u) = match world.refused {
            true => {
                let l = *world.imp.wts[w].history.last().unwrap();
                (made, l, made - 1)
            }
            false => {
                let m = made - 1;
                (m, world.git.parents[m][0], world.git.parents[m][1])
            }
        };
        mirror.make_commits_upto(world, end);
        let (l, u) = (mirror.oids[l].clone(), mirror.oids[u].clone());
        let out = std::process::Command::new("git")
            .args(["merge-tree", "--write-tree", "--name-only", &l, &u])
            .current_dir(&mirror.root)
            .output()
            .expect("git");
        assert!(matches!(out.status.code(), Some(0 | 1)), "merge-tree: {out:?}");
        let text = String::from_utf8(out.stdout).unwrap();
        let mut lines = text.lines();
        let tree = gix::ObjectId::from_hex(lines.next().unwrap().as_bytes()).unwrap();
        let conflicted: Vec<String> = lines
            .take_while(|l| !l.is_empty())
            .map(str::to_string)
            .collect();
        let merged = self
            .rt
            .block_on(self.repos[&w].merge_upstream(u.parse().unwrap(), tree, &conflicted))
            .unwrap();
        match (merged, world.refused) {
            (unfurl_git_sync::MergedUpstream::Refused(_), true) => {}
            (unfurl_git_sync::MergedUpstream::Merged { commit, .. }, false) => {
                // the caller's fast-forward, while HEAD is still `l`
                let dir = mirror.checkouts[&w].dir.clone();
                git(&dir, &["merge", "-q", "--ff-only", &commit.to_string()], None);
                mirror.adopt(&world.git, w, end, &commit.to_string());
                mirror.sync(world);
                self.scan(w, false);
            }
            (merged, refused) => panic!("the model refused: {refused}; the implementation: {merged:?}"),
        }
    }

    /// An export from `before.w`: a new one, one failing after a step,
    /// or the one finishing its export that failed.
    fn export(&mut self, op: &Op, world: &World, mirror: &mut GitMirror, before: &Before) {
        let w = before.w;
        if let Some((f, branch, folded)) = self.stuck.remove(&w) {
            let got = self
                .rt
                .block_on(self.repos[&w].export_conflicts(&branch))
                .unwrap()
                .expect("the export finished");
            match folded {
                // only the ref was left: the commit is the model's already
                true => {
                    assert_eq!(world.git.commits.len(), before.commits, "a commit");
                    let tip = git(&mirror.root, &["rev-parse", &branch], None);
                    assert_eq!(tip, got.commit.to_string(), "the export's ref");
                }
                false => self.took_export(world, mirror, before, f, &got.commit.to_string()),
            }
            return;
        }
        let (f, branch) = (before.wts, format!("export-{}", before.wts));
        let got = match *op {
            Op::FailedExport(n) => match self.fail_export(w, &branch, n % 3) {
                Some(done) => done,
                None => return self.stopped_export(world, mirror, before, branch),
            },
            _ => self
                .rt
                .block_on(self.repos[&w].export_conflicts(&branch))
                .unwrap(),
        };
        assert!(
            !world.stuck.contains_key(&w),
            "the model's export stopped where the implementation's finished: {got:?}"
        );
        if world.model.wts.len() == before.wts {
            assert!(got.is_none(), "the implementation exported: {got:?}");
            return;
        }
        let got = got.expect("an export");
        self.took_export(world, mirror, before, f, &got.commit.to_string());
        assert_eq!(self.exported.insert(f, got.worktree_id), None);
    }

    /// An export from `before.w` to `branch` that stopped: its edits in the
    /// branch's draft, or folded into a commit the ref isn't at.
    fn stopped_export(&mut self, world: &World, mirror: &mut GitMirror, before: &Before, branch: String) {
        let (w, f) = (before.w, before.wts);
        let folded = match world.stuck.get(&w) {
            Some(Unfinished::Moved(_)) => false,
            Some(Unfinished::Folded) => true,
            None => panic!("the implementation's export stopped where the model's finished"),
        };
        let filter = WorktreeFilter {
            origin: None,
            branch: Some(branch.clone()),
            ids: None,
        };
        let rows = self.rt.block_on(self.repos[&w].worktrees(&filter)).unwrap();
        if folded {
            let commit = rows[0].commit_id.expect("the commit folded");
            self.took_export(world, mirror, before, f, &commit.to_string());
        }
        self.exported.insert(f, rows[0].id);
        self.stuck.insert(w, (f, branch, folded));
    }

    /// Export branch `f`'s commit, made in this step if the model made
    /// one: it's the model's, on the base the model forked at.
    fn took_export(&mut self, world: &World, mirror: &mut GitMirror, before: &Before, f: Wt, commit: &str) {
        let history = &world.imp.wts[f].history;
        let base = match world.git.commits.len() > before.commits {
            true => {
                mirror.adopt(&world.git, f, before.commits, commit);
                self.check_rollup(world, before.commits, commit, mirror);
                history[history.len() - 2]
            }
            false => *history.last().unwrap(),
        };
        let parent = format!("{commit}^");
        let at = match commit == mirror.oids[base] {
            true => commit.to_string(),
            false => git(&mirror.root, &["rev-parse", &parent], None),
        };
        assert_eq!(at, mirror.oids[base], "the export's base");
    }

    /// Export `w`'s conflicts to `branch`, failing after step `step` of 3:
    /// `None` when it failed, else what it did, with nothing to move or
    /// no such step to fail after.
    fn fail_export(&mut self, w: Wt, branch: &str, step: u8) -> Option<Option<Exported>> {
        use unfurl_git_sync::export::ExportStep;
        let steps = [ExportStep::Moved, ExportStep::Written, ExportStep::Folded];
        let at = steps[step as usize];
        match self.rt.block_on(self.repos[&w].export_failing(branch, at)) {
            Err(unfurl_git_sync::Error::Other(m)) if m.starts_with("stopped") => None,
            Err(e) => panic!("the failed export: {e}"),
            Ok(done) => Some(done),
        }
    }

    /// The rollup of the implementation's commit `oid`, which is the
    /// model's commit `c`, names the records the model's does, with the
    /// same ids.
    fn check_rollup(&mut self, world: &World, c: usize, oid: &str, mirror: &GitMirror) {
        let message = git(&mirror.root, &["log", "-1", "--format=%B", oid], None);
        let rollup = unfurl_git_sync::parse_commit_rollup(&message)
            .unwrap()
            .expect("a git-sync commit");
        let named: BTreeMap<Key, i64> = rollup
            .txns
            .iter()
            .flat_map(|t| &t.records)
            .chain(&rollup.records)
            .map(|r| {
                let file = r.file_path.as_deref().expect("a file on every record line");
                (key_of(file, &r.key), r.key_id.expect("an id on every record line"))
            })
            .collect();
        let want = world.git.rollups[c].clone().unwrap_or_default();
        assert_eq!(
            named.keys().collect::<Vec<_>>(),
            want.keys().collect::<Vec<_>>(),
            "commit {c}: the records its rollup names\n{message}"
        );
        for (k, &sql) in &named {
            let imp = want[k];
            let a = *self.ids.entry(sql).or_insert(imp);
            let b = *self.back.entry(imp).or_insert(sql);
            assert!(
                a == imp && b == sql,
                "commit {c}: rollup's id at key {k}: SQL id {sql} is in-memory {a}, and in-memory {imp} is SQL {b}\n{message}"
            );
        }
    }

    fn scan(&self, w: Wt, force: bool) {
        self.rt
            .block_on(self.repos[&w].update_from_working_dir(ScanOptions { force, ..Default::default() }))
            .unwrap();
    }

    /// Worktree `w`'s view: key → (version, key_id).
    fn view(&self, w: Wt) -> BTreeMap<Key, (Ver, i64)> {
        self.rt
            .block_on(self.repos[&w].find_records(&RecordQuery::default()))
            .unwrap()
            .into_iter()
            .map(|r| (key_of(&r.file_path, &r.key), (version_of(&r.json), r.id)))
            .collect()
    }

    /// How many segments worktree `w`'s committed chain holds: what
    /// compaction shortens.
    fn chain_len(&self, w: Wt) -> usize {
        let sql = "SELECT count(*) FROM worktree_segment WHERE worktree_id = ?1";
        let w = self.worktree_id(w);
        let n: i64 = match &self.raw {
            Raw::Sqlite(pool) => self
                .rt
                .block_on(sqlx::query_scalar(sql).bind(w).fetch_one(pool))
                .unwrap(),
            #[cfg(feature = "postgres")]
            Raw::Postgres(pool, _) => self
                .rt
                .block_on(sqlx::query_scalar(&sql.replace("?1", "$1")).bind(w).fetch_one(pool))
                .unwrap(),
        };
        usize::try_from(n).expect("a count")
    }

    /// Worktree `w`'s committed chain, the same way.
    fn chain(&self, w: Wt) -> BTreeMap<Key, (Ver, i64)> {
        let sql = "WITH v AS (SELECT segment_id FROM worktree_segment WHERE worktree_id = ?1) \
             SELECT r.file_path, r.key, json(r.json), r.key_id FROM record r \
             JOIN v ON v.segment_id = r.segment_id \
             WHERE r.conflict IS NULL AND NOT r.deleted \
               AND NOT EXISTS (SELECT 1 FROM superseded x \
                               JOIN v vx ON vx.segment_id = x.segment_id \
                               WHERE x.record_id = r.id)";
        let w = self.worktree_id(w);
        let rows: Vec<(String, String, String, i64)> = match &self.raw {
            Raw::Sqlite(pool) => self
                .rt
                .block_on(sqlx::query_as(sql).bind(w).fetch_all(pool))
                .unwrap(),
            #[cfg(feature = "postgres")]
            Raw::Postgres(pool, _) => {
                let sql = sql.replace("json(r.json)", "r.json::text").replace("?1", "$1");
                self.rt
                    .block_on(sqlx::query_as(&sql).bind(w).fetch_all(pool))
                    .unwrap()
            }
        };
        rows.into_iter()
            .map(|(file, key, json, id)| {
                let json: serde_json::Value = serde_json::from_str(&json).unwrap();
                (key_of(&file, &key), (version_of(&json), id))
            })
            .collect()
    }

    /// Worktree `w`'s id in the database: its handle's, or an exported
    /// branch's.
    fn worktree_id(&self, w: Wt) -> i64 {
        match self.repos.get(&w) {
            Some(repo) => repo.worktree_id(),
            None => self.exported[&w],
        }
    }

    /// Worktree `w`'s conflicts: key → (the file's value, resolved).
    fn conflicts(&self, w: Wt) -> BTreeMap<Key, (Option<Ver>, bool)> {
        self.rt
            .block_on(self.repos[&w].list_conflicts(None))
            .unwrap()
            .into_iter()
            .map(|r| {
                let theirs = (!r.deleted).then(|| version_of(&r.json));
                let resolved = r.conflict == Some(ConflictState::Resolved);
                (key_of(&r.file_path, &r.key), (theirs, resolved))
            })
            .collect()
    }

    /// Compare `got`'s ids with the in-memory ones, extending the
    /// bijection with pairs not seen before.
    fn same_ids(
        &mut self,
        what: &str,
        step: usize,
        got: &BTreeMap<Key, (Ver, i64)>,
        want: &BTreeMap<Key, (Ver, Ver)>,
    ) {
        let versions = |m: &BTreeMap<Key, (Ver, i64)>| -> BTreeMap<Key, Ver> {
            m.iter().map(|(&k, &(v, _))| (k, v)).collect()
        };
        let want_versions: BTreeMap<Key, Ver> = want.iter().map(|(&k, &(v, _))| (k, v)).collect();
        assert_eq!(versions(got), want_versions, "step {step}: {what}");
        for (k, &(_, sql)) in got {
            let imp = want[k].1;
            let a = *self.ids.entry(sql).or_insert(imp);
            let b = *self.back.entry(imp).or_insert(sql);
            assert!(
                a == imp && b == sql,
                "step {step}: {what} at key {k}: SQL id {sql} is in-memory {a}, and in-memory {imp} is SQL {b}"
            );
        }
    }

    /// The live rows worktree `w`'s draft supersedes, wherever they are,
    /// as `(key, version, segment)`, a segment named by its kind and owner,
    /// as ids differ between the sides. Not tombstones: a deletion the
    /// model writes as a tombstone, SQL may write as an entry alone, with
    /// nothing to hide.
    fn draft_entries(&self, w: Wt) -> BTreeSet<(Key, Ver, &'static str, Option<Wt>)> {
        let sql = "SELECT r.file_path, r.key, json(r.json), r.deleted, s.kind, s.owner_id \
             FROM superseded x JOIN record r ON r.id = x.record_id \
             JOIN segment s ON s.id = r.segment_id \
             WHERE x.segment_id = (SELECT draft_segment_id FROM worktree WHERE id = ?1)";
        let owners: BTreeMap<i64, Wt> = self
            .repos
            .keys()
            .chain(self.exported.keys())
            .map(|&u| (self.worktree_id(u), u))
            .collect();
        let w = self.worktree_id(w);
        type Row = (String, String, String, bool, String, Option<i64>);
        let rows: Vec<Row> = match &self.raw {
            Raw::Sqlite(pool) => self
                .rt
                .block_on(sqlx::query_as(sql).bind(w).fetch_all(pool))
                .unwrap(),
            #[cfg(feature = "postgres")]
            Raw::Postgres(pool, _) => self
                .rt
                .block_on(
                    sqlx::query_as(&sql.replace("json(r.json)", "r.json::text").replace("?1", "$1"))
                        .bind(w)
                        .fetch_all(pool),
                )
                .unwrap(),
        };
        rows.into_iter()
            .filter(|(_, _, _, deleted, ..)| !deleted)
            .map(|(file, key, json, _, kind, owner)| {
                let json: serde_json::Value = serde_json::from_str(&json).unwrap();
                let kind = match kind.as_str() {
                    "head" => "head",
                    "draft" => "draft",
                    _ => "internal",
                };
                let owner = owner.map(|o| owners[&o]);
                (key_of(&file, &key), version_of(&json), kind, owner)
            })
            .collect()
    }

    /// A read across every open worktree, SQL's against the in-memory
    /// implementation's (C.7), as live `(key, version)`s: whole, a page of
    /// two at a time, and as a facet's total.
    fn check_across(&self, world: &World, step: usize) {
        let open: Vec<Wt> = self.repos.keys().copied().collect();
        if open.len() < 2 {
            return;
        }
        let ids: Vec<i64> = open.iter().map(|&w| self.worktree_id(w)).collect();
        let query = RecordQuery {
            worktrees: Some(unfurl_git_sync::WorktreeFilter {
                ids: Some(ids),
                ..Default::default()
            }),
            ..Default::default()
        };
        let repo = &self.repos[&open[0]];
        let wt_of: BTreeMap<i64, Wt> = open.iter().map(|&w| (self.worktree_id(w), w)).collect();
        let live = |records: Vec<Record>| -> BTreeSet<(Wt, Key, Ver)> {
            records
                .into_iter()
                .filter(|r| !r.deleted)
                .map(|r| (wt_of[&r.worktree_id], key_of(&r.file_path, &r.key), version_of(&r.json)))
                .collect()
        };
        let imp = &world.imp;
        let expected: BTreeSet<(Wt, Key, Ver)> = imp
            .read_across_by(&open)
            .into_iter()
            .map(|(w, r)| (w, &imp.rows[&r]))
            .filter(|(_, r)| !r.deleted)
            .map(|(w, r)| (w, r.key, r.ver))
            .collect();
        // SQL finds a moved version by its `key_id`, the in-memory
        // implementation by content: a copy carrying its own id since a
        // fast-forward or a fork (§3.5) is a rename only the latter sees.
        // So, for a worktree showing version `v` at `k` as record `id`, and
        // another: every merge base lacks `v` at `k`, and one has it at
        // another key as another record.
        let moved_from_another = |w: Wt, u: Wt, k: Key, v: Ver, id: Ver| {
            let views: Vec<BTreeSet<SegId>> = imp
                .merge_bases(w, u)
                .into_iter()
                .map(|s| {
                    let mut view = imp.ancestors(s);
                    view.insert(s);
                    view
                })
                .collect();
            !views.is_empty()
                && views.iter().all(|t| imp.live(t, k) != Some(v))
                && views.iter().any(|t| {
                    (0..KEYS).any(|j| j != k && imp.live(t, j) == Some(v) && imp.live_id(t, j) != Some(id))
                })
        };
        let renamed_copy = |&(w, k, v): &(Wt, Key, Ver)| {
            imp.visible(&imp.own_view(w), Some(k))
                .into_iter()
                .map(|r| &imp.rows[&r])
                .any(|a| {
                    a.ver == v && !a.deleted && open.iter().any(|&u| u != w && moved_from_another(w, u, k, v, a.id))
                })
        };
        let spec = FacetSpec {
            group: FacetPath::new(vec!["v".to_string()], false).unwrap(),
            columns: vec![],
        };
        // the read whole, paged and as a facet's total, `how` it reads rows
        let reads = |how: &str| {
            let sql = live(self.rt.block_on(repo.find_records(&query)).unwrap());
            let extra: Vec<&(Wt, Key, Ver)> = sql.difference(&expected).collect();
            allowed(8, extra.len());
            assert!(
                expected.is_subset(&sql) && extra.iter().all(|e| renamed_copy(e)),
                "step {step}: a read across {open:?}, {how}\n  sql: {sql:?}\n want: {expected:?}"
            );
            // read as a client does: a page shorter than its limit is the last
            let mut paged = Vec::new();
            let mut after: Option<Cursor> = None;
            loop {
                let page = RecordQuery {
                    limit: Some(2),
                    whole_groups: true,
                    after: after.clone(),
                    ..query.clone()
                };
                let rows = self.rt.block_on(repo.find_records(&page)).unwrap();
                let short = rows.len() < 2;
                after = rows.last().map(|last| Cursor {
                    path: last.path.clone(),
                    key: last.key.clone(),
                    file_path: Some(last.file_path.clone()),
                    worktree_id: Some(last.worktree_id),
                });
                paged.extend(rows);
                if short {
                    break;
                }
            }
            assert_eq!(live(paged), sql, "step {step}: a read across {open:?}, paged, {how}");
            let facets = self.rt.block_on(repo.facet_records(&query, &spec)).unwrap();
            // a facet counts a (path, key), which a key in each file shares
            let keys: BTreeSet<Key> = sql.iter().map(|&(_, k, _)| k % KEYS_PER_FILE).collect();
            assert_eq!(
                facets.total,
                keys.len() as i64,
                "step {step}: a facet across {open:?}, {how}"
            );
        };
        reads("by place");
        unfurl_git_sync::reading_every_row(|| reads("reading every row"));
    }

    /// Rows of a query with no parameters, from either database.
    fn raw_all<T>(&self, sql: &str) -> Vec<T>
    where
        T: for<'r> sqlx::FromRow<'r, sqlx::sqlite::SqliteRow> + Send + Unpin,
        T: RawPg,
    {
        match &self.raw {
            Raw::Sqlite(pool) => self.rt.block_on(sqlx::query_as(sql).fetch_all(pool)).unwrap(),
            #[cfg(feature = "postgres")]
            Raw::Postgres(pool, _) => self.rt.block_on(sqlx::query_as(sql).fetch_all(pool)).unwrap(),
        }
    }

    /// Each two open worktrees' merge bases, as SQL's segments have them
    /// (the latest their histories share, through parents and merges),
    /// against the in-memory implementation's, as commits.
    fn check_merge_bases(&self, world: &World, mirror: &GitMirror, step: usize) {
        let open: Vec<Wt> = self.repos.keys().copied().collect();
        if open.len() < 2 || std::env::var_os("SEGMENTS_VISIBLE_ONLY").is_some() {
            return;
        }
        let segs: Vec<(i64, Option<i64>, Option<String>)> =
            self.raw_all("SELECT id, parent_id, head_commit FROM segment WHERE kind <> 'draft'");
        let merges: Vec<(i64, i64)> = self.raw_all("SELECT segment_id, merged_segment_id FROM segment_merge");
        let heads: BTreeMap<i64, i64> = self
            .raw_all::<(i64, Option<i64>)>("SELECT id, head_segment_id FROM worktree")
            .into_iter()
            .filter_map(|(w, h)| h.map(|h| (w, h)))
            .collect();
        let below: BTreeMap<i64, Vec<i64>> = segs
            .iter()
            .filter_map(|&(id, parent, _)| parent.map(|p| (id, p)))
            .chain(merges.iter().copied())
            .fold(BTreeMap::new(), |mut m, (from, to)| {
                m.entry(from).or_insert_with(Vec::new).push(to);
                m
            });
        let ends: BTreeMap<i64, &str> = segs.iter().filter_map(|(id, _, c)| c.as_deref().map(|c| (*id, c))).collect();
        let history = |s: i64| {
            let mut out = BTreeSet::from([s]);
            let mut todo = vec![s];
            while let Some(x) = todo.pop() {
                for &y in below.get(&x).into_iter().flatten() {
                    if out.insert(y) {
                        todo.push(y);
                    }
                }
            }
            out
        };
        let bases = |a: BTreeSet<i64>, b: BTreeSet<i64>| -> BTreeSet<String> {
            let common: BTreeSet<i64> = a.intersection(&b).copied().collect();
            common
                .iter()
                .copied()
                .filter(|&s| !common.iter().any(|&x| x != s && history(x).contains(&s)))
                .map(|s| ends[&s].to_string())
                .collect()
        };
        let imp = &world.imp;
        for (i, &w) in open.iter().enumerate() {
            for &u in &open[i + 1..] {
                let sql = bases(history(heads[&self.worktree_id(w)]), history(heads[&self.worktree_id(u)]));
                let want: BTreeSet<String> = imp
                    .merge_bases(w, u)
                    .into_iter()
                    .map(|s| mirror.oids[imp.segs[s].head_commit.unwrap()].clone())
                    .collect();
                assert_eq!(sql, want, "step {step}: worktrees {w} and {u}'s merge bases");
            }
        }
    }

    fn check(&mut self, world: &World, step: usize) {
        let imp = &world.imp;
        let rows = |view: BTreeSet<SegId>| -> BTreeMap<Key, (Ver, Ver)> {
            imp.visible(&view, None)
                .into_iter()
                .map(|r| &imp.rows[&r])
                .filter(|r| !r.deleted)
                .map(|r| (r.key, (r.ver, r.id)))
                .collect()
        };
        // `SEGMENTS_VISIBLE_ONLY` leaves out the shape checks, chain length
        // and draft entries, which no read sees: proptest then shrinks a
        // history onto a difference a user could see
        let shape = std::env::var_os("SEGMENTS_VISIBLE_ONLY").is_none();
        let open: Vec<Wt> = self.repos.keys().copied().collect();
        for w in open {
            let view = self.view(w);
            self.same_ids(&format!("worktree {w}'s view"), step, &view, &rows(imp.own_view(w)));
            let chain = self.chain(w);
            self.same_ids(&format!("worktree {w}'s committed chain"), step, &chain, &rows(imp.chain_set(w)));
            assert_eq!(self.conflicts(w), imp.conflicts(w), "step {step}: worktree {w}'s conflicts");
            if !shape {
                continue;
            }
            assert_eq!(
                self.chain_len(w),
                imp.wts[w].chain.len(),
                "step {step}: worktree {w}'s chain length"
            );
            // what its draft hides, its own rows and other worktrees'
            let d = imp.wts[w].draft;
            let want: BTreeSet<(Key, Ver, &'static str, Option<Wt>)> = imp
                .sup
                .iter()
                .filter(|&&(_, s)| s == d)
                .map(|(r, _)| &imp.rows[r])
                .filter(|r| !r.deleted)
                .map(|r| {
                    let seg = &imp.segs[r.seg];
                    let kind = match seg.kind {
                        Kind::Head => "head",
                        Kind::Draft => "draft",
                        Kind::Internal => "internal",
                    };
                    (r.key, r.ver, kind, seg.owner)
                })
                .collect();
            assert_eq!(self.draft_entries(w), want, "step {step}: worktree {w}'s draft entries");
        }
        self.check_across(world, step);
        // an exported branch: its chain is its view, unless its export
        // stopped with the edits in its draft, and nothing conflicts
        let exported: Vec<Wt> = self.exported.keys().copied().collect();
        let stuck: BTreeSet<Wt> = self
            .stuck
            .values()
            .filter(|&&(_, _, folded)| !folded)
            .map(|&(f, _, _)| f)
            .collect();
        for w in exported {
            assert!(imp.conflicts(w).is_empty(), "step {step}: worktree {w}'s conflicts");
            let chain = self.chain(w);
            self.same_ids(&format!("worktree {w}'s committed chain"), step, &chain, &rows(imp.chain_set(w)));
            if !stuck.contains(&w) {
                self.same_ids(&format!("worktree {w}'s view"), step, &chain, &rows(imp.own_view(w)));
            }
            if shape {
                assert_eq!(
                    self.chain_len(w),
                    imp.wts[w].chain.len(),
                    "step {step}: worktree {w}'s chain length"
                );
            }
        }
    }
}

impl Drop for SqlWorld {
    fn drop(&mut self) {
        #[cfg(feature = "postgres")]
        if let (Raw::Postgres(pool, schema), Some(base)) = (&self.raw, pg_url()) {
            self.rt.block_on(async {
                pool.close().await;
                let admin = sqlx::PgPool::connect(&base).await.unwrap();
                sqlx::query(&format!("DROP SCHEMA \"{schema}\" CASCADE"))
                    .execute(&admin)
                    .await
                    .unwrap();
            });
        }
    }
}

/// A row Postgres can give [`SqlWorld::raw_all`] too.
#[cfg(feature = "postgres")]
trait RawPg: for<'r> sqlx::FromRow<'r, sqlx::postgres::PgRow> {}

#[cfg(feature = "postgres")]
impl<T: for<'r> sqlx::FromRow<'r, sqlx::postgres::PgRow>> RawPg for T {}

#[cfg(not(feature = "postgres"))]
trait RawPg {}

#[cfg(not(feature = "postgres"))]
impl<T> RawPg for T {}
