"""Compare the segment bench's rows with a baseline.

    python3 compare.py BASELINE RUN.jsonl... [--write-baseline OUT]

Each RUN.jsonl is one run of src/db/bench.rs with $BENCH_JSON set: a JSON
object per row, with its family (the database generated, "20k" where it
has none), backend, kind ("read" or "write"), name, n, the statements its
n operations took, and ms, the time of one; and first a row of kind
"database" with the database's size. A row's ratio is its fastest ms over
the runs, over the reference row's fastest in the same family and backend,
and is compared with the baseline's. A row fails where:

- its statements differ from the baseline's, or between runs;
- its ratio is more than `limit` times the baseline's: the row's own
  `limit`, else the baseline's;
- the baseline has it and the runs don't.

A family and backend's rows all fail where its database's size differs
from the baseline's, or between runs: their ratios aren't comparable.

Every row is printed, failing or not, beside the baseline's numbers, to
stdout and to $GITHUB_STEP_SUMMARY where that's set. Exits 1 if a row
fails; without a BASELINE file it only reports. --write-baseline writes
these runs' numbers as a baseline, with each row's spread over the runs
(its slowest time over its fastest) and the limits BASELINE had.
"""

import argparse
import json
import os
import pathlib
import sys

REFERENCE = "reference: scan every record"
LIMIT = 2.0
FAMILY = "20k"
SIZE = ("worktrees", "segments", "records", "superseded")

Group = tuple[str, str]
Key = tuple[str, str, str, str, int]
Row = dict[str, object]


def key(row: Row) -> Key:
    family = str(row.get("family", FAMILY))
    return (family, str(row["backend"]), str(row["kind"]), str(row["name"]), int(row["n"]))


def group_of(row: Row) -> Group:
    return (str(row.get("family", FAMILY)), str(row["backend"]))


def label(g: Group) -> str:
    return f"{g[0]}/{g[1]}"


class Run:
    """One run's reference times and database sizes per group, and its rows."""

    def __init__(self, path: str) -> None:
        lines = pathlib.Path(path).read_text().splitlines()
        rows = [json.loads(line) for line in lines if line.strip()]
        self.reference = {group_of(r): float(r["ms"]) for r in rows if r.get("name") == REFERENCE}
        self.size = {
            group_of(r): {k: int(r[k]) for k in SIZE} for r in rows if r["kind"] == "database"
        }
        self.rows: dict[Key, Row] = {}
        for r in rows:
            if r["kind"] == "database" or r["name"] == REFERENCE:
                continue
            if group_of(r) not in self.reference:
                sys.exit(f"{path}: no reference row for {label(group_of(r))}")
            self.rows[key(r)] = r


def fastest(runs: list[Run]) -> dict[Group, float]:
    """Each group's fastest reference time over the runs."""
    groups = dict.fromkeys(g for run in runs for g in run.reference)
    return {g: min(run.reference[g] for run in runs if g in run.reference) for g in groups}


def measure(runs: list[Run], reference: dict[Group, float]) -> dict[Key, Row]:
    """Each row's fastest time over `runs`, its ratio, statements and spread."""
    keys = dict.fromkeys(k for run in runs for k in run.rows)
    out: dict[Key, Row] = {}
    for k in keys:
        seen = [run.rows[k] for run in runs if k in run.rows]
        times = [float(r["ms"]) for r in seen]
        out[k] = {
            "statements": sorted({int(r["statements"]) for r in seen}),
            "ms": min(times),
            "ratio": min(times) / reference[k[:2]],
            "spread": max(times) / min(times),
            "runs": len(seen),
        }
    return out


def judge(now: Row | None, base: Row | None, limit: float, runs: int) -> str | None:
    """Why the row fails, or None."""
    if base is None:
        return None
    if now is None:
        return "missing from the runs"
    statements = now["statements"]
    if len(statements) > 1:
        return f"statements vary between runs: {statements}"
    if now["runs"] < runs:
        return f"in {now['runs']} of {runs} runs"
    if statements[0] > base["statements"]:
        return "more statements"
    if statements[0] < base["statements"]:
        return "fewer statements: update the baseline"
    if now["ratio"] > base["ratio"] * base.get("limit", limit):
        return f"slower than {base.get('limit', limit)}x the baseline"
    return None


def cell(value: object, fmt: str = "{}") -> str:
    return "" if value is None else fmt.format(value)


def size_text(size: dict[str, int]) -> str:
    return ", ".join(f"{size[k]:,} {k}" for k in SIZE)


class Baseline:
    """A baseline file's rows, reference times, database sizes and limit."""

    def __init__(self, data: dict | None) -> None:
        data = data or {}
        self.present = bool(data)
        self.limit = float(data.get("limit", LIMIT))
        self.rows = {key(r): r for r in data.get("rows", [])}

        def group(name: str) -> Group:
            family, _, backend = name.rpartition("/")
            return (family or FAMILY, backend)

        self.reference = {group(n): ms for n, ms in data.get("reference_ms", {}).items()}
        self.size = {group(n): s for n, s in data.get("database", {}).items()}
        self.limits = {key(r): r["limit"] for r in data.get("rows", []) if "limit" in r}


