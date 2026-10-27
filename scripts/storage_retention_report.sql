-- Read-only evidence only. Run after the database can accept connections.
-- No payload export, DDL, maintenance, retention, or cleanup operation.
BEGIN TRANSACTION READ ONLY;
SET LOCAL statement_timeout = '15000';
SET LOCAL lock_timeout = '2000';
SELECT current_timestamp AS observed_at, pg_database_size(current_database()) AS database_bytes,
       to_regclass('public.prediction_publications')::text AS publication_table;
SELECT relname, pg_total_relation_size(relid) AS total_bytes,
       pg_relation_size(relid) AS heap_bytes, pg_indexes_size(relid) AS index_bytes,
       n_live_tup AS estimated_live_rows, n_dead_tup AS estimated_dead_rows
FROM pg_stat_user_tables WHERE schemaname='public'
ORDER BY pg_total_relation_size(relid) DESC;
SELECT league_id, count(*) AS edition_count, min(created_at) AS first_created,
       max(created_at) AS last_created
FROM public.board_editions GROUP BY league_id ORDER BY league_id;
SELECT count(*) AS editions_without_current_pointer
FROM public.board_editions e
WHERE NOT EXISTS (SELECT 1 FROM public.board_heads h WHERE h.version=e.version)
  AND NOT EXISTS (SELECT 1 FROM public.board_read_publications p WHERE p.version=e.version);
-- Absence of a current pointer does NOT authorize deleting historical evidence.
SELECT c.conrelid::regclass::text AS dependent_table, c.confrelid::regclass::text AS referenced_table,
       pg_get_constraintdef(c.oid) AS foreign_key
FROM pg_constraint c WHERE c.contype='f' AND c.confrelid='public.board_editions'::regclass;
SELECT c.relname AS table_name, t.tgname AS guard_name, t.tgenabled AS enabled
FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname='public' AND NOT t.tgisinternal AND c.relname IN
('board_editions','prediction_snapshots','prediction_settlements','prediction_publications');
ROLLBACK;
