-- Index the expression the query actually compares.
CREATE INDEX students_lower_email_idx ON students (lower(email));
