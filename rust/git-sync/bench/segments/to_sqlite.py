"""Copy the bench database run.sh generated into a SQLite file.

    python3 to_sqlite.py [OUT.db]

The file gets migrations/sqlite and then every table's rows, so the same
data can be read through the store's SQLite path. OUT.db defaults to
$BENCH_SQLITE, else unfurl_segments_bench.db in the temp directory, where
src/db/bench.rs looks for it. BENCH_PG_URL and BENCH_DB name the source, as
in run.sh. Needs Python's sqlite3 at 3.45 or later, for jsonb().
"""

import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
MIGRATIONS = HERE.parent.parent / "migrations" / "sqlite"
JSONB = {"record": {"json", "base_json", "settled"}}
TABLES = [
    "version_seq",
    "worktree",
    "segment",
    "worktree_segment",
    "file",
    "record",
    "superseded",
    "alias",
    "txn",
]


def rows(url: str, table: str) -> list[dict[str, object]]:
    """Every row of `table`, as JSON objects."""
    out = subprocess.run(
        ["psql", url, "-XAtq", "-c", f"SELECT row_to_json(t) FROM {table} t"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [json.loads(line) for line in out.splitlines()]


def column_value(table: str, column: str, value: object) -> object:
    """A Postgres value as the SQLite schema stores it."""
    if value is None:
        return None
    if column in JSONB.get(table, ()):
        return json.dumps(value)
    if isinstance(value, bool):
        return int(value)
    return value


def copy_table(url: str, db: sqlite3.Connection, table: str) -> int:
    """Insert every row of `table`; returns how many."""
    source = rows(url, table)
    if not source:
        return 0
    columns = list(source[0])
    jsonb = JSONB.get(table, set())
    marks = ", ".join("jsonb(?)" if c in jsonb else "?" for c in columns)
    db.executemany(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({marks})",
        ([column_value(table, c, r[c]) for c in columns] for r in source),
    )
    return len(source)


def main(out: str) -> None:
    """Build `out` from the migrations and the Postgres bench database."""
    if sqlite3.sqlite_version_info < (3, 45):
        sys.exit(f"sqlite {sqlite3.sqlite_version} has no jsonb(); need 3.45")
    url = os.environ.get("BENCH_PG_URL", "postgres://postgres:pgjit@127.0.0.1:55432")
    url = f"{url}/{os.environ.get('BENCH_DB', 'unfurl_segments_bench')}"
    check = subprocess.run(["psql", url, "-XAtqc", "SELECT 1"], capture_output=True, text=True)
    if check.returncode:
        sys.exit(f"{check.stderr.strip()}\nrun `run.sh generate` first")
    # Built aside and renamed, so a failed copy leaves the previous one.
    building = pathlib.Path(f"{out}.building")
    building.unlink(missing_ok=True)
    db = sqlite3.connect(building)
    for migration in sorted(MIGRATIONS.glob("*.sql")):
        db.executescript(migration.read_text())
    for table in TABLES:
        print(f"{table}: {copy_table(url, db, table)}")
    db.commit()
    db.execute("ANALYZE")
    db.close()
    os.replace(building, out)
    print(f"wrote {out}")


def default_path() -> str:
    """Where the copy goes when no path is given."""
    return os.environ.get("BENCH_SQLITE") or str(
        pathlib.Path(tempfile.gettempdir()) / "unfurl_segments_bench.db"
    )


if __name__ == "__main__":
    if len(sys.argv) > 2:
        sys.exit(__doc__)
    main(sys.argv[1] if len(sys.argv) == 2 else default_path())
