# Changelog

## 1.1.0 - unreleased

### Changed

- `sqd check` and `sqd compare` now stop any single statement that runs longer than 300 s. Before, a slow query ran with no limit. This covers each `EXPLAIN ANALYZE`, each timed or warm-up run, and each `--setup` statement. The limit is per statement, not for the whole command.
- Change it with `--timeout SECONDS`. `--timeout 0` sets no limit of its own.
- A shorter `statement_timeout` already set on the server, database, role or session is kept. sqd never loosens it.
- A stopped statement exits with code 2 and says which limit stopped it, and which `--setup` statement if it was one. The transaction is rolled back, so nothing stays behind.
- The Python API has a new keyword `timeout` on `explain_analyze`, `compare_queries` and `compare_with_setup`. It defaults to `None`, which leaves `statement_timeout` alone, so code that calls them sees no change.

## 1.0.0 - 2026-10-14

First release. PostgreSQL only.

### What it does

- `sqd check <file.sql>` runs `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` on a single `SELECT` and says why it is slow, in plain words, with the SQL to fix it. The query runs in a read-only transaction that is rolled back.
- `sqd check --plan plan.json` reads a plan saved on another database, as FORMAT JSON or text `EXPLAIN (ANALYZE, BUFFERS)` output. It never connects to a database. `sqd catalog-sql` prints the query that saves the table info to go with it.
- `sqd compare slow.sql fixed.sql` times a rewrite against the original, taking turns, and checks both return the same rows.
- `sqd compare slow.sql --setup fix.sql` times an index or statistics fix inside a transaction that is rolled back.
- `sqd demo load` builds a made-up school database with a fixed seed, so every example gives the same result.

### Patterns found

1. Missing index on a filter column
2. Function wrapped around an indexed column (`lower(email) = ...`)
3. Leading wildcard search (`LIKE '%smith'`)
4. Deep `OFFSET` paging
5. Date filters that can't use an index (`date_trunc('day', created_at) = ...`)
6. Join on a column with no index
7. Index finds the rows, then a filter throws most of them away
8. Subquery that runs once per row (`NOT IN (SELECT ...)`)
9. `ORDER BY ... LIMIT` sorts every matching row to return a few
10. PostgreSQL guessed far too few rows and picked a nested loop that ran many times

Each pattern has a slow query and its fix in `examples/`.

### Fixed just before this release

- Column names in quotes that contain a `'`, `(` or `)`, such as `"it's"`, are now read correctly. Rule 2 used to miss `lower("it's")` and flag `"lower(x)" = 'a'` by mistake. Rule 5 missed date functions on such names.
- The time shown for a step below a parallel Gather is now the average per process. Before, it added up time from processes that ran side by side, so a step could show more time than the whole query.

### Known limits

- PostgreSQL only. Oracle and MySQL come later.
- FORMAT JSON is the safest plan format to share. Text output prints CTE names without quotes, so an odd CTE name can confuse the text reader.
- Times for a step below a Gather are an average per process, so one slow worker can be hidden.
