-- One index for the whole filter: the equality column first, then the ranges.
CREATE INDEX grades_assessment_graded_at_score_idx ON grades (assessment, graded_at, score);
