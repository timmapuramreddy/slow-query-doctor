-- Grades recorded on one day. date_trunc() hides graded_at from its index.
SELECT id, enrollment_id, assessment, score
FROM grades
WHERE date_trunc('day', graded_at) = '2024-03-15';
