-- Откат 008: возвращает названия колонок. Строки по гостям (team_side = 'away')
-- будут потеряны, так как прежняя схема хранит только хозяев.
DELETE FROM live_alerts_sent WHERE team_side = 'away';
ALTER TABLE live_alerts_sent DROP COLUMN team_side;
ALTER TABLE live_alerts_sent RENAME COLUMN opponent_shots_on_goal TO shots_on_goal_away;
ALTER TABLE live_alerts_sent RENAME COLUMN shots_on_goal TO shots_on_goal_home;
ALTER TABLE live_alerts_sent RENAME COLUMN possession TO possession_home;
