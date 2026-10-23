//! The segment bench's reads through the store's own SQL, against a
//! database `bench/segments/run.sh generate` built, or the SQLite copy
//! `run.sh sqlite` makes of it. Run by hand:
//!
//! ```text
//! cargo test --release --features postgres --lib bench_segments_pg -- --ignored --nocapture
//! cargo test --release --lib bench_segments_sqlite -- --ignored --nocapture
//! ```
//!
//! Each read runs [`WARM`] times, then once more. On Postgres that last run
//! is under `auto_explain`, whose plans come back as notices.
//!
//! The write APIs run last, as they change the database: batches of
//! records of the bench's own made and updated on [`FORK`], then deleted,
//! each with the statements it took, and last main's scan of a merge.
//!
//! With `$BENCH_JSON` set, every row is also appended to that file, for
//! `bench/segments/compare.py`, under the family `$BENCH_FAMILY` names
//! (`20k` by default), after one with the database's size.

use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::{Duration, Instant};

use tracing_subscriber::layer::SubscriberExt;
use tracing_subscriber::util::SubscriberInitExt;
use tracing_subscriber::Layer;

use crate::commit::CommitId;
use crate::crud::{apply_batch_inner, crud_delete_in_pool, crud_upsert_in_pool};
use crate::crud::{WriteTarget, Writer};
use crate::db::store::{Filter, Scope, Store};
use crate::db::tables::{At, Place};
use crate::db::{record, Db};
use crate::format::FormatRegistry;
use crate::model::{
    BatchOp, FacetPath, FacetSpec, JsonQuery, QueryOp, RecordQuery, WorktreeFilter,
};
use crate::segments::{self, Origin, Value};
use crate::Result;

const WARM: usize = 6;
/// The read every row's time is taken in proportion to.
const REFERENCE: &str = "reference: scan every record";
const MAIN: i64 = 1;
/// The first fork, whose draft the writes go in.
const FORK: i64 = 2;

/// Statements sqlx has run, process-wide: SQLite runs them on a worker
/// thread of its own.
static STATEMENTS: AtomicUsize = AtomicUsize::new(0);

struct CountStatements;

impl<S: tracing::Subscriber> Layer<S> for CountStatements {
    fn on_event(&self, e: &tracing::Event<'_>, _: tracing_subscriber::layer::Context<'_, S>) {
        if e.metadata().target() == "sqlx::query" {
            STATEMENTS.fetch_add(1, Ordering::Relaxed);
        }
    }
}

/// Print Postgres's notices, which carry `auto_explain`'s plans, and count
/// statements.
fn subscribe() {
    let notices = tracing_subscriber::fmt::layer()
        .without_time()
        .with_target(false)
        .with_level(false)
        .with_filter(tracing_subscriber::EnvFilter::new(
            "sqlx::postgres::notice=info",
        ));
    let _ = tracing_subscriber::registry()
        .with(notices)
        .with(CountStatements)
        .try_init();
}

/// Append `row`, with its family, to the file `$BENCH_JSON` names, if it
/// names one.
fn report(mut row: serde_json::Value) {
    use std::io::Write;
    let Ok(path) = std::env::var("BENCH_JSON") else {
        return;
    };
    let family = std::env::var("BENCH_FAMILY").unwrap_or_else(|_| "20k".into());
    row["family"] = family.into();
    let mut file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
        .expect("open $BENCH_JSON");
    writeln!(file, "{row}").expect("write $BENCH_JSON");
}

fn median<T: Copy + Ord>(mut values: Vec<T>) -> T {
    values.sort();
    values[values.len() / 2]
}

/// The backend `db` runs on, as sqlx names it.
fn backend(db: &Db) -> &'static str {
    match db {
        Db::Sqlite(_) => <sqlx::Sqlite as sqlx::Database>::NAME,
        #[cfg(feature = "postgres")]
        Db::Postgres(_) => <sqlx::Postgres as sqlx::Database>::NAME,
    }
}

/// [`REFERENCE`]: SQL of no store function's, over every record row, which
/// no index holds.
async fn scan(db: &Db) -> Result<(i64, i64)> {
    const SQL: &str = "SELECT count(*), count(json) FROM record";
    Ok(match db {
        Db::Sqlite(pool) => sqlx::query_as(SQL).fetch_one(pool).await?,
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => sqlx::query_as(SQL).fetch_one(pool).await?,
    })
}

