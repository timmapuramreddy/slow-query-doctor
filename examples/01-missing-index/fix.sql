-- Run once, then run slow.sql again: PostgreSQL can now jump to the matching rows.
CREATE INDEX attendance_enrollment_id_idx ON attendance (enrollment_id);
