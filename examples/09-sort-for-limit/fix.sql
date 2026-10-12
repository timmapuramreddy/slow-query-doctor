-- An index in filter-then-sort order: PostgreSQL reads the first 20 matching rows and stops.
CREATE INDEX attendance_status_class_date_id_idx ON attendance (status, class_date, id);
