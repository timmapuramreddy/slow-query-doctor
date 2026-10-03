-- Attendance for one enrollment. attendance.enrollment_id has no index.
SELECT class_date, status
FROM attendance
WHERE enrollment_id = 4242
ORDER BY class_date;
