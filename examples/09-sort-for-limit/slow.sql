-- The 20 most recent absences (id breaks ties between rows of the same day). PostgreSQL reads
-- every absence and sorts them all to keep 20.
SELECT id, enrollment_id, class_date
FROM attendance
WHERE status = 'absent'
ORDER BY class_date DESC, id DESC
LIMIT 20;
