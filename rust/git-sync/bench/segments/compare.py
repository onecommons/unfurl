"""Compare the segment bench at HEAD with its baseline commit's, run alike.

    python3 compare.py LIMITS --base RUN.jsonl... --head RUN.jsonl... [--cpu FILE]

Each RUN.jsonl is one run of src/db/bench.rs with $BENCH_JSON set: a JSON
object per row, with its family (the database generated, "20k" where it
has none), backend, kind ("read", "write" or "database"), name, n, the
statements its n operations took, and ms, the time of one. The base runs
are the baseline commit's (refs/bench/git-sync), made on the same machine
in the same job as HEAD's, so a row's ratio, its fastest time at HEAD
over its fastest at the base, doesn't depend on the machine. A row fails
where:

- its ratio is more than its limit in LIMITS, else LIMITS' default;
- it takes more statements at HEAD than at the base, or either side's
  count varies between runs;
- the base has it and HEAD doesn't.

A family and backend's rows all fail where its database's size differs
between the two sides, or between runs: they didn't run on the same data.

Every row is printed, failing or not, to stdout and to
$GITHUB_STEP_SUMMARY where that's set, after the machine's CPU from
--cpu (`lscpu`'s output). Exits 1 if a row fails.
"""

import argparse
import json
import os
import pathlib
import sys

REFERENCE = "reference: scan every record"
FAMILY = "20k"
SIZE = ("worktrees", "segments", "records", "superseded")

Group = tuple[str, str]
Key = tuple[str, str, str, str, int]
Row = dict[str, object]


def key(row: Row) -> Key:
    family = str(row.get("family", FAMILY))
    return (family, str(row["backend"]), str(row["kind"]), str(row["name"]), int(row["n"]))


def label(k: Key) -> str:
    return "/".join(map(str, k[:4]))


class Side:
    """One side's runs: each row's times and statements, each group's sizes."""

    def __init__(self, paths: list[str]) -> None:
        self.runs = len(paths)
        self.times: dict[Key, list[float]] = {}
        self.statements: dict[Key, set[int]] = {}
        self.sizes: dict[Group, list[dict[str, int]]] = {}
        for path in paths:
            for line in pathlib.Path(path).read_text().splitlines():
                if not line.strip():
                    continue
                r = json.loads(line)
                group = (str(r.get("family", FAMILY)), str(r["backend"]))
                if r["kind"] == "database":
                    self.sizes.setdefault(group, []).append({k: int(r[k]) for k in SIZE})
                    continue
                k = key(r)
                self.times.setdefault(k, []).append(float(r["ms"]))
                self.statements.setdefault(k, set()).add(int(r["statements"]))

    def size(self, g: Group) -> tuple[dict[str, int] | None, bool]:
        """The group's database size, and whether it varied between runs."""
        sizes = self.sizes.get(g, [])
        return (sizes[0] if sizes else None), any(s != sizes[0] for s in sizes)


def size_text(size: dict[str, int] | None) -> str:
    return "not reported" if size is None else ", ".join(f"{size[k]:,} {k}" for k in SIZE)


def size_check(base: Side, head: Side, g: Group) -> tuple[str, str | None]:
    """The group's database line, and why all its rows fail, if they do."""
    (b, b_varies), (h, h_varies) = base.size(g), head.size(g)
    line = f"Database: {size_text(h)}"
    if b_varies or h_varies:
        return line + ".", "its database's size varies between runs"
    if b != h:
        return line + f"; the base's: {size_text(b)}.", "the base ran on another database"
    return line + ", the same at the base.", None


def judge(k: Key, base: Side, head: Side, limit: float) -> str | None:
    """Why the row fails, or None."""
    if k not in head.times:
        return "gone from HEAD: move refs/bench/git-sync if that's meant"
    if k not in base.times:
        return None
    b, h = base.statements[k], head.statements[k]
    if len(b) > 1 or len(h) > 1:
        return f"statements vary between runs: base {sorted(b)}, HEAD {sorted(h)}"
    if min(h) > min(b):
        return "more statements than the base"
    if min(head.times[k]) / min(base.times[k]) > limit:
        return f"slower than {limit}x the base"
    return None


