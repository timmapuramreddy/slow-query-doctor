-- Failed finals graded in March 2024. The index on graded_at finds every grade from March,
-- then the filter on assessment and score throws most of them away.
SELECT id, enrollment_id, score, graded_at
FROM grades
WHERE graded_at >= '2024-03-01' AND graded_at < '2024-04-01'
  AND assessment = 'final'
  AND score < 50;
