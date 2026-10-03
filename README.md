# Slow Query Doctor

Tell it a query is slow. It tells you why in plain words, and proves the fix with before/after timings.

Work in progress, built in public. Version 1 is PostgreSQL only. Oracle and MySQL come later.

## What works today

```
$ sqd check examples/01-missing-index/slow.sql
Query ran in 42.5 ms.
Found 1 problem(s):

1. No index on attendance.enrollment_id  [missing-index]
   Why:  PostgreSQL read every row of attendance (about 1,000,000) one by one and kept 3 (under 0.01%). There is no index on enrollment_id, so it has no shortcut to the matching rows.
   Plan: Seq Scan on attendance (Filter: (enrollment_id = 4242))  (33.7 ms)
   Fix:  CREATE INDEX ON attendance (enrollment_id);
```

- `sqd check <file.sql>` runs PostgreSQL's `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` and reads the plan.
- Fixed rules find the problem, so every answer can be checked. No AI in v1.
- Only single `SELECT` statements are accepted, and they run in a read-only transaction that is rolled back. `EXPLAIN ANALYZE` really executes the query, so this matters.

Then prove the fix:

```
$ cd examples/05-non-sargable-date
$ sqd compare slow.sql fixed.sql
Ran each query 5 time(s) after 1 warm-up run(s), taking turns.

              median         min         max  rows
slow.sql     41.9 ms     41.1 ms     42.4 ms  803
fixed.sql     1.9 ms      1.8 ms      6.7 ms  803

fixed.sql is 21.6x faster (median 41.9 ms -> 1.9 ms).
Both queries returned the same 803 row(s).
```

- `sqd compare <slow.sql> <fixed.sql>` runs one warm-up, then 5 timed runs of each query, taking turns. Change it with `-n/--runs` and `--warmup`.
- Times are wall clock for running the query and fetching every row, not `EXPLAIN ANALYZE` times (those include measuring overhead).
- It checks both queries return the same rows, so a "fix" that changes the answer gets a warning.
- Timings above are from a laptop (Apple M1 Pro, PostgreSQL 17 in Docker). Yours will differ.

## Patterns

| # | Pattern | Status |
|---|---|---|
| 1 | Missing index on a filter column | done |
| 2 | Function wrapped around an indexed column (`lower(email) = ...`) | done |
| 3 | Leading wildcard search (`LIKE '%smith'`) | done |
| 4 | Deep `OFFSET` paging | done |
| 5 | Date filters that can't use an index (`date_trunc('day', created_at) = ...`) | done |

Each pattern has a slow query and its fix in [`examples/`](examples/). The fix is either a rewritten query (`fixed.sql`, which `sqd compare` can time) or an index to add (`fix.sql`).

## Try it

Needs Python 3.11+ and Docker (or any PostgreSQL you can point it at).

```
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'

cp .env.example .env              # pick your own password
docker compose up -d              # PostgreSQL 17 on localhost:5434
set -a && . ./.env && set +a      # exports SQD_DATABASE_URL

sqd demo load                     # synthetic school data, about 1.9M rows
sqd check examples/01-missing-index/slow.sql
```

The demo database is a made-up school: students, courses, enrollments, attendance, grades. All rows are generated in SQL with a fixed seed, so everyone gets the same data. No real people.

## Development

```
ruff check src tests
black --check src tests
pytest                            # the database test is skipped if SQD_DATABASE_URL is not set
```

## License

MIT
