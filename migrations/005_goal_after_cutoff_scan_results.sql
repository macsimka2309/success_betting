-- Результаты перебора признаков для голов после отсечки
-- (specs/разведочный-анализ-поздних-голов.md, раздел «Данные»).
-- Аддитивная миграция: одна новая таблица, ничего существующего не трогает.
-- Блокировок нет (новая пустая таблица).

CREATE TABLE goal_after_cutoff_scan_results (
    run_id                  UUID         NOT NULL,
    run_at                  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    cutoff_minute           SMALLINT     NOT NULL,
    phase                   TEXT         NOT NULL CHECK (phase IN ('full_match', 'half2_approx')),
    target                  TEXT         NOT NULL CHECK (target IN ('any_team', 'dominant_team')),
    split                   TEXT         NOT NULL CHECK (split IN ('train', 'holdout', 'monitor')),
    poss_threshold          SMALLINT     NOT NULL,
    shots_threshold         SMALLINT     NOT NULL,
    sog_threshold           SMALLINT     NOT NULL,
    situation               TEXT         NOT NULL,
    subst_threshold         SMALLINT     NOT NULL,
    cards_threshold         SMALLINT     NOT NULL,
    n                       INTEGER      NOT NULL,
    share                   NUMERIC(6, 4),
    ci_low                  NUMERIC(6, 4),
    ci_high                 NUMERIC(6, 4),
    ev_yes                  NUMERIC(6, 4),
    ev_no                   NUMERIC(6, 4),
    p_value                 NUMERIC(10, 8),
    p_value_corrected       NUMERIC(10, 8),
    is_candidate            BOOLEAN      NOT NULL DEFAULT false,
    meets_threshold_train   BOOLEAN      NOT NULL DEFAULT false,
    meets_threshold_holdout BOOLEAN      NOT NULL DEFAULT false,
    PRIMARY KEY (
        run_id, cutoff_minute, phase, target, split,
        poss_threshold, shots_threshold, sog_threshold, situation,
        subst_threshold, cards_threshold
    )
);

COMMENT ON TABLE goal_after_cutoff_scan_results IS
    'Результаты разового перебора признаков (specs/разведочный-анализ-поздних-голов.md). '
    'Один run_id — один прогон скрипта scan_goal_after_cutoff; строки накапливаются '
    'между прогонами для сравнения по мере наполнения событий/таймов.';

CREATE INDEX idx_goal_scan_run ON goal_after_cutoff_scan_results (run_id);
CREATE INDEX idx_goal_scan_candidates ON goal_after_cutoff_scan_results (meets_threshold_train, meets_threshold_holdout);
