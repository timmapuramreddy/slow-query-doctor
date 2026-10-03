-- Synthetic school schema for the Slow Query Doctor demo database.
-- Some indexes are left out on purpose so the rules have something to find.

DROP TABLE IF EXISTS grades, attendance, enrollments, courses, students CASCADE;

CREATE TABLE students (
    id          integer PRIMARY KEY,
    first_name  text        NOT NULL,
    last_name   text        NOT NULL,
    email       varchar(200) NOT NULL,
    birth_date  date        NOT NULL,
    enrolled_on date        NOT NULL,
    status      text        NOT NULL
);
CREATE UNIQUE INDEX students_email_idx ON students (email);

CREATE TABLE courses (
    id         integer PRIMARY KEY,
    code       text    NOT NULL UNIQUE,
    title      text    NOT NULL,
    department text    NOT NULL,
    credits    integer NOT NULL
);

CREATE TABLE enrollments (
    id          integer PRIMARY KEY,
    student_id  integer     NOT NULL REFERENCES students (id),
    course_id   integer     NOT NULL REFERENCES courses (id),
    term        text        NOT NULL,
    enrolled_at timestamptz NOT NULL
);
CREATE INDEX enrollments_student_id_idx ON enrollments (student_id);
-- No index on enrollments.course_id (on purpose).

CREATE TABLE attendance (
    id            integer PRIMARY KEY,
    enrollment_id integer NOT NULL REFERENCES enrollments (id),
    class_date    date    NOT NULL,
    status        text    NOT NULL
);
-- No index on attendance.enrollment_id (on purpose).

CREATE TABLE grades (
    id            integer PRIMARY KEY,
    enrollment_id integer      NOT NULL REFERENCES enrollments (id),
    assessment    text         NOT NULL,
    score         numeric(5,2) NOT NULL,
    graded_at     timestamptz  NOT NULL
);
CREATE INDEX grades_graded_at_idx ON grades (graded_at);
