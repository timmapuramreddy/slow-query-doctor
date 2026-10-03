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

Coming next: `sqd compare slow.sql fixed.sql` (median timings, before vs after).

## Patterns

| # | Pattern | Status |
|---|---|---|
| 1 | Missing index on a filter column | done |
| 2 | Function wrapped around an indexed column (`lower(email) = ...`) | done |
| 3 | Leading wildcard search (`LIKE '%smith'`) | planned |
| 4 | Deep `OFFSET` paging | planned |
| 5 | Date filters that can't use an index (`date_trunc('day', created_at) = ...`) | planned |

Each pattern has a slow query and its fix in [`examples/`](examples/).

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
