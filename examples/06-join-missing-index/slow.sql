-- Who is enrolled in one course. enrollments.course_id has no index, so the join reads
-- every enrollment to find the few hundred for this course.
SELECT c.code, e.student_id, e.term
FROM courses c
JOIN enrollments e ON e.course_id = c.id
WHERE c.code = 'C0042';
