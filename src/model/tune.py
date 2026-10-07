"""Подбор гиперпараметров LightGBM для 1X2.

Спецификация: specs/модель-предсказаний.md, «Подбор гиперпараметров для 1X2».

Только `result_1x2` — для `league_total_over` LightGBM уже стабильно лучше
baseline (`train.py`), подбор для него не оправдан. Случайный поиск по
`val` (log loss); `test` используется один раз, только для финального
отчёта по уже выбранной конфигурации — не для выбора между попытками.

Использование:
    python3 -m src.model.tune [--n-trials 12] [--models-dir models]
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Any

import joblib
import lightgbm as lgb
import pandas as pd

from src.db.connection import connect, database_url
from src.model.features import FEATURE_COLUMNS, TARGET_COLUMNS
from src.model.splits import time_split
from src.model.train import coerce_feature_columns, evaluate_multiclass, load_training_rows

RANDOM_STATE = 42

SEARCH_N_ESTIMATORS = 500
SEARCH_EARLY_STOPPING_ROUNDS = 30
# тот же бюджет, что у дефолтной модели в train.py — чтобы сравнение было
# честным (выигрыш не просто от более долгого обучения)
FINAL_N_ESTIMATORS = 2000
FINAL_EARLY_STOPPING_ROUNDS = 50

PARAM_SPACE: dict[str, list[Any]] = {
    "learning_rate": [0.02, 0.05, 0.1],
    "num_leaves": [15, 31, 63, 127],
    "min_child_samples": [10, 20, 50, 100],
    "feature_fraction": [0.7, 0.9, 1.0],
}


def sample_params(rng: random.Random, param_space: dict[str, list[Any]] = PARAM_SPACE) -> dict[str, Any]:
    return {name: rng.choice(values) for name, values in param_space.items()}


def _fit_gbm(
    params: dict[str, Any],
    train_X: pd.DataFrame,
    train_y: pd.Series,
    val_X: pd.DataFrame,
    val_y: pd.Series,
    classes: list[str],
    n_estimators: int,
    early_stopping_rounds: int,
) -> lgb.LGBMClassifier:
    gbm = lgb.LGBMClassifier(
        objective="multiclass",
        num_class=len(classes),
        n_estimators=n_estimators,
        random_state=RANDOM_STATE,
        verbosity=-1,
        **params,
    )
    gbm.fit(
        train_X,
        train_y,
        eval_set=[(val_X, val_y)],
        eval_metric="multi_logloss",
        callbacks=[lgb.early_stopping(early_stopping_rounds, verbose=False)],
    )
    return gbm


def random_search(
    splits: dict[str, pd.DataFrame],
    n_trials: int,
    param_space: dict[str, list[Any]] = PARAM_SPACE,
    n_estimators: int = SEARCH_N_ESTIMATORS,
    early_stopping_rounds: int = SEARCH_EARLY_STOPPING_ROUNDS,
    seed: int = RANDOM_STATE,
) -> list[dict[str, Any]]:
    """Пробует до `n_trials` уникальных конфигураций, оценивает каждую по
    log loss на `val` (не `test`). Возвращает список по возрастанию log loss."""
    target = TARGET_COLUMNS["1x2"]
    full_train = splits["train"].dropna(subset=[target])
    full_val = splits["val"].dropna(subset=[target])
    classes = sorted(full_train[target].unique())
    cols = list(FEATURE_COLUMNS)

    max_combos = 1
    for values in param_space.values():
        max_combos *= len(values)
    n_trials = min(n_trials, max_combos)

    rng = random.Random(seed)
    seen: set[tuple] = set()
    results = []
    while len(results) < n_trials:
        params = sample_params(rng, param_space)
        key = tuple(sorted(params.items()))
        if key in seen:
            continue
        seen.add(key)

        gbm = _fit_gbm(
            params,
            full_train[cols],
            full_train[target],
            full_val[cols],
            full_val[target],
            classes,
            n_estimators,
            early_stopping_rounds,
        )
        val_proba = gbm.predict_proba(full_val[cols])
        val_metrics = evaluate_multiclass(full_val[target], val_proba, classes)
        results.append(
            {
                "params": params,
                "val_log_loss": val_metrics["log_loss"],
                "best_iteration": int(gbm.best_iteration_ or n_estimators),
            }
        )
        print(f"[{len(results)}/{n_trials}] val_log_loss={val_metrics['log_loss']:.5f} params={params}")

    return sorted(results, key=lambda r: r["val_log_loss"])


def refit_best(
    splits: dict[str, pd.DataFrame],
    best_params: dict[str, Any],
    n_estimators: int = FINAL_N_ESTIMATORS,
    early_stopping_rounds: int = FINAL_EARLY_STOPPING_ROUNDS,
) -> tuple[lgb.LGBMClassifier, dict[str, float]]:
    """Финальный рефит лучшей конфигурации с полным бюджетом деревьев;
    метрики — на `test`, один раз."""
    target = TARGET_COLUMNS["1x2"]
    full = {name: part.dropna(subset=[target]) for name, part in splits.items()}
    classes = sorted(full["train"][target].unique())
    cols = list(FEATURE_COLUMNS)

    gbm = _fit_gbm(
        best_params,
        full["train"][cols],
        full["train"][target],
        full["val"][cols],
        full["val"][target],
        classes,
        n_estimators,
        early_stopping_rounds,
    )
    test_proba = gbm.predict_proba(full["test"][cols])
    test_metrics = evaluate_multiclass(full["test"][target], test_proba, classes)
    return gbm, test_metrics


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n-trials", type=int, default=12)
    parser.add_argument("--models-dir", default="models")
    args = parser.parse_args()

    url = database_url()
    with connect(url) as conn:
        df = load_training_rows(conn)
    df = coerce_feature_columns(df)
    splits = time_split(df)

    results = random_search(splits, args.n_trials)
    best = results[0]
    print("\nЛучшие параметры по val log loss:")
    print(json.dumps(best, indent=2, ensure_ascii=False))

    gbm, test_metrics = refit_best(splits, best["params"])
    print("\nФинальные метрики на test (лучшая конфигурация, полный бюджет деревьев):")
    print(json.dumps(test_metrics, indent=2, ensure_ascii=False))

    models_dir = Path(args.models_dir)
    models_dir.mkdir(parents=True, exist_ok=True)
    joblib.dump(gbm, models_dir / "result_1x2_lightgbm_tuned.joblib")
    report = {"trials": results, "best_params": best["params"], "test_metrics": test_metrics}
    (models_dir / "tune_1x2_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