/// Report `db`'s size: its worktrees, segments, record rows and
/// superseded entries.
async fn database(db: &Db) {
    const SQL: &str = "SELECT (SELECT count(*) FROM worktree), (SELECT count(*) FROM segment), \
        (SELECT count(*) FROM record), (SELECT count(*) FROM superseded)";
    let (worktrees, segments, records, superseded): (i64, i64, i64, i64) = match db {
        Db::Sqlite(pool) => sqlx::query_as(SQL).fetch_one(pool).await,
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => sqlx::query_as(SQL).fetch_one(pool).await,
    }
    .expect("the database's size");
    println!(
        "\n==== database: {worktrees} worktrees, {segments} segments, \
         {records} record rows, {superseded} superseded entries"
    );
    report(serde_json::json!({
        "backend": backend(db),
        "kind": "database",
        "worktrees": worktrees,
        "segments": segments,
        "records": records,
        "superseded": superseded,
    }));
}

/// Turn `auto_explain` on or off for the session, when `db` is Postgres.
async fn explain(db: &Db, on: bool) {
    #[cfg(feature = "postgres")]
    if let Db::Postgres(pool) = db {
        use sqlx::Executor;
        let duration = if on { 0 } else { -1 };
        pool.execute(format!("SET auto_explain.log_min_duration = {duration}").as_str())
            .await
            .expect("set auto_explain.log_min_duration");
    }
    let _ = (db, on);
}

/// Run `read` [`WARM`] times, then once more with its plans explained, and
/// print the timings and the statements of one run. It reports the fastest
/// time.
async fn bench<T, F, Fut>(db: &Db, name: &str, read: F)
where
    F: Fn() -> Fut,
    Fut: std::future::Future<Output = Result<T>>,
{
    println!("\n==== {name}");
    let mut times: Vec<Duration> = Vec::with_capacity(WARM);
    let mut statements = Vec::with_capacity(WARM);
    for _ in 0..WARM {
        let before = STATEMENTS.load(Ordering::Relaxed);
        let start = Instant::now();
        read().await.expect(name);
        times.push(start.elapsed());
        statements.push(STATEMENTS.load(Ordering::Relaxed) - before);
    }
    explain(db, true).await;
    let start = Instant::now();
    let explained = read().await;
    let last = start.elapsed();
    explain(db, false).await;
    explained.expect(name);
    let ms: Vec<String> = times
        .iter()
        .map(|t| format!("{:.1}", t.as_secs_f64() * 1e3))
        .collect();
    let statements = median(statements);
    println!(
        "{name}: warm {} ms; last {:.1} ms; {statements} statements",
        ms.join(" "),
        last.as_secs_f64() * 1e3
    );
    report(serde_json::json!({
        "backend": backend(db),
        "kind": "read",
        "name": name,
        "n": 1,
        "statements": statements,
        "ms": times.iter().min().expect("timed").as_secs_f64() * 1e3,
    }));
}

fn tags(tag: &str) -> JsonQuery {
    JsonQuery {
        tokens: vec!["tags".into()],
        value: tag.into(),
        op: QueryOp::Equals,
    }
}

