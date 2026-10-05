-- Index the join key, so PostgreSQL can look up the enrollments of one course.
CREATE INDEX enrollments_course_id_idx ON enrollments (course_id);
