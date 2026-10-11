-- NOT EXISTS runs as one anti join that looks up each student in enrollments_student_id_idx.
-- Same rows as slow.sql because enrollments.student_id is NOT NULL.
SELECT s.id, s.first_name, s.last_name
FROM students s
WHERE s.id <= 200
  AND NOT EXISTS (SELECT 1 FROM enrollments e WHERE e.student_id = s.id);