/// Every bench read against `db`, whose versions run up to `cursor` + 1000.
async fn reads(db: &Db, cursor: i64) {
    // a database with fewer versions lists them all
    let cursor = cursor.max(0);
    database(db).await;
    bench(db, REFERENCE, || scan(db)).await;
    bench(db, "1. get one record", || {
        record::get(db, MAIN, "cloudmap.yaml", "/artifacts", "k0000121")
    })
    .await;

    bench(db, "1b. get a key the section lacks", || {
        record::get(db, MAIN, "cloudmap.yaml", "/artifacts", "k0000123")
    })
    .await;

    let by_tag = RecordQuery {
        json_queries: vec![tags("tag7")],
        limit: Some(50),
        ..Default::default()
    };
    bench(db, "2. find with a JSON filter, one page", || {
        record::find(db, MAIN, &by_tag)
    })
    .await;
    let by_tag_next = next_page(db, &by_tag).await;
    bench(db, "8. find with a JSON filter, the next page", || {
        record::find(db, MAIN, &by_tag_next)
    })
    .await;

    let by_type = RecordQuery {
        type_names: Some(vec!["T2".into()]),
        subtypes: true,
        limit: Some(50),
        ..Default::default()
    };
    bench(db, "3. find ?type=T2 and its subtypes, one page", || {
        record::find(db, MAIN, &by_type)
    })
    .await;

    let everything = RecordQuery::default();
    let by_group = FacetSpec {
        group: FacetPath::new(vec!["type".into()], true).expect("facet path"),
        columns: vec![],
    };
    bench(db, "5. facet group counts by type, rolled up", || {
        record::facet(db, MAIN, &everything, &by_group)
    })
    .await;

    let by_column = FacetSpec {
        group: FacetPath::new(vec!["type".into()], true).expect("facet path"),
        columns: vec![vec![
            FacetPath::new(vec!["metadata".into(), "tier".into()], false).expect("facet path"),
            FacetPath::new(vec!["tags".into()], false).expect("facet path"),
        ]],
    };
    bench(db, "6. facet column: type x metadata.tier x tags", || {
        record::facet(db, MAIN, &everything, &by_column)
    })
    .await;

    bench(db, "7. list_changes since a cursor", || {
        record::list_changes(db, MAIN, Some(cursor), false)
    })
    .await;

    let across = RecordQuery {
        worktrees: Some(WorktreeFilter {
            origin: None,
            branch: Some("main".into()),
            ids: None,
        }),
        json_queries: vec![tags("tag7")],
        limit: Some(50),
        ..Default::default()
    };
    bench(
        db,
        "10. find across every branch named main, one page",
        || record::find(db, MAIN, &across),
    )
    .await;

    let mains = RecordQuery {
        worktrees: across.worktrees.clone(),
        ..Default::default()
    };
    bench(
        db,
        "11. facet group counts by type across every main",
        || record::facet(db, MAIN, &mains, &by_group),
    )
    .await;
    bench(db, "12. facet column across every main", || {
        record::facet(db, MAIN, &mains, &by_column)
    })
    .await;
    let across_next = next_page(db, &across).await;
    bench(
        db,
        "13. find across every branch named main, the next page",
        || record::find(db, MAIN, &across_next),
    )
    .await;
}

/// `query`, from where its first page ends.
async fn next_page(db: &Db, query: &RecordQuery) -> RecordQuery {
    let first = record::find(db, MAIN, query).await.expect("a first page");
    let last = first.last().expect("a full first page");
    RecordQuery {
        after: Some(crate::model::Cursor {
            path: last.path.clone(),
            key: last.key.clone(),
            file_path: Some(last.file_path.clone()),
            worktree_id: Some(last.worktree_id),
        }),
        ..query.clone()
    }
}

fn key(i: usize) -> String {
    format!("git://bench.example.com/r{i}.git")
}

fn upserts(keys: std::ops::Range<usize>, name: &str) -> Vec<BatchOp> {
    keys.map(|i| BatchOp::Upsert {
        file_path: Some("cloudmap.yaml".into()),
        path: "/repositories".into(),
        key: key(i),
        json: serde_json::json!({ "name": name }),
        expected: None,
        resolve: false,
    })
    .collect()
}

fn target(key: &str) -> WriteTarget<'_> {
    WriteTarget {
        file_path: Some("cloudmap.yaml"),
        path: "/repositories",
        key,
    }
}

fn deletes(keys: std::ops::Range<usize>) -> Vec<BatchOp> {
    keys.map(|i| BatchOp::Delete {
        file_path: Some("cloudmap.yaml".into()),
        path: "/repositories".into(),
        key: key(i),
        expected: None,
        resolve: false,
    })
    .collect()
}

/// Time `write`, which makes `n` writes on `backend`, and print its
/// statements and time per write.
async fn timed<F: std::future::Future<Output = ()>>(backend: &str, what: &str, n: usize, write: F) {
    let before = STATEMENTS.load(Ordering::Relaxed);
    let start = Instant::now();
    write.await;
    let ms = start.elapsed().as_secs_f64() * 1e3;
    let used = STATEMENTS.load(Ordering::Relaxed) - before;
    let per = |x: f64| x / n as f64;
    println!(
        "| {what} | {n} | {used} | {:.1} | {ms:.0} | {:.2} |",
        per(used as f64),
        per(ms)
    );
    report(serde_json::json!({
        "backend": backend,
        "kind": "write",
        "name": what,
        "n": n,
        "statements": used,
        "ms": per(ms),
    }));
}

