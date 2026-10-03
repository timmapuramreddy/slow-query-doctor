"""Command line entry point: sqd check, sqd compare, sqd demo load."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import psycopg

from sqd import db
from sqd.compare import compare_queries, format_comparison
from sqd.plan import walk
from sqd.rules import Finding, run_rules


def format_findings(findings: list[Finding], execution_ms: float) -> str:
    """Plain-text report for the terminal."""
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
    sql = Path(args.file).read_text()
    with psycopg.connect(db.get_dsn(args.dsn)) as conn:
        result = db.explain_analyze(conn, sql)
        tables = {n.relation for n in walk(result) if n.relation}
        catalog = db.load_catalog(conn, tables)
    print(format_findings(run_rules(result, catalog), float(result.get("Execution Time", 0.0))))
    return 0


def positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return value


def cmd_compare(args: argparse.Namespace) -> int:
    queries = [(name, Path(name).read_text()) for name in (args.slow, args.fixed)]
    with psycopg.connect(db.get_dsn(args.dsn)) as conn:
        before, after = compare_queries(conn, queries, runs=args.runs, warmup=args.warmup)
    print(format_comparison(before, after, args.warmup))
    return 0


def cmd_demo_load(args: argparse.Namespace) -> int:
    print("Creating the school schema and loading synthetic data (about 1.9M rows)...")
    with psycopg.connect(db.get_dsn(args.dsn)) as conn:
        db.load_demo(conn)
    print("Done.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sqd", description="Explain why a SQL query is slow.")
    parser.add_argument("--dsn", help="PostgreSQL URL (default: $SQD_DATABASE_URL)")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="run EXPLAIN ANALYZE on a SELECT and explain the plan")
    check.add_argument("file", help="file with one SELECT statement")
    check.set_defaults(func=cmd_check)

    compare = sub.add_parser("compare", help="time two SELECTs against each other")
    compare.add_argument("slow", help="file with the original SELECT")
    compare.add_argument("fixed", help="file with the rewritten SELECT")
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
