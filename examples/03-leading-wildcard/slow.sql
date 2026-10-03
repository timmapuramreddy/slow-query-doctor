-- Find students whose email ends with a number. The leading % stops the email index from helping.
SELECT id, first_name, last_name, email
FROM students
WHERE email LIKE '%4242@example.edu';