/// The write APIs on [`FORK`], against records of the bench's own, which
/// it makes, updates, and deletes again: its draft keeps their tombstones,
/// which the next run's writes replace.
async fn writes<DB: Store>(pool: &sqlx::Pool<DB>) {
    subscribe();
    let formats = FormatRegistry::with_builtins();
    let writer = Writer {
        worktree_id: FORK,
        family_id: MAIN,
        formats: &formats,
    };
    let batch = |ops: Vec<BatchOp>| async move {
        let n = ops.len();
        let outcome = apply_batch_inner(writer, pool, ops, true, false, None)
            .await
            .expect("batch");
        assert_eq!(outcome.applied.len(), n, "{:?}", outcome.failed);
    };
    println!("\n==== writes on worktree {FORK}");
    println!("| write | n | statements | per write | ms | ms per write |");
    println!("|---|---|---|---|---|---|");
    let mut made = 0;
    for n in [10, 100, 1000] {
        timed(
            DB::NAME,
            "batch create",
            n,
            batch(upserts(made..made + n, "made")),
        )
        .await;
        made += n;
    }
    let mut start = 0;
    for n in [10, 100, 1000] {
        timed(
            DB::NAME,
            "batch update",
            n,
            batch(upserts(start..start + n, "edited")),
        )
        .await;
        start += n;
    }
    let json = serde_json::json!({ "name": "again" });
    timed(DB::NAME, "single update", 10, async {
        for i in 0..10 {
            let key = key(i);
            crud_upsert_in_pool(writer, pool, target(&key), json.clone(), None, false)
                .await
                .expect("upsert");
        }
    })
    .await;
    timed(DB::NAME, "single delete", 10, async {
        for i in 0..10 {
            let key = key(i);
            crud_delete_in_pool(writer, pool, target(&key), None, false)
                .await
                .expect("delete");
        }
    })
    .await;
    // cleared after, untimed
    let taken = made..made + 100;
    timed(
        DB::NAME,
        "scan take-in",
        taken.len(),
        take_in(pool, taken.clone()),
    )
    .await;
    batch(deletes(taken)).await;
    timed(DB::NAME, "batch delete", 100, batch(deletes(10..110))).await;
    timed(DB::NAME, "batch delete", 1000, batch(deletes(110..made))).await;
    let places = main_places(pool, 100).await;
    timed(
        DB::NAME,
        "main scan",
        places.len(),
        merge_into_main(pool, places),
    )
    .await;
}

/// `n` places of live records in main's committed cloudmap.yaml.
async fn main_places<DB: Store>(pool: &sqlx::Pool<DB>, n: usize) -> Vec<Place> {
    let mut tx = pool.begin().await.expect("begin");
    let rows = DB::visible(&mut tx, MAIN, Scope::Chain, Filter::File("cloudmap.yaml"))
        .await
        .expect("main's rows");
    rows.iter()
        .filter(|r| !r.deleted)
        .take(n)
        .map(Place::from)
        .collect()
}

/// Main's scan of a merge that changed the records at `places`: each
/// through [`segments::scan_key`], in one transaction.
async fn merge_into_main<DB: Store>(pool: &sqlx::Pool<DB>, places: Vec<Place>) {
    let commit: CommitId = "0123456789abcdef0123456789abcdef01234567"
        .parse()
        .expect("a commit id");
    let mut tx = pool.begin().await.expect("begin");
    let first = DB::next_version(&mut tx, MAIN, places.len() as i64)
        .await
        .expect("versions");
    let json = serde_json::json!({ "name": "merged" });
    for (place, version) in places.iter().zip(first..) {
        let ids = segments::KeyIds {
            named: None,
            moved: None,
            elsewhere: Default::default(),
        };
        segments::scan_key(
            &mut tx,
            MAIN,
            place.as_at(),
            Some(&json),
            commit,
            version,
            ids,
        )
        .await
        .expect("scan a key");
    }
    tx.commit().await.expect("commit");
}

/// The scan's draft side: a write taken in from a file at each of `keys`
/// on [`FORK`], each loading the draft, in one transaction.
async fn take_in<DB: Store>(pool: &sqlx::Pool<DB>, keys: std::ops::Range<usize>) {
    let mut tx = pool.begin().await.expect("begin");
    let first = DB::next_version(&mut tx, MAIN, keys.len() as i64)
        .await
        .expect("versions");
    let json = serde_json::json!({ "name": "taken in" });
    for (i, version) in keys.zip(first..) {
        let key = key(i);
        let at = At {
            file_path: "cloudmap.yaml",
            path: "/repositories",
            key: &key,
        };
        let value = Value::Live(&json);
        segments::write(&mut tx, FORK, at, value, Origin::File(None), None, version)
            .await
            .expect("take in");
    }
    tx.commit().await.expect("commit");
}