def cell(value: object, fmt: str = "{}") -> str:
    return "" if value is None else fmt.format(value)


def spread(times: list[float] | None) -> str | None:
    return None if not times else f"{max(times) / min(times):.2f}"


def row_line(k: Key, base: Side, head: Side, limit: float, why: str | None) -> str:
    bt, ht = base.times.get(k), head.times.get(k)
    ratio = min(ht) / min(bt) if bt and ht else None
    bs, hs = base.statements.get(k), head.statements.get(k)
    if why:
        status = f"**FAIL**: {why}"
    elif bt is None:
        status = "new at HEAD"
    elif hs and bs and min(hs) < min(bs):
        status = "ok, fewer statements"
    else:
        status = "ok"
    cells = [
        k[2],
        k[3],
        "" if k[2] == "read" else str(k[4]),
        cell(bs and "/".join(map(str, sorted(bs)))),
        cell(hs and "/".join(map(str, sorted(hs)))),
        cell(bt and min(bt), "{:.3f}"),
        cell(ht and min(ht), "{:.3f}"),
        cell(ratio, "{:.2f}"),
        f"{limit:g}",
        cell(spread(bt)),
        cell(spread(ht)),
        status,
    ]
    return "| " + " | ".join(cells) + " |"


def section(g: Group, base: Side, head: Side, limits: dict) -> tuple[list[str], int, int]:
    """One family and backend's lines, its rows and how many fail."""
    keys = [k for k in dict.fromkeys([*head.times, *base.times]) if k[:2] == g]
    size_line, size_fails = size_check(base, head, g)
    ref = (g[0], g[1], "read", REFERENCE, 1)
    lines = [f"### {g[0]}: {g[1]}", "", size_line]
    if ref in base.times and ref in head.times:
        lines.append(
            f"{REFERENCE}, ms: base {min(base.times[ref]):.2f}, HEAD {min(head.times[ref]):.2f}"
            " (SQL of no store function's: how far the machine moved between the two)."
        )
    lines += [
        "",
        "A read is timed as the fastest of six calls, a write as n together;"
        " each side's time is its fastest over its runs.",
        "",
        "| | row | n | base statements | statements | base ms | ms | ratio | limit"
        " | base spread | spread | |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    failures = 0
    default = float(limits.get("limit", 1.5))
    for k in keys:
        if k[3] == REFERENCE:
            continue
        limit = float(limits.get("rows", {}).get(label(k), default))
        why = size_fails or judge(k, base, head, limit)
        failures += why is not None
        lines.append(row_line(k, base, head, limit, why))
    lines.append("")
    return lines, len(keys) - (ref in keys), failures


def cpu_lines(path: str | None) -> list[str]:
    """The machine's CPU model and count, from `lscpu`'s output."""
    if not path:
        return []
    fields = {}
    for line in pathlib.Path(path).read_text().splitlines():
        name, _, value = line.partition(":")
        fields[name.strip()] = value.strip()
    return [f"CPU: {fields.get('Model name', '?')}, {fields.get('CPU(s)', '?')} CPUs.", ""]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("limits")
    parser.add_argument("--base", nargs="+", required=True)
    parser.add_argument("--head", nargs="+", required=True)
    parser.add_argument("--cpu")
    args = parser.parse_args()
    limits = json.loads(pathlib.Path(args.limits).read_text())
    base, head = Side(args.base), Side(args.head)
    groups = dict.fromkeys(k[:2] for k in [*head.times, *base.times])
    lines = cpu_lines(args.cpu)
    total = failures = 0
    for g in groups:
        more, rows, failed = section(g, base, head, limits)
        lines += more
        total += rows
        failures += failed
    lines.append(f"{failures} of {total} rows fail." if failures else f"All {total} rows pass.")
    text = "\n".join(lines) + "\n"
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as out:
            out.write("## Segment bench, HEAD against refs/bench/git-sync\n\n" + text)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
