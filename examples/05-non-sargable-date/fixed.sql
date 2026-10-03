-- Same day as a half-open range on the bare column, so grades_graded_at_idx can be used.
SELECT id, enrollment_id, assessment, score
FROM grades
WHERE graded_at >= '2024-03-15' AND graded_at < '2024-03-16';
