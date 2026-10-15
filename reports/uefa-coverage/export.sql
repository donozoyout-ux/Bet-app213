-- Run through a READ ONLY transaction. No credentials or raw provider payloads.
SELECT json_build_object(
  'as_of', now(),
  'leagues', (SELECT json_agg(l) FROM (
    SELECT id, external_id, name, country, enabled, schedule_format, verified_at
    FROM leagues ORDER BY id) l),
  'matches', (SELECT json_agg(m) FROM (
    SELECT id, external_match_id, league_id, home_team_id, away_team_id,
      kickoff_at, status, ft_home, ft_away, created_at, updated_at, last_scraped_at
    FROM matches WHERE kickoff_at >= '2024-01-01'
      AND kickoff_at < now() + interval '30 days' ORDER BY kickoff_at, id) m),
  'teams', (SELECT json_agg(t) FROM (
    SELECT id, external_id, name FROM teams ORDER BY id) t)
) AS audit;
