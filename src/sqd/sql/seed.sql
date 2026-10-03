-- Fills the school schema with synthetic rows (about 1.9 million in total).
-- setseed() makes random() repeat the same sequence, so every run gives the same data.
-- No real people: names come from short fixed lists.

SELECT setseed(0.42);

INSERT INTO students (id, first_name, last_name, email, birth_date, enrolled_on, status)
SELECT
    g,
    (ARRAY['Asha','Ben','Chen','Dara','Eli','Fatima','Gus','Hana','Ivan','Jo',
           'Kai','Lena','Mo','Nia','Omar','Priya','Quinn','Ravi','Sara','Tom'])[1 + (g % 20)],
    (ARRAY['Smith','Garcia','Patel','Kim','Nguyen','Brown','Singh','Lopez','Khan','Rossi',
           'Muller','Sato','Silva','Okafor','Novak'])[1 + ((g / 20) % 15)],
    'Student' || g || '@example.edu',
    date '2000-01-01' + (random() * 3650)::int,
    date '2018-08-01' + (random() * 2500)::int,
    (ARRAY['active','active','active','active','graduated','withdrawn'])[1 + floor(random() * 6)::int]
FROM generate_series(1, 50000) AS g;

INSERT INTO courses (id, code, title, department, credits)
SELECT
    g,
    'C' || lpad(g::text, 4, '0'),
    'Course ' || g,
    (ARRAY['Math','Science','English','History','Art','Computing','Languages','Music'])[1 + (g % 8)],
    1 + (g % 4)
FROM generate_series(1, 500) AS g;

INSERT INTO enrollments (id, student_id, course_id, term, enrolled_at)
SELECT
    g,
    1 + floor(random() * 50000)::int,
    1 + floor(random() * 500)::int,
    (ARRAY['2023-fall','2024-spring','2024-fall','2025-spring','2025-fall'])[1 + (g % 5)],
    timestamptz '2023-08-01' + random() * interval '800 days'
FROM generate_series(1, 300000) AS g;

INSERT INTO attendance (id, enrollment_id, class_date, status)
SELECT
    g,
    1 + floor(random() * 300000)::int,
    date '2023-09-01' + (random() * 700)::int,
    (ARRAY['present','present','present','present','present','late','absent'])[1 + floor(random() * 7)::int]
FROM generate_series(1, 1000000) AS g;

INSERT INTO grades (id, enrollment_id, assessment, score, graded_at)
SELECT
    g,
    1 + floor(random() * 300000)::int,
    (ARRAY['quiz','homework','midterm','project','final'])[1 + (g % 5)],
    round((40 + random() * 60)::numeric, 2),
    timestamptz '2023-09-01' + random() * interval '700 days'
FROM generate_series(1, 600000) AS g;

ANALYZE;
