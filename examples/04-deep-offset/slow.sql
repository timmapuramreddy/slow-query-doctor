-- Page 25,001 of the attendance list, 20 rows per page.
SELECT id, enrollment_id, class_date, status
FROM attendance
ORDER BY id
LIMIT 20 OFFSET 500000;
