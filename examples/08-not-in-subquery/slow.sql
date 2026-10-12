-- Students 1 to 200 who have no enrollments. The list of enrolled students is too big to
-- keep in memory (work_mem, 4MB by default), so PostgreSQL searches it again for each student.
SELECT s.id, s.first_name, s.last_name
FROM students s
WHERE s.id <= 200
  AND s.id NOT IN (SELECT student_id FROM enrollments);
