-- Case-insensitive email lookup. students.email has an index, but lower(email) cannot use it.
SELECT id, first_name, last_name
FROM students
WHERE lower(email) = 'student4242@example.edu';
