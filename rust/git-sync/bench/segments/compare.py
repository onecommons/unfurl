"""Compare the segment bench's rows with a baseline.

    python3 compare.py BASELINE RUN.jsonl... [--write-baseline OUT]

Each RUN.jsonl is one run of src/db/bench.rs with $BENCH_JSON set: a JSON
object per row, with its backend, kind ("read" or "write"), name, n, the
statements its n operations took, and ms, the time of one. A row's ratio is
its fastest ms over the runs, over the reference row's fastest on the same
backend, and is compared with the baseline's. A row fails where:

- its statements differ from the baseline's, or between runs;
- its ratio is more than `limit` times the baseline's: the row's own
  `limit`, else the baseline's;
- the baseline has it and the runs don't.

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

Key = tuple[str, str, str, int]
Row = dict[str, object]


def key(row: Row) -> Key:
    return (str(row["backend"]), str(row["kind"]), str(row["name"]), int(row["n"]))


def load(path: str) -> tuple[dict[str, float], dict[Key, Row]]:
    """A run's reference time per backend, and its other rows."""
    lines = pathlib.Path(path).read_text().splitlines()
    rows = [json.loads(line) for line in lines if line.strip()]
    reference = {r["backend"]: r["ms"] for r in rows if r["name"] == REFERENCE}
    out: dict[Key, Row] = {}
    for r in rows:
        if r["name"] == REFERENCE:
            continue
        if r["backend"] not in reference:
            sys.exit(f"{path}: no reference row for {r['backend']}")
        out[key(r)] = r
    return reference, out


def fastest(references: list[dict[str, float]]) -> dict[str, float]:
    """Each backend's fastest reference time over the runs."""
    backends = dict.fromkeys(b for r in references for b in r)
    return {b: min(r[b] for r in references if b in r) for b in backends}


def measure(runs: list[dict[Key, Row]], reference: dict[str, float]) -> dict[Key, Row]:
    """Each row's fastest time over `runs`, its ratio, statements and spread."""
    keys = dict.fromkeys(k for rows in runs for k in rows)
    out: dict[Key, Row] = {}
    for k in keys:
        seen = [rows[k] for rows in runs if k in rows]
        times = [float(r["ms"]) for r in seen]
        out[k] = {
            "statements": sorted({int(r["statements"]) for r in seen}),
            "ms": min(times),
            "ratio": min(times) / reference[k[0]],
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


def report(
    now: dict[Key, Row],
    baseline: dict | None,
    reference: dict[str, float],
    runs: int,
) -> tuple[list[str], int]:
    """The report's lines, and how many rows fail."""
    base_rows = {key(r): r for r in (baseline or {}).get("rows", [])}
    limit = float((baseline or {}).get("limit", LIMIT))
    base_reference = (baseline or {}).get("reference_ms", {})
    keys = list(dict.fromkeys([*now, *base_rows]))
    failures = 0
    lines = []
    if baseline is None:
        lines.append("No baseline yet: reporting only.\n")
    for backend in dict.fromkeys(k[0] for k in keys):
        ref = reference[backend]
        was = base_reference.get(backend)
        lines += [
            f"### {backend}",
            "",
            f"{REFERENCE}: {ref:.2f} ms" + (f", baseline {was:.2f} ms." if was else "."),
            "Ratio is a row's time over the reference's; change is the"
            " ratio's against the baseline's.",
            "",
            "| | row | n | statements | baseline | ms | baseline ms"
            " | ratio | baseline ratio | change | spread | |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for k in (k for k in keys if k[0] == backend):
            row, base = now.get(k), base_rows.get(k)
            why = judge(row, base, limit, runs)
            failures += why is not None
            change = None
            if row and base:
                change = row["ratio"] / base["ratio"] - 1
            status = why or ("ok" if base else "no baseline")
            statements = row and "/".join(map(str, row["statements"]))
            lines.append(
                "| "
                + " | ".join(
                    [
                        k[1],
                        k[2],
                        str(k[3]),
                        cell(statements),
                        cell(base and base["statements"]),
                        cell(row and row["ms"], "{:.3f}"),
                        cell(base and base["ms"], "{:.3f}"),
                        cell(row and row["ratio"], "{:.4f}"),
                        cell(base and base["ratio"], "{:.4f}"),
                        cell(change, "{:+.0%}"),
                        cell(row and row["spread"], "{:.2f}"),
                        ("**FAIL**: " if why else "") + status,
                    ]
                )
                + " |"
            )
        lines.append("")
    verdict = f"{failures} of {len(keys)} rows fail." if failures else "Every row passes."
    lines.append(verdict if baseline is not None else f"{len(keys)} rows, no baseline.")
    return lines, failures


def proposed(now: dict[Key, Row], baseline: dict | None, reference: dict[str, float]) -> dict:
    """These runs' numbers as a baseline, keeping `baseline`'s limits."""
    limits = {key(r): r["limit"] for r in (baseline or {}).get("rows", []) if "limit" in r}
    rows = []
    for k, row in now.items():
        out = {
            "backend": k[0],
            "kind": k[1],
            "name": k[2],
            "n": k[3],
            "statements": row["statements"][-1],
            "ms": round(row["ms"], 4),
            "ratio": round(row["ratio"], 6),
            "spread": round(row["spread"], 3),
        }
        if k in limits:
            out["limit"] = limits[k]
        rows.append(out)
    return {
        "limit": (baseline or {}).get("limit", LIMIT),
        "reference_ms": {b: round(ms, 4) for b, ms in reference.items()},
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("baseline")
    parser.add_argument("runs", nargs="+")
    parser.add_argument("--write-baseline")
    args = parser.parse_args()
    path = pathlib.Path(args.baseline)
    baseline = json.loads(path.read_text()) if path.exists() else None
    loaded = [load(run) for run in args.runs]
    reference = fastest([reference for reference, _ in loaded])
    now = measure([rows for _, rows in loaded], reference)
    lines, failures = report(now, baseline, reference, len(loaded))
    text = "\n".join(lines) + "\n"
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as out:
            out.write("## Segment bench\n\n" + text)
    if args.write_baseline:
        pathlib.Path(args.write_baseline).write_text(
            json.dumps(proposed(now, baseline, reference), indent=2) + "\n"
        )
    return 1 if failures and baseline is not None else 0


if __name__ == "__main__":
    sys.exit(main())
