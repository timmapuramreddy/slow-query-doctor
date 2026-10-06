"""Command line entry point: sqd check, sqd catalog-sql, sqd compare, sqd demo load."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import psycopg

from sqd import db
from sqd.catalog import Catalog
from sqd.compare import compare_queries, compare_with_setup, format_comparison
from sqd.plan import parse_explain, relations
from sqd.rules import Finding, run_rules


def format_findings(findings: list[Finding], execution_ms: float | None) -> str:
    """Plain-text report for the terminal. execution_ms is None if the plan has no time."""
    if execution_ms is None:
        lines = ["Query time is not in the plan (no Execution Time; SUMMARY OFF leaves it out)."]
    else:
        lines = [f"Query ran in {execution_ms:.1f} ms."]
    if not findings:
        lines.append("No known slow patterns found.")
        return "\n".join(lines)
    lines.append(f"Found {len(findings)} problem(s):")
    for n, f in enumerate(findings, 1):
        lines += [
            "",
            f"{n}. {f.title}  [{f.rule_id}]",
            f"   Why:  {f.explanation}",
            f"   Plan: {f.node}  ({f.time_ms:.1f} ms)",
            f"   Fix:  {f.suggestion}",
        ]
    return "\n".join(lines)


def cmd_check(args: argparse.Namespace) -> int:
    if args.plan is not None:
        if args.file is not None:
            raise ValueError("Give a SQL file or --plan, not both.")
        # A shared plan: read files only, never connect to a database.
        result = parse_explain(Path(args.plan).read_bytes())
        catalog = (
            Catalog.from_json(Path(args.catalog).read_bytes())
            if args.catalog is not None
            else Catalog({})
        )
        hint = (
            f"Run `sqd catalog-sql {args.plan} > catalog.sql`, run catalog.sql on the database "
            "the plan came from (psql -XqAt -f catalog.sql -o catalog.json), then add "
            "--catalog catalog.json."
        )
    else:
        if args.file is None:
            raise ValueError("Give a SQL file or --plan FILE.")
        if args.catalog is not None:
            raise ValueError("--catalog only works with --plan.")
        sql = Path(args.file).read_text()
        with psycopg.connect(db.get_dsn(args.dsn)) as conn:
            result = db.explain_analyze(conn, sql)
            catalog = db.load_catalog(conn, relations(result))
        hint = "They were not found on the search_path."
    missing = sorted(relations(result) - catalog.tables.keys())
    if missing:
        print(
            f"sqd: no table info for {', '.join(missing)}, so rules that need row counts "
            f"and indexes skipped them. {hint}",
            file=sys.stderr,
        )
    execution_ms = result.get("Execution Time")
    print(format_findings(run_rules(result, catalog), execution_ms))
    return 0


def cmd_catalog_sql(args: argparse.Namespace) -> int:
    # A plan that reads no tables still gets a query; it returns {} so the steps stay the same.
    print(db.catalog_sql(relations(parse_explain(Path(args.plan).read_bytes()))))
    return 0


def demo_scale(text: str) -> int:
    value = int(text)
    if not 1 <= value <= db.MAX_DEMO_SCALE:
        raise argparse.ArgumentTypeError(f"must be 1 to {db.MAX_DEMO_SCALE}")
    return value


def positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return value


def cmd_compare(args: argparse.Namespace) -> int:
    if not args.fixed and not args.setup:
        raise ValueError("Give a fixed query file, --setup FILE, or both.")
    slow = (args.slow, Path(args.slow).read_text())
    fixed = (args.fixed, Path(args.fixed).read_text()) if args.fixed else slow
    with psycopg.connect(db.get_dsn(args.dsn)) as conn:
        if args.setup:
            after = (f"{fixed[0]} + {args.setup}", fixed[1])
            before_t, after_t, setup_ms = compare_with_setup(
                conn, slow, after, Path(args.setup).read_text(), args.runs, args.warmup
            )
            print(format_comparison(before_t, after_t, args.warmup, (args.setup, setup_ms)))
        else:
            before_t, after_t = compare_queries(conn, [slow, fixed], args.runs, args.warmup)
            print(format_comparison(before_t, after_t, args.warmup))
    return 0


def cmd_demo_load(args: argparse.Namespace) -> int:
    rows = db.DEMO_ROWS_PER_SCALE * args.scale + db.DEMO_FIXED_ROWS
    print(f"Creating the school schema and loading synthetic data ({rows:,} rows)...")
    with psycopg.connect(db.get_dsn(args.dsn)) as conn:
        db.load_demo(conn, args.scale)
    print("Done.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sqd", description="Explain why a SQL query is slow.")
    parser.add_argument("--dsn", help="PostgreSQL URL (default: $SQD_DATABASE_URL)")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser(
        "check",
        help="run EXPLAIN ANALYZE on a SELECT, or read a shared plan, and explain it",
        description="Explain why a query is slow. Give a SQL file to run EXPLAIN ANALYZE on "
        "the database, or --plan with saved EXPLAIN (ANALYZE, FORMAT JSON) output to check it "
        "without any database access.",
    )
    check.add_argument("file", nargs="?", help="file with one SELECT statement")
    check.add_argument(
        "--plan", metavar="FILE", help="EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) output to read"
    )
    check.add_argument(
        "--catalog",
        metavar="FILE",
        help="table info for --plan, made with `sqd catalog-sql` (without it most rules skip)",
    )
    check.set_defaults(func=cmd_check)

    catalog_sql = sub.add_parser(
        "catalog-sql",
        help="print a read-only query that collects table info for a shared plan",
        description="Print SQL that reads row estimates, columns and indexes for the tables in "
        "a plan. Run it on the database the plan came from: "
        "psql -XqAt -f catalog.sql -o catalog.json",
    )
    catalog_sql.add_argument("plan", help="EXPLAIN (ANALYZE, FORMAT JSON) output")
    catalog_sql.set_defaults(func=cmd_catalog_sql)

    compare = sub.add_parser(
        "compare",
        help="time a slow SELECT against its rewrite, its index fix, or both",
        description="Time a slow SELECT against a rewrite (fixed) and/or an index fix (--setup). "
        "--setup runs inside a transaction that is rolled back, but blocks writes to the "
        "table while it runs: use a test database, not a busy production one.",
    )
    compare.add_argument("slow", help="file with the original SELECT")
    compare.add_argument(
        "fixed", nargs="?", help="file with the rewritten SELECT (default: slow again)"
    )
    compare.add_argument(
        "--setup",
        metavar="FILE",
        help="CREATE INDEX / CREATE EXTENSION / ANALYZE to apply before timing the fix; "
        "rolled back afterwards",
    )
    compare.add_argument(
        "-n", "--runs", type=positive_int, default=5, help="timed runs (default 5)"
    )
    compare.add_argument(
        "--warmup", type=int, default=1, help="untimed runs before timing (default 1)"
    )
    compare.set_defaults(func=cmd_compare)

    demo = sub.add_parser("demo", help="demo database commands")
    demo_sub = demo.add_subparsers(dest="demo_command", required=True)
    load = demo_sub.add_parser("load", help="create and fill the synthetic school database")
    load.add_argument(
        "--scale",
        type=demo_scale,
        default=1,
        help="multiply the row counts (courses stay at 500); 1 is what the tests expect",
    )
    load.set_defaults(func=cmd_demo_load)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, RuntimeError, OSError, psycopg.Error) as exc:
        print(f"sqd: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
