-- Откат 006: возвращает cards_threshold/subst_threshold, убирает
-- dominant_subst_window/other_subst_window. Таблица уже пуста после TRUNCATE в
-- миграции вверх — откат не восстанавливает удалённые ею строки.

TRUNCATE TABLE goal_after_cutoff_scan_results;

ALTER TABLE goal_after_cutoff_scan_results DROP CONSTRAINT goal_after_cutoff_scan_results_pkey;
ALTER TABLE goal_after_cutoff_scan_results DROP COLUMN dominant_subst_window;
ALTER TABLE goal_after_cutoff_scan_results DROP COLUMN other_subst_window;
ALTER TABLE goal_after_cutoff_scan_results ADD COLUMN subst_threshold SMALLINT NOT NULL DEFAULT 0;
ALTER TABLE goal_after_cutoff_scan_results ADD COLUMN cards_threshold SMALLINT NOT NULL DEFAULT 0;
ALTER TABLE goal_after_cutoff_scan_results ADD PRIMARY KEY (
    run_id, cutoff_minute, phase, target, split,
    poss_threshold, shots_threshold, sog_threshold, situation,
    subst_threshold, cards_threshold
);
