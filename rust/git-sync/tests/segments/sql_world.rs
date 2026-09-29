// The SQL implementation, driven through `SyncedRepo` in step with the
// in-memory one, over the mirrored repository. Exact agreement on what
// either shows: each worktree's view, its committed chain and its
// conflicts, with `key_id`s compared through a bijection.
//
// Phase 1 of docs/segments-implementation.md: main alone, and the
// operations on one worktree.

/// Whether the SQL side runs `op` yet.
fn sql_supports(op: &Op) -> bool {
    match op {
        Op::Write(..) | Op::Commit(_) | Op::External(..) | Op::Move(..) | Op::Resolve(..) => true,
        // a trailer is a commit's; there's no working-tree form of it
        Op::DiskEdit(_, _, _, wins) => !matches!(wins, FileWins::Diverged),
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
    /// The key a Resolve picks, as the model picks it.
    resolve: Option<Key>,
}

impl Before {
    fn of(world: &World, op: &Op) -> Self {
        let resolve = match *op {
            Op::Resolve(_, false, i, _) => {
                let keys: Vec<Key> = world.model.wts[MAIN].conflicts.keys().copied().collect();
                (!keys.is_empty()).then(|| keys[i as usize % keys.len()])
            }
            _ => None,
        };
        Before {
            commits: world.git.commits.len(),
            next_ver: world.next_ver,
            resolve,
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

/// Postgres when `SEGMENTS_SQL_PG` and `UNFURL_TEST_PG_URL` are set.
fn pg_url() -> Option<String> {
    std::env::var_os("SEGMENTS_SQL_PG").and(std::env::var("UNFURL_TEST_PG_URL").ok())
}

struct SqlWorld {
    rt: tokio::runtime::Runtime,
    main: SyncedRepo,
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
        let (main, raw) = rt.block_on(async {
            let mut formats = FormatRegistry::new();
            formats.register(SegTest);
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
            let main = SyncedRepo::open(&mirror.root, config, formats).await.unwrap();
            main.update_from_working_dir(ScanOptions::default())
                .await
                .unwrap();
            (main, raw)
        });
        SqlWorld {
            rt,
            main,
            raw,
            _db: db,
            ids: BTreeMap::new(),
            back: BTreeMap::new(),
        }
    }

    /// The SQL side of `op`, which `world` has applied and `mirror` is
    /// about to take up: a commit is the implementation's to make.
    fn apply(&mut self, op: &Op, world: &World, mirror: &mut GitMirror, before: &Before) {
        match *op {
            Op::Write(_, k, deleted) => {
                // a write the model made draws a version: its value's
                if world.next_ver == before.next_ver {
                    return;
                }
                let d = &world.model.wts[MAIN].draft[&k];
                let (file, path, key) = place(k);
                self.rt.block_on(async {
                    if deleted {
                        self.main
                            .delete_record(Some(&file), path, &key, None, false)
                            .await
                            .map(|_| ())
                    } else {
                        self.main
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
                    .block_on(self.main.commit_repository("commit"))
                    .unwrap();
                match (made, world.git.commits.len() > before.commits) {
                    (Some(oid), true) => mirror.adopt(&world.git, MAIN, before.commits, &oid),
                    (None, false) => {}
                    (made, _) => {
                        let shown = made
                            .as_deref()
                            .map(|oid| git(&mirror.root, &["show", oid], None))
                            .unwrap_or_default();
                        panic!(
                            "the implementation's commit: {made:?}\n{shown}\nmodel: {:?}",
                            world.model.wts[MAIN].draft.keys().collect::<Vec<_>>()
                        )
                    }
                }
            }
            Op::External(..) | Op::Move(..) => {
                mirror.sync(world);
                self.scan(false);
            }
            Op::DiskEdit(_, _, _, wins) => {
                mirror.sync(world);
                self.scan(matches!(wins, FileWins::Always));
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
                    .block_on(self.main.resolve_conflict(&file, path, &key, resolution, None))
                    .unwrap();
            }
            _ => unreachable!("unsupported ops aren't run"),
        }
    }

    fn scan(&self, force: bool) {
        self.rt
            .block_on(self.main.update_from_working_dir(ScanOptions { force }))
            .unwrap();
    }

    /// Main's view: key → (version, key_id).
    fn view(&self) -> BTreeMap<Key, (Ver, i64)> {
        self.rt
            .block_on(self.main.find_records(&RecordQuery::default()))
            .unwrap()
            .into_iter()
            .map(|r| (key_of(&r.file_path, &r.key), (version_of(&r.json), r.id)))
            .collect()
    }

    /// Main's committed chain, the same way.
    fn chain(&self) -> BTreeMap<Key, (Ver, i64)> {
        let sql = "WITH v AS (SELECT segment_id FROM worktree_segment WHERE worktree_id = ?1) \
             SELECT r.file_path, r.key, json(r.json), r.key_id FROM record r \
             JOIN v ON v.segment_id = r.segment_id \
             WHERE r.conflict IS NULL AND NOT r.deleted \
               AND NOT EXISTS (SELECT 1 FROM superseded x \
                               JOIN v vx ON vx.segment_id = x.segment_id \
                               WHERE x.record_id = r.id)";
        let w = self.main.worktree_id();
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

    /// Main's conflicts: key → (the file's value, resolved).
    fn conflicts(&self) -> BTreeMap<Key, (Option<Ver>, bool)> {
        self.rt
            .block_on(self.main.list_conflicts(None))
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
    fn same_ids(&mut self, what: &str, step: usize, got: &BTreeMap<Key, (Ver, i64)>, want: &BTreeMap<Key, (Ver, Ver)>) {
        let versions = |m: &BTreeMap<Key, (Ver, i64)>| -> BTreeMap<Key, Ver> {
            m.iter().map(|(&k, &(v, _))| (k, v)).collect()
        };
        let want_versions: BTreeMap<Key, Ver> = want.iter().map(|(&k, &(v, _))| (k, v)).collect();
        assert_eq!(versions(got), want_versions, "step {step}: main's {what}");
        for (k, &(_, sql)) in got {
            let imp = want[k].1;
            let a = *self.ids.entry(sql).or_insert(imp);
            let b = *self.back.entry(imp).or_insert(sql);
            assert!(
                a == imp && b == sql,
                "step {step}: main's {what} at key {k}: SQL id {sql} is in-memory {a}, and in-memory {imp} is SQL {b}"
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
        let view = self.view();
        self.same_ids("view", step, &view, &rows(imp.own_view(MAIN)));
        let chain = self.chain();
        self.same_ids("committed chain", step, &chain, &rows(imp.chain_set(MAIN)));
        assert_eq!(
            self.conflicts(),
            imp.conflicts(MAIN),
            "step {step}: main's conflicts"
        );
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
