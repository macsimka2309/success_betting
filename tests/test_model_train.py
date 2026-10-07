"""Тесты обучения модели (specs/модель-предсказаний.md, «Обучение модели»).

Синтетические данные, без обращения к production-базе (НФТ-8), как и у
`build_features.py`/`test_model_splits.py`.
"""

from __future__ import annotations

import hashlib
from datetime import date, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from src.common.feature_columns import stat_columns as stat_columns_fn
from src.model import train as mt
from src.model.features import FEATURE_COLUMNS, TARGET_COLUMNS
from src.model.splits import time_split

RESULT_CLASSES = ["H", "D", "A"]
STAT_COLUMNS = {name for name, _ in stat_columns_fn()}

# Тестовый сплит по умолчанию: train=60 (i 0..59), val=24 (60..83),
# test=24 (84..107). Один конкретный индекс в test намеренно получает
# пропуск в LR-признаке, чтобы проверить, что логрегрессия оценивается
# на более узком подмножестве, чем LightGBM (specs, «Baseline»).
SPECIAL_NULL_LR_INDEX = 90

FAST_GBM_PARAMS = {
    "n_estimators": 20,
    "learning_rate": 0.3,
    "random_state": 42,
    "verbosity": -1,
    "min_child_samples": 3,
}


def _col_value(col: str, i: int) -> float:
    h = int(hashlib.sha1(col.encode()).hexdigest(), 16) % 1000
    return float((h + i) % 100) / 10.0


def _make_row(i: int) -> dict:
    row: dict = {}
    for col in FEATURE_COLUMNS:
        if col in STAT_COLUMNS and i % 5 != 0:
            row[col] = None  # имитация низкого покрытия статистики (ДП-4)
        else:
            value = _col_value(col, i)
            # имитация того, что psycopg отдаёт NUMERIC как Decimal
            row[col] = Decimal(str(value)) if i % 3 == 0 else value
    if i == SPECIAL_NULL_LR_INDEX:
        row["home_team_elo"] = None
    row[TARGET_COLUMNS["1x2"]] = RESULT_CLASSES[i % 3]
    row[TARGET_COLUMNS["league_total"]] = None if i % 7 == 0 else bool(i % 2)
    return row


def _make_synthetic_df(n_train: int = 60, n_val: int = 24, n_test: int = 24) -> pd.DataFrame:
    rows = []
    i = 0
    for n, start in ((n_train, date(2024, 1, 1)), (n_val, date(2025, 1, 1)), (n_test, date(2026, 1, 1))):
        for j in range(n):
            row = _make_row(i)
            row["match_date"] = start + timedelta(days=j)
            rows.append(row)
            i += 1
    return pd.DataFrame(rows)


@pytest.fixture
def splits():
    df = mt.coerce_feature_columns(_make_synthetic_df())
    return time_split(df)


def test_coerce_feature_columns_converts_decimal_and_keeps_nan():
    df = pd.DataFrame({"home_team_elo": [Decimal("1500.5"), None, 1400.0]})
    out = mt.coerce_feature_columns(df, columns=("home_team_elo",))
    assert out["home_team_elo"].dtype == np.float64
    assert out["home_team_elo"].iloc[0] == pytest.approx(1500.5)
    assert pd.isna(out["home_team_elo"].iloc[1])


def test_naive_multiclass_proba_matches_train_frequencies():
    train_y = pd.Series(["H", "H", "D", "A"])
    proba = mt.naive_multiclass_proba(train_y, classes=["A", "D", "H"], n_rows=3)
    assert proba.shape == (3, 3)
    assert np.allclose(proba[0], [0.25, 0.25, 0.5])
    assert np.allclose(proba, proba[0])  # одинаково для всех строк


def test_naive_binary_proba_is_train_mean():
    train_y = pd.Series([True, True, False, False, True])
    proba = mt.naive_binary_proba(train_y, n_rows=4)
    assert np.allclose(proba, 0.6)


def test_evaluate_multiclass_known_values():
    y_true = pd.Series(["H", "D"])
    proba = np.array([[0.1, 0.1, 0.8], [0.2, 0.7, 0.1]])  # порядок классов A, D, H
    result = mt.evaluate_multiclass(y_true, proba, classes=["A", "D", "H"])
    assert result["accuracy"] == 1.0
    assert result["n"] == 2
    assert result["brier"] > 0


def test_evaluate_binary_known_values():
    y_true = pd.Series([True, False, True, False])
    proba = np.array([0.9, 0.1, 0.6, 0.4])
    result = mt.evaluate_binary(y_true, proba)
    assert result["accuracy"] == 1.0
    assert result["n"] == 4


def test_train_1x2_runs_end_to_end_and_saves_artifacts(tmp_path, splits):
    result = mt.train_1x2(splits, tmp_path, gbm_params=FAST_GBM_PARAMS, early_stopping_rounds=5)

    assert set(result) == {"naive", "logreg", "lightgbm"}
    for name, metrics in result.items():
        assert set(metrics) == {"log_loss", "accuracy", "brier", "n"}
        assert metrics["n"] > 0
    assert (tmp_path / "result_1x2_lightgbm.joblib").exists()
    assert (tmp_path / "result_1x2_logreg_baseline.joblib").exists()


def test_train_1x2_logreg_uses_narrower_subset_than_lightgbm(tmp_path, splits):
    result = mt.train_1x2(splits, tmp_path, gbm_params=FAST_GBM_PARAMS, early_stopping_rounds=5)

    assert result["naive"]["n"] == result["lightgbm"]["n"]
    assert result["logreg"]["n"] == result["naive"]["n"] - 1


def test_train_league_total_runs_end_to_end_and_saves_artifacts(tmp_path, splits):
    result = mt.train_league_total(splits, tmp_path, gbm_params=FAST_GBM_PARAMS, early_stopping_rounds=5)

    assert set(result) == {"naive", "logreg", "lightgbm"}
    for name, metrics in result.items():
        assert set(metrics) == {"log_loss", "accuracy", "brier", "n"}
        assert metrics["n"] > 0
    assert (tmp_path / "league_total_lightgbm.joblib").exists()
    assert (tmp_path / "league_total_logreg_baseline.joblib").exists()


def test_train_league_total_excludes_rows_with_null_target(splits, tmp_path):
    target = TARGET_COLUMNS["league_total"]
    non_null_test_count = int(splits["test"][target].notna().sum())

    result = mt.train_league_total(splits, tmp_path, gbm_params=FAST_GBM_PARAMS, early_stopping_rounds=5)

    assert non_null_test_count < len(splits["test"])  # пропуски реально есть в синтетике
    assert result["naive"]["n"] == non_null_test_count
    assert result["lightgbm"]["n"] == non_null_test_count
    assert result["logreg"]["n"] == non_null_test_count - 1  # ещё минус строка со спец. пропуском в LR-признаке


def test_training_sql_default_has_no_stats_filter():
    sql = mt.training_sql(full_stats_only=False)
    assert sql.endswith("WHERE reg_home IS NOT NULL")


def test_training_sql_full_stats_only_requires_both_teams_full_coverage():
    sql = mt.training_sql(full_stats_only=True)
    assert "home_team_overall_short_stats_coverage = 1" in sql
    assert "away_team_overall_short_stats_coverage = 1" in sql
