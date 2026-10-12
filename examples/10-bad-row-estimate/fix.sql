-- When the query can't change: statistics on the two expressions, then ANALYZE to fill them.
CREATE STATISTICS attendance_status_class_date_stats
    ON (lower(status)), (EXTRACT(year FROM class_date)) FROM attendance;
ANALYZE attendance;
