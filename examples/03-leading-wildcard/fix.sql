-- A trigram index can serve LIKE '%x' and ILIKE. pg_trgm ships with PostgreSQL.
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX students_email_trgm_idx ON students USING gin (email gin_trgm_ops);
