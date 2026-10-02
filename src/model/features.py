"""Выбор признаков и целей для модели предсказаний.

Спецификация: specs/модель-предсказаний.md

Источник колонок — `src/common/feature_columns.py`, уже используемый
`ml_match_features` (единственный источник правды для имён, см. его
собственный докстринг). Здесь — только то, какое подмножество идёт
в обучение, а какое остаётся целью или исключается как утечка.
"""

from __future__ import annotations

from src.common.feature_columns import (
    elo_columns,
    eval_columns,
    form_columns,
    h2h_columns,
    injury_columns,
    league_context_columns,
    stat_columns,
    target_columns,
)

# Линии-признаки: формально задаются в target_columns() (считаются вместе
# с целями в build_features.py), но для модели это контекст, а не то, что
# предсказывается — предсказывается `league_total_over`, а не сама линия.
LINE_FEATURE_COLUMNS = ("league_total_line", "home_team_line", "away_team_line")

FEATURE_COLUMNS: tuple[str, ...] = tuple(
    [name for name, _ in form_columns()]
    + [name for name, _ in stat_columns()]
    + [name for name, _ in h2h_columns()]
    + [name for name, _ in injury_columns()]
    + [name for name, _ in league_context_columns()]
    + [name for name, _ in elo_columns()]
    + list(LINE_FEATURE_COLUMNS)
)

# Цели первой очереди (specs/модель-предсказаний.md, «Принятые решения»).
# Остальные 5 рынков — вне охвата этого этапа.
TARGET_COLUMNS: dict[str, str] = {
    "1x2": "result_1x2",
    "league_total": "league_total_over",
}

_TARGET_NAMES = {name for name, _ in target_columns()}
_EVAL_NAMES = {name for name, _ in eval_columns()}


def assert_no_leakage() -> None:
    """ДП-1/ДП-10: ни одна целевая или eval-колонка не должна попасть
    в признаки. Вызывается тестом, а не только при импорте — тест даёт
    понятное сообщение об ошибке, здесь же защита от молчаливой регрессии
    при будущих правках списка."""
    leaked_targets = (_TARGET_NAMES - set(LINE_FEATURE_COLUMNS)) & set(FEATURE_COLUMNS)
    leaked_eval = _EVAL_NAMES & set(FEATURE_COLUMNS)
    if leaked_targets or leaked_eval:
        raise AssertionError(
            f"утечка в FEATURE_COLUMNS: целевые={leaked_targets}, eval={leaked_eval}"
        )


assert_no_leakage()
