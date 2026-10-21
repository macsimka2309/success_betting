-- Убирает карточки из решётки перебора и заменяет совмещённый "subst_threshold"
-- (количество замен, 0..3+) на два булевых признака — факт замены ведущей и
-- отстающей команды в окне [60, cutoff_minute] (specs/разведочный-анализ-
-- поздних-голов.md, «Принятые решения», п. 16).
--
-- ВНИМАНИЕ: очищает таблицу (TRUNCATE) перед изменением колонок. Старые
-- строки (три прогона 13.10.2026 по прежней решётке с картами) под новые
-- колонки не переносятся — они относятся к уже не существующему определению
-- решётки (нет смысла хранить "cards_threshold=1" без оси карточек). Цифры
-- из этих прогонов уже разобраны в переписке чата, в БД они не нужны.
-- Применять только по отдельному подтверждению владельца (как и всегда для
-- изменений схемы, здесь — ещё и из-за потери данных).

TRUNCATE TABLE goal_after_cutoff_scan_results;

ALTER TABLE goal_after_cutoff_scan_results DROP CONSTRAINT goal_after_cutoff_scan_results_pkey;
ALTER TABLE goal_after_cutoff_scan_results DROP COLUMN cards_threshold;
ALTER TABLE goal_after_cutoff_scan_results DROP COLUMN subst_threshold;
ALTER TABLE goal_after_cutoff_scan_results ADD COLUMN dominant_subst_window BOOLEAN;
ALTER TABLE goal_after_cutoff_scan_results ADD COLUMN other_subst_window BOOLEAN;
ALTER TABLE goal_after_cutoff_scan_results ADD PRIMARY KEY (
    run_id, cutoff_minute, phase, target, split,
    poss_threshold, shots_threshold, sog_threshold, situation,
    dominant_subst_window, other_subst_window
);

COMMENT ON COLUMN goal_after_cutoff_scan_results.dominant_subst_window IS
    'Была ли хотя бы одна замена у команды с преимуществом по владению в окне [60, cutoff_minute] (верхняя граница — та же отсечка, что и у исхода).';
COMMENT ON COLUMN goal_after_cutoff_scan_results.other_subst_window IS
    'То же для другой команды.';