def size_check(runs: list[Run], base: Baseline, g: Group) -> tuple[str, str | None]:
    """The group's database size as a line, and why its rows fail, if they do."""
    sizes = [run.size[g] for run in runs if g in run.size]
    if not sizes:
        return "Database: not reported.", None
    text = f"Database: {size_text(sizes[0])}"
    if any(s != sizes[0] for s in sizes):
        return text + ".", "its database's size varies between runs"
    was = base.size.get(g)
    if was is None:
        return text + "; no baseline size.", None
    if was != sizes[0]:
        why = f"the baseline's database had {size_text(was)}: remake the baseline"
        return text + ".", why
    return text + ", as the baseline's.", None


def section(
    g: Group, runs: list[Run], now: dict[Key, Row], base: Baseline, reference: dict[Group, float]
) -> tuple[list[str], int, int]:
    """One family and backend's lines, its rows and how many of them fail."""
    keys = [k for k in dict.fromkeys([*now, *base.rows]) if k[:2] == g]
    count = sum(1 for run in runs if g in run.reference)
    size_line, size_fails = size_check(runs, base, g)
    was = base.reference.get(g)
    ref = f"{REFERENCE}: {reference[g]:.2f} ms" if g in reference else f"{REFERENCE}: missing"
    lines = [
        f"### {g[0]}: {g[1]}",
        "",
        size_line,
        ref + (f", baseline {was:.2f} ms." if was else "."),
        "A read is timed as the fastest of six calls, a write as n together."
        " Ratio is a row's time over the reference's; change is the ratio's"
        " against the baseline's.",
        "",
        "| | row | n | statements | baseline | ms | baseline ms"
        " | ratio | baseline ratio | change | spread | |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    failures = 0
    for k in keys:
        row, b = now.get(k), base.rows.get(k)
        why = size_fails or judge(row, b, base.limit, count)
        failures += why is not None
        change = row["ratio"] / b["ratio"] - 1 if row and b else None
        status = why or ("ok" if b else "no baseline")
        statements = row and "/".join(map(str, row["statements"]))
        cells = [
            k[2],
            k[3],
            "" if k[2] == "read" else str(k[4]),
            cell(statements),
            cell(b and b["statements"]),
            cell(row and row["ms"], "{:.3f}"),
            cell(b and b["ms"], "{:.3f}"),
            cell(row and row["ratio"], "{:.4f}"),
            cell(b and b["ratio"], "{:.4f}"),
            cell(change, "{:+.0%}"),
            cell(row and row["spread"], "{:.2f}"),
            ("**FAIL**: " if why else "") + status,
        ]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    return lines, len(keys), failures


def report(
    runs: list[Run], now: dict[Key, Row], base: Baseline, reference: dict[Group, float]
) -> tuple[list[str], int]:
    """The report's lines, and how many rows fail."""
    groups = dict.fromkeys([*(k[:2] for k in now), *(k[:2] for k in base.rows)])
    lines = [] if base.present else ["No baseline yet: reporting only.\n"]
    total = failures = 0
    for g in groups:
        more, rows, failed = section(g, runs, now, base, reference)
        lines += more
        total += rows
        failures += failed
    if not base.present:
        lines.append(f"{total} rows, no baseline.")
    elif failures:
        lines.append(f"{failures} of {total} rows fail.")
    else:
        lines.append("Every row passes.")
    return lines, failures


def proposed(
    runs: list[Run], now: dict[Key, Row], base: Baseline, reference: dict[Group, float]
) -> dict:
    """These runs' numbers as a baseline, keeping `base`'s limits."""
    rows = []
    for k, row in now.items():
        out = {
            "family": k[0],
            "backend": k[1],
            "kind": k[2],
            "name": k[3],
            "n": k[4],
            "statements": row["statements"][-1],
            "ms": round(row["ms"], 4),
            "ratio": round(row["ratio"], 6),
            "spread": round(row["spread"], 3),
        }
        if k in base.limits:
            out["limit"] = base.limits[k]
        rows.append(out)
    sizes = {label(g): s for run in runs for g, s in run.size.items()}
    return {
        "limit": base.limit,
        "reference_ms": {label(g): round(ms, 4) for g, ms in reference.items()},
        "database": sizes,
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("baseline")
    parser.add_argument("runs", nargs="+")
    parser.add_argument("--write-baseline")
    args = parser.parse_args()
    path = pathlib.Path(args.baseline)
    base = Baseline(json.loads(path.read_text()) if path.exists() else None)
    runs = [Run(p) for p in args.runs]
    reference = fastest(runs)
    now = measure(runs, reference)
    lines, failures = report(runs, now, base, reference)
    text = "\n".join(lines) + "\n"
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as out:
            out.write("## Segment bench\n\n" + text)
    if args.write_baseline:
        pathlib.Path(args.write_baseline).write_text(
            json.dumps(proposed(runs, now, base, reference), indent=2) + "\n"
        )
    return 1 if failures and base.present else 0


if __name__ == "__main__":
    sys.exit(main())
