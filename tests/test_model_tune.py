"""Тесты подбора гиперпараметров (specs/модель-предсказаний.md,
«Подбор гиперпараметров для 1X2»).

Синтетические данные, без обращения к production-базе (НФТ-8) —
переиспользует генератор из test_model_train.py.
"""

from __future__ import annotations

import random

import pytest

from src.model import train as mt
from src.model import tune as mtune
from src.model.splits import time_split
from tests.test_model_train import _make_synthetic_df

SMALL_PARAM_SPACE = {
    "learning_rate": [0.1, 0.3],
    "num_leaves": [7, 15],
}
FAST_SEARCH_KWARGS = {"n_estimators": 15, "early_stopping_rounds": 5}


@pytest.fixture
def splits():
    df = mt.coerce_feature_columns(_make_synthetic_df())
    return time_split(df)


def test_sample_params_draws_from_every_dimension():
    rng = random.Random(1)
    params = mtune.sample_params(rng, SMALL_PARAM_SPACE)
    assert params["learning_rate"] in SMALL_PARAM_SPACE["learning_rate"]
    assert params["num_leaves"] in SMALL_PARAM_SPACE["num_leaves"]


def test_random_search_caps_trials_at_param_space_size(splits):
    # SMALL_PARAM_SPACE даёт всего 2*2=4 уникальные комбинации — запрос
    # 100 попыток не должен зациклиться на попытках найти несуществующую
    # пятую уникальную комбинацию.
    results = mtune.random_search(splits, n_trials=100, param_space=SMALL_PARAM_SPACE, **FAST_SEARCH_KWARGS)
    assert len(results) == 4


def test_random_search_returns_sorted_by_val_log_loss(splits):
    results = mtune.random_search(splits, n_trials=4, param_space=SMALL_PARAM_SPACE, **FAST_SEARCH_KWARGS)
    losses = [r["val_log_loss"] for r in results]
    assert losses == sorted(losses)


def test_random_search_never_touches_test_split(splits):
    """Выбор конфигурации — строго по val; test не должен даже читаться
    на этом шаге (иначе это была бы утечка в подбор гиперпараметров)."""
    splits_without_test = {"train": splits["train"], "val": splits["val"], "test": None}
    results = mtune.random_search(splits_without_test, n_trials=2, param_space=SMALL_PARAM_SPACE, **FAST_SEARCH_KWARGS)
    assert len(results) == 2


def test_refit_best_runs_end_to_end_and_returns_test_metrics(splits):
    best_params = {"learning_rate": 0.3, "num_leaves": 7}
    gbm, test_metrics = mtune.refit_best(splits, best_params, n_estimators=15, early_stopping_rounds=5)

    assert set(test_metrics) == {"log_loss", "accuracy", "brier", "n"}
    assert test_metrics["n"] > 0
    assert gbm.predict_proba is not None
