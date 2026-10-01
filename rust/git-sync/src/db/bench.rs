//! The segment bench's reads through the store's own SQL, against a
//! database `bench/segments/run.sh generate` built, or the SQLite copy
//! `run.sh sqlite` makes of it. Run by hand:
//!
//! ```text
//! cargo test --features postgres --lib bench_segments_pg -- --ignored --nocapture
//! cargo test --lib bench_segments_sqlite -- --ignored --nocapture
//! ```
//!
//! Each read runs [`WARM`] times, then once more. On Postgres that last run
//! is under `auto_explain`, whose plans come back as notices.

use std::time::{Duration, Instant};

use crate::db::{record, Db};
use crate::model::{FacetPath, FacetSpec, JsonQuery, QueryOp, RecordQuery, WorktreeFilter};
use crate::Result;

const WARM: usize = 6;
const MAIN: i64 = 1;

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
/// print the timings.
async fn bench<T, F, Fut>(db: &Db, name: &str, read: F)
where
    F: Fn() -> Fut,
    Fut: std::future::Future<Output = Result<T>>,
{
    println!("\n==== {name}");
    let mut times: Vec<Duration> = Vec::with_capacity(WARM);
    for _ in 0..WARM {
        let start = Instant::now();
        read().await.expect(name);
        times.push(start.elapsed());
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
    println!(
        "{name}: warm {} ms; last {:.1} ms",
        ms.join(" "),
        last.as_secs_f64() * 1e3
    );
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

    let _ = tracing_subscriber::fmt()
        .with_env_filter("sqlx::postgres::notice=info")
        .without_time()
        .with_target(false)
        .with_level(false)
        .try_init();
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
    let (cursor,): (i64,) = sqlx::query_as("SELECT max(version) - 1000 FROM record")
        .fetch_one(&pool)
        .await
        .expect("read the cursor");
    reads(&Db::Postgres(pool), cursor).await;
}

#[tokio::test]
#[ignore = "needs the SQLite copy bench/segments/run.sh sqlite makes"]
async fn bench_segments_sqlite() {
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
    let (cursor,): (i64,) = sqlx::query_as("SELECT max(version) - 1000 FROM record")
        .fetch_one(&pool)
        .await
        .expect("read the cursor");
    reads(&Db::Sqlite(pool), cursor).await;
}
