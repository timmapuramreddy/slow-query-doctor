-- Keyset paging: start after the last id of the previous page instead of counting rows.
-- Returns the same 20 rows as slow.sql here because the demo ids have no gaps.
SELECT id, enrollment_id, class_date, status
FROM attendance
WHERE id > 500000
ORDER BY id
LIMIT 20;
