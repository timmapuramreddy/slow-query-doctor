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
- `sqd check --plan plan.json` reads a plan someone saved on their own database. It never connects to a database. See [Share a slow query](#share-a-slow-query).
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

When the fix is an index, time it with `--setup`:

```
$ cd examples/01-missing-index
$ sqd compare slow.sql --setup fix.sql
Ran each query 5 time(s) after 1 warm-up run(s): first without fix.sql, then with it.
fix.sql took 0.2 s to apply (not timed) and was rolled back.

                        median         min         max  rows
slow.sql               20.3 ms     18.7 ms     22.1 ms  3
slow.sql + fix.sql     0.36 ms     0.32 ms     0.66 ms  3

slow.sql + fix.sql is 56.4x faster (median 20.3 ms -> 0.36 ms).
Both queries returned the same 3 row(s).
```

- `--setup` times the slow query as it is. Then, inside one transaction, it runs the setup file, times the query again, and rolls everything back. The index never stays in your database.
- The setup file may only contain `CREATE INDEX`, `CREATE EXTENSION` and `ANALYZE`. Anything else is refused.
- The runs can't take turns here, because the index exists only inside that transaction. All runs without it come first. Speedups move between runs (40x to 56x for this example), so run it more than once.
- While it runs, `CREATE INDEX` blocks writes to that table. It gives up if it waits more than 5 s for a lock. Use a test database, not a busy production one.
- `sqd compare slow.sql fixed.sql --setup fix.sql` times a rewrite and an index together.

## Patterns

| # | Pattern | Status |
|---|---|---|
| 1 | Missing index on a filter column | done |
| 2 | Function wrapped around an indexed column (`lower(email) = ...`) | done |
| 3 | Leading wildcard search (`LIKE '%smith'`) | done |
| 4 | Deep `OFFSET` paging | done |
| 5 | Date filters that can't use an index (`date_trunc('day', created_at) = ...`) | done |
| 6 | Join on a column with no index | done |
| 7 | Index finds the rows, then a filter throws most of them away | done |

Each pattern has a slow query and its fix in [`examples/`](examples/). The fix is either a rewritten query (`fixed.sql`, time it with `sqd compare slow.sql fixed.sql`) or an index to add (`fix.sql`, time it with `sqd compare slow.sql --setup fix.sql`).

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

`sqd demo load --scale 10` loads ten times the rows (about 19.5M, 1.7 GB, a few minutes) so slow queries are slower and fixes show bigger gaps. Courses stay at 500. The tests expect scale 1.

## Share a slow query

You can check a slow query from your own database without giving anyone access to it. Save its plan and some table info as two files, then run `sqd check --plan` on them, or send them to someone who has `sqd`.

1. Save the plan as JSON. `EXPLAIN ANALYZE` really runs the query, so use it on a `SELECT`, or on a test copy of the database for anything that writes.

   ```
   psql -XqAt -c "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT ..." > plan.json
   ```

   Text-format `EXPLAIN` output is not supported. `-XqAt` makes psql print the raw JSON with no table borders.

2. Make the table info query and run it on the same database. It only reads `pg_catalog`: row estimates, column names and index definitions for the tables in the plan.

   ```
   sqd catalog-sql plan.json > catalog.sql
   psql -XqAt -f catalog.sql -o catalog.json
   ```

3. Check it. No database needed.

   ```
   sqd check --plan plan.json --catalog catalog.json
   ```

Without `--catalog`, only the rules that need no table info can run, and `sqd` lists the tables it knows nothing about.

Read both files before you share them. The plan contains the literal values from your query (emails, ids, dates), and the catalog contains your table, column and index names.

## Development

```
ruff check src tests
black --check src tests
pytest                            # the database test is skipped if SQD_DATABASE_URL is not set
```

## License

MIT
