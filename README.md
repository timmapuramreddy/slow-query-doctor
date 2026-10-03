# Slow Query Doctor

Tell it a query is slow. It tells you why in plain words, and proves the fix with before/after timings.

Work in progress, built in public. Version 1 is PostgreSQL only. Oracle and MySQL come later.

## Planned for v1

```
sqd check slow.sql               # why is this slow
sqd compare slow.sql fixed.sql   # median timings, before vs after
```

- Runs PostgreSQL's `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` and reads the plan.
- Fixed rules find the problem, so every answer can be checked. No AI in v1.
- Ships with a synthetic school database (students, courses, enrollments, attendance, grades). No real data.

First five patterns:

1. Missing index on a join or filter column
2. Function wrapped around an indexed column (`lower(email) = ...`)
3. Leading wildcard search (`LIKE '%smith'`)
4. Deep `OFFSET` paging
5. Date filters that can't use an index (`date_trunc('day', created_at) = ...`)

## License

MIT
