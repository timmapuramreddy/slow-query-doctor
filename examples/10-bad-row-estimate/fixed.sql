-- Plain columns have statistics, so PostgreSQL expects the right number of rows and joins
-- with hash joins. Same rows as slow.sql because every status is already lowercase.
SELECT s.status, count(*)
FROM attendance a
JOIN enrollments e ON e.id = a.enrollment_id
JOIN students s ON s.id = e.student_id
WHERE a.status = 'present'
  AND a.class_date >= date '2024-01-01'
  AND a.class_date < date '2025-01-01'
GROUP BY s.status;
