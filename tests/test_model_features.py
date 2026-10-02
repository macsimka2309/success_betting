"""Тесты выбора признаков модели (specs/модель-предсказаний.md)."""

from __future__ import annotations

from src.common.feature_columns import eval_columns, target_columns
from src.model.features import FEATURE_COLUMNS, LINE_FEATURE_COLUMNS, TARGET_COLUMNS


def test_no_eval_columns_in_features():
    """ДП-10: коэффициенты для сверки — не признак, иначе утечка рыночного
    консенсуса в обучение."""
    eval_names = {name for name, _ in eval_columns()}
    assert not (eval_names & set(FEATURE_COLUMNS))


def test_no_target_columns_in_features_except_lines():
    """Целевые колонки не должны быть признаками — кроме линий (контекст,
    не цель; специально разрешено спецификацией)."""
    target_names = {name for name, _ in target_columns()}
    leaked = (target_names - set(LINE_FEATURE_COLUMNS)) & set(FEATURE_COLUMNS)
    assert not leaked


def test_line_features_are_present():
    """Линии нужны модели как контекст (specs/модель-предсказаний.md)."""
    for line in LINE_FEATURE_COLUMNS:
        assert line in FEATURE_COLUMNS


def test_first_priority_targets_defined():
    assert TARGET_COLUMNS == {"1x2": "result_1x2", "league_total": "league_total_over"}


def test_feature_columns_has_no_duplicates():
    assert len(FEATURE_COLUMNS) == len(set(FEATURE_COLUMNS))
