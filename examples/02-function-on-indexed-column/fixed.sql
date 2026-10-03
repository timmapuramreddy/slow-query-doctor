-- Or compare the bare column. Only correct if emails are stored and searched in the same case.
SELECT id, first_name, last_name
FROM students
WHERE email = 'Student4242@example.edu';
