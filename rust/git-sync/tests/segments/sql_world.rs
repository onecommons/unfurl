// The SQL implementation, driven through `SyncedRepo` in step with the
// in-memory one, over the mirrored repository. Exact agreement on what
// either shows: each worktree's view, its committed chain and its
// conflicts, with `key_id`s compared through a bijection.
//
// Phase 1 of docs/segments-implementation.md, the operations on one
// worktree, and phase 2's so far: forks and rebuilds.

/// Whether the SQL side runs `op` yet.
fn sql_supports(op: &Op) -> bool {
    match op {
        Op::Write(..) | Op::Commit(_) | Op::External(..) | Op::Move(..) | Op::Resolve(..) => true,
        // a trailer is a commit's; there's no working-tree form of it
        Op::DiskEdit(_, _, _, wins) => !matches!(wins, FileWins::Diverged),
        Op::Fork(..) | Op::Rebuild(..) | Op::Rebase(..) | Op::Delete(_) => true,
        Op::Export(_) | Op::FailedExport(_) => true,
        _ => false,
    }
}

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
        Before {
            commits: world.git.commits.len(),
            next_ver: world.next_ver,
            w,
            resolve,
            wts: world.model.wts.len(),
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
                    (DbConfig::Postgres { url }, Raw::Postgres(pool, schema))
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
            Op::Commit(_) => {
                let made = self
                    .rt
                    .block_on(self.repos[&w].commit_repository("commit", Default::default()))
                    .unwrap()
                    .commit;
                match (made, world.git.commits.len() > before.commits) {
                    (Some(oid), true) => {
                        mirror.adopt(&world.git, w, before.commits, &oid);
                        self.check_rollup(world, before.commits, &oid, mirror);
                    }
                    (None, false) => {}
                    (made, _) => {
                        let shown = made
                            .as_deref()
                            .map(|oid| git(&mirror.root, &["show", oid], None))
                            .unwrap_or_default();
                        panic!(
                            "the implementation's commit: {made:?}\n{shown}\nmodel: {:?}",
                            world.model.wts[w].draft.keys().collect::<Vec<_>>()
                        )
                    }
                }
            }
            Op::External(..) | Op::Move(..) | Op::Rebuild(..) | Op::Rebase(..) => {
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
                    assert_eq!(tip, got.commit, "the export's ref");
                }
                false => self.took_export(world, mirror, before, f, &got.commit),
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
        self.took_export(world, mirror, before, f, &got.commit);
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
        };
        let rows = self.rt.block_on(self.repos[&w].worktrees(&filter)).unwrap();
        if folded {
            let commit = rows[0].commit_id.clone().expect("the commit folded");
            self.took_export(world, mirror, before, f, &commit);
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
        let open: Vec<Wt> = self.repos.keys().copied().collect();
        for w in open {
            let view = self.view(w);
            self.same_ids(&format!("worktree {w}'s view"), step, &view, &rows(imp.own_view(w)));
            let chain = self.chain(w);
            self.same_ids(&format!("worktree {w}'s committed chain"), step, &chain, &rows(imp.chain_set(w)));
            assert_eq!(self.conflicts(w), imp.conflicts(w), "step {step}: worktree {w}'s conflicts");
            assert_eq!(
                self.chain_len(w),
                imp.wts[w].chain.len(),
                "step {step}: worktree {w}'s chain length"
            );
        }
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
            assert_eq!(
                self.chain_len(w),
                imp.wts[w].chain.len(),
                "step {step}: worktree {w}'s chain length"
            );
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
