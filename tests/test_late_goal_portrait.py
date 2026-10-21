"""Тесты модели «портрета» гола после 75' (specs/разведочный-анализ-поздних-голов.md).

Синтетика, без обращения к БД/CSV (НФТ-8).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.model import late_goal_portrait as lgp


def test_wilson_report_basic():
    y = pd.Series([1, 1, 1, 0, 0])
    r = lgp._wilson_report(y)
    assert r["n"] == 5
    assert r["share"] == pytest.approx(0.6)
    assert r["ci_low"] < 0.6 < r["ci_high"]


def _synthetic_xy(n: int, seed: int = 0):
    rng = np.random.RandomState(seed)
    x1 = rng.normal(size=n)
    x2 = rng.normal(size=n)
    logit = 1.5 * x1 - 0.5 * x2
    p = 1 / (1 + np.exp(-logit))
    y = (rng.random_sample(n) < p).astype(int)
    X = pd.DataFrame({"x1": x1, "x2": x2})
    return X, pd.Series(y)


def test_fit_logreg_ranking_recovers_dominant_feature_sign():
    X, y = _synthetic_xy(2000, seed=1)
    ranking = lgp.fit_logreg_ranking(X, y, ["x1", "x2"], n_bootstrap=30)
    assert ranking[0]["feature"] == "x1"  # x1 имеет больший истинный коэффициент
    assert ranking[0]["coef_std"] > 0
    assert ranking[0]["sign_stability"] > 0.8


def test_fit_tree_portrait_flags_confirmed_leaf_on_strong_signal():
    rng = np.random.RandomState(2)
    n = 2000
    x1 = rng.normal(size=n)
    p = np.where(x1 > 0, 0.8, 0.2)  # сильный, но не детерминированный сигнал
    y = pd.Series((rng.random_sample(n) < p).astype(int))
    X = pd.DataFrame({"x1": x1})

    result = lgp.fit_tree_portrait(X, y, X, y, ["x1"])  # train == holdout: сигнал должен подтвердиться
    assert any(leaf["confirmed_on_holdout"] for leaf in result["leaves"])
    assert "x1" in result["rules"]


def test_run_group_skips_target_below_gate():
    targets_df = pd.DataFrame(
        {
            "fixture_id": [1, 2, 3],
            "split": ["train", "train", "holdout"],
            "any_after": [1, 0, 1],
            "dominant_after": [1, 1, 0],
        }
    )
    features_df = pd.DataFrame({"fixture_id": [1, 2, 3], "feat": [0.1, 0.2, 0.3]})
    report = lgp.run_group(targets_df, features_df, ("feat",), "test")
    assert report["targets"]["any_after"]["skipped"] is True


def test_run_group_merges_on_fixture_id_and_drops_missing_features():
    targets_df = pd.DataFrame(
        {
            "fixture_id": list(range(1, 701)),
            "split": ["train"] * 400 + ["holdout"] * 300,
            "any_after": ([1, 0] * 350),
            "dominant_after": ([0, 1] * 350),
        }
    )
    features_df = pd.DataFrame({"fixture_id": list(range(1, 601)), "feat": np.random.RandomState(3).normal(size=600)})
    report = lgp.run_group(targets_df, features_df, ("feat",), "test")
    assert report["n_merged"] == 600  # матчи 601..700 без признаков отсеялись inner join'ом
