-- Read-only quality queries; stop rather than scan indefinitely.
BEGIN READ ONLY;
SET LOCAL statement_timeout='5s';
SELECT l.name league,count(*) finished_matches,
count(*) FILTER(WHERE json_typeof(m.raw->'schedule')='array') stored_schedule,
count(*) FILTER(WHERE m.raw->'schedule'->>0=m.external_match_id::text) source_id_matches,
count(*) FILTER(WHERE m.raw->'schedule'->>1=l.external_id::text) source_league_matches,
count(*) FILTER(WHERE m.raw->'schedule'->>4=h.external_id::text AND m.raw->'schedule'->>5=a.external_id::text) source_teams_match,
count(*) FILTER(WHERE m.raw->'schedule'->>6=concat(m.ft_home,'-',m.ft_away)) stored_schedule_ft_matches,
count(*) FILTER(WHERE m.raw->'schedule'->>7=concat(m.ht_home,'-',m.ht_away)) stored_schedule_ht_matches
FROM matches m JOIN leagues l ON l.id=m.league_id JOIN teams h ON h.id=m.home_team_id JOIN teams a ON a.id=m.away_team_id WHERE m.status='finished' AND m.kickoff_at>='2024-01-01' GROUP BY l.name ORDER BY l.name;
WITH prices AS (
 SELECT match_id,bookmaker_id,'1x2' market,(opening_home>1 AND opening_draw>1 AND opening_away>1 AND closing_home>1 AND closing_draw>1 AND closing_away>1) complete,raw FROM odds_1x2
 UNION ALL SELECT match_id,bookmaker_id,'AH',(opening_line IS NOT NULL AND closing_line IS NOT NULL AND opening_home>1 AND opening_away>1 AND closing_home>1 AND closing_away>1),raw FROM asian_handicap
 UNION ALL SELECT match_id,bookmaker_id,'OU',(opening_line IS NOT NULL AND closing_line IS NOT NULL AND opening_over>1 AND opening_under>1 AND closing_over>1 AND closing_under>1),raw FROM asian_totals)
 SELECT l.name league,s.season_name,b.name bookmaker,p.market,count(*) rows,count(DISTINCT m.id) matches,count(*) FILTER(WHERE p.complete) complete_open_close,count(*) FILTER(WHERE p.raw->'_field_observed_at' IS NOT NULL) observation_metadata_present FROM prices p JOIN matches m ON m.id=p.match_id JOIN leagues l ON l.id=m.league_id JOIN seasons s ON s.id=m.season_id JOIN bookmakers b ON b.id=p.bookmaker_id WHERE m.status='finished' AND m.kickoff_at>='2024-01-01' AND b.name IN ('Crown','Bet365','Sbobet') GROUP BY l.name,s.season_name,b.name,p.market ORDER BY l.name,s.season_name,b.name,p.market;
COMMIT;

