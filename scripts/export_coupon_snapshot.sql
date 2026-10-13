-- Run with a read-only production connection. Export the single snapshot column.
-- No credentials, Telegram settings or delivery records are included.
WITH eligible AS (
  SELECT m.* FROM matches m JOIN leagues l ON l.id=m.league_id
  WHERE l.enabled AND m.status='scheduled' AND m.kickoff_at>now()
    AND m.kickoff_at<=now()+interval '7 days'
)
SELECT jsonb_build_object(
  'captured_at',now(),
  'leagues',(SELECT jsonb_agg(to_jsonb(l)) FROM leagues l WHERE l.id IN (SELECT league_id FROM eligible)),
  'seasons',(SELECT jsonb_agg(to_jsonb(s)) FROM seasons s WHERE s.id IN (SELECT season_id FROM eligible)),
  'teams',(SELECT jsonb_agg(to_jsonb(t)) FROM teams t WHERE t.id IN (SELECT home_team_id FROM eligible UNION SELECT away_team_id FROM eligible)),
  'matches',(SELECT jsonb_agg((to_jsonb(m)-'raw')||jsonb_build_object('raw','{}'::jsonb,'referee_id',NULL)) FROM eligible m),
  'bookmakers',(SELECT jsonb_agg(to_jsonb(b)) FROM bookmakers b),
  'prediction_boards',(SELECT jsonb_agg(to_jsonb(b)) FROM prediction_boards b WHERE b.league_id IN (SELECT league_id FROM eligible)),
  'odds_1x2',(SELECT jsonb_agg((to_jsonb(o)-'raw')||jsonb_build_object('raw',jsonb_build_object('_field_observed_at',o.raw->'_field_observed_at','_field_lines',o.raw->'_field_lines'))) FROM odds_1x2 o WHERE o.match_id IN (SELECT id FROM eligible)),
  'asian_totals',(SELECT jsonb_agg((to_jsonb(o)-'raw')||jsonb_build_object('raw',jsonb_build_object('_field_observed_at',o.raw->'_field_observed_at','_field_lines',o.raw->'_field_lines'))) FROM asian_totals o WHERE o.match_id IN (SELECT id FROM eligible)),
  'asian_handicap',(SELECT jsonb_agg((to_jsonb(o)-'raw')||jsonb_build_object('raw',jsonb_build_object('_field_observed_at',o.raw->'_field_observed_at','_field_lines',o.raw->'_field_lines'))) FROM asian_handicap o WHERE o.match_id IN (SELECT id FROM eligible)),
  'market_odds',(SELECT jsonb_agg((to_jsonb(o)-'raw')||jsonb_build_object('raw',jsonb_build_object('_field_observed_at',o.raw->'_field_observed_at','_field_lines',o.raw->'_field_lines'))) FROM market_odds o WHERE o.match_id IN (SELECT id FROM eligible))
) AS snapshot;