/// Write the content hashes [`crate::db::content_hash`] gives the rows
/// `generate.sql` made, as `Store::insert` would have.
#[cfg(feature = "postgres")]
async fn hash_rows_pg(pool: &sqlx::PgPool) {
    let rows: Vec<(i64, serde_json::Value)> =
        sqlx::query_as("SELECT id, json FROM record WHERE content_hash IS NULL")
            .fetch_all(pool)
            .await
            .expect("read the rows to hash");
    let (ids, hashes): (Vec<i64>, Vec<i64>) = rows
        .iter()
        .map(|(id, json)| (*id, crate::db::content_hash(json)))
        .unzip();
    sqlx::query(
        "UPDATE record r SET content_hash = v.h \
         FROM unnest($1::bigint[], $2::bigint[]) AS v(id, h) WHERE r.id = v.id",
    )
    .bind(&ids)
    .bind(&hashes)
    .execute(pool)
    .await
    .expect("write the hashes");
}

/// [`hash_rows_pg`] for the SQLite copy.
async fn hash_rows_sqlite(pool: &sqlx::SqlitePool) {
    let rows: Vec<(i64, String)> =
        sqlx::query_as("SELECT id, json(json) FROM record WHERE content_hash IS NULL")
            .fetch_all(pool)
            .await
            .expect("read the rows to hash");
    let mut tx = pool.begin().await.expect("begin");
    for (id, json) in rows {
        let json: serde_json::Value = serde_json::from_str(&json).expect("a row's JSON");
        sqlx::query("UPDATE record SET content_hash = ?2 WHERE id = ?1")
            .bind(id)
            .bind(crate::db::content_hash(&json))
            .execute(&mut *tx)
            .await
            .expect("write a hash");
    }
    tx.commit().await.expect("commit the hashes");
}

#[cfg(feature = "postgres")]
#[tokio::test]
#[ignore = "needs a database built by bench/segments/run.sh"]
async fn bench_segments_pg() {
    use sqlx::Executor;
    const SETUP: &str = "LOAD 'auto_explain'; \
        SET auto_explain.log_min_duration = -1; \
        SET auto_explain.log_analyze = on; \
        SET auto_explain.log_buffers = on; \
        SET auto_explain.log_settings = on; \
        SET auto_explain.log_level = notice; \
        SET client_min_messages = notice";

    subscribe();
    let url = std::env::var("BENCH_PG_URL")
        .unwrap_or_else(|_| "postgres://postgres:pgjit@127.0.0.1:55432".into());
    let name = std::env::var("BENCH_DB").unwrap_or_else(|_| "unfurl_segments_bench".into());
    // One connection, so the session's auto_explain settings apply to
    // every read. `Db::connect` isn't used because run.sh applied the
    // migrations without sqlx's table.
    let pool = sqlx::postgres::PgPoolOptions::new()
        .max_connections(1)
        .after_connect(|conn, _| {
            Box::pin(async move {
                conn.execute(SETUP).await?;
                Ok(())
            })
        })
        .connect(&format!("{url}/{name}"))
        .await
        .expect("connect to the bench database");
    hash_rows_pg(&pool).await;
    let (cursor,): (i64,) = sqlx::query_as("SELECT max(version) - 1000 FROM record")
        .fetch_one(&pool)
        .await
        .expect("read the cursor");
    reads(&Db::Postgres(pool.clone()), cursor).await;
    writes(&pool).await;
}

#[tokio::test]
#[ignore = "needs the SQLite copy bench/segments/run.sh sqlite makes"]
async fn bench_segments_sqlite() {
    subscribe();
    // to_sqlite.py's default
    let path = std::env::var("BENCH_SQLITE").unwrap_or_else(|_| {
        std::env::temp_dir()
            .join("unfurl_segments_bench.db")
            .display()
            .to_string()
    });
    // `Db::connect` isn't used because to_sqlite.py applied the migrations
    // without sqlx's table.
    let pool = sqlx::sqlite::SqlitePoolOptions::new()
        .max_connections(1)
        .connect(&format!("sqlite://{path}"))
        .await
        .expect("open the SQLite copy");
    hash_rows_sqlite(&pool).await;
    let (cursor,): (i64,) = sqlx::query_as("SELECT max(version) - 1000 FROM record")
        .fetch_one(&pool)
        .await
        .expect("read the cursor");
    reads(&Db::Sqlite(pool.clone()), cursor).await;
    writes(&pool).await;
}
