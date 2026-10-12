-- Present days in 2024, counted by student status. PostgreSQL keeps no statistics for
-- lower(status) or EXTRACT(year ...), so it guesses about 30 rows from attendance. It gets
-- 372,907 and looks up enrollments and students once for each of them in nested loops.
SELECT s.status, count(*)
FROM attendance a
JOIN enrollments e ON e.id = a.enrollment_id
JOIN students s ON s.id = e.student_id
WHERE lower(a.status) = 'present'
  AND extract(year FROM a.class_date) = 2024
GROUP BY s.status;
