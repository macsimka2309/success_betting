"""Обучение модели предсказаний (1X2, ТБ/ТМ лиги).

Спецификация: specs/модель-предсказаний.md, «Обучение модели».

Обучает multiclass LightGBM для `result_1x2` и бинарный LightGBM для
`league_total_over`, с ранней остановкой по log loss на `val`. Сравнивает
с наивным baseline (частоты классов train) и логрегрессией на подмножестве
признаков без систематических пропусков (форма, Эло, контекст лиги, линии).
Печатает метрики (log loss, accuracy, Brier) на `test` и сохраняет артефакты
локально в `models/` (не в Git — см. decision log в спецификации).

Использование:
    python3 -m src.model.train [--models-dir models]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
import psycopg
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, brier_score_loss, log_loss

from src.common.feature_columns import elo_columns, form_columns, league_context_columns
from src.db.connection import connect, database_url
from src.model.features import FEATURE_COLUMNS, LINE_FEATURE_COLUMNS, TARGET_COLUMNS
from src.model.splits import time_split

RANDOM_STATE = 42

# specs/модель-предсказаний.md, «Алгоритм»: подмножество без систематических
# пропусков — в отличие от статистики матча и H2H (15-19% покрытия даже
# в недавних данных). До 03.10.2026 сюда же приходилось исключать
# `home_team_away_*`/`away_team_home_*` из-за бага в compute_form
# (build_features.py, коммит d2c846b) — исправлено, полный пересчёт
# ml_match_features подтверждён, исключение снято.
LR_FEATURE_COLUMNS: tuple[str, ...] = tuple(
    [name for name, _ in form_columns()]
    + [name for name, _ in league_context_columns()]
    + [name for name, _ in elo_columns()]
    + list(LINE_FEATURE_COLUMNS)
)

DEFAULT_GBM_PARAMS: dict[str, Any] = {
    "n_estimators": 2000,
    "learning_rate": 0.05,
    "random_state": RANDOM_STATE,
    "verbosity": -1,
}
DEFAULT_EARLY_STOPPING_ROUNDS = 50


def training_sql(full_stats_only: bool = False) -> str:
    """`full_stats_only` — только матчи, где у обеих команд полная статистика
    за короткое окно (coverage = 1): самый полный набор данных для модели."""
    columns = list(dict.fromkeys(["match_date"] + list(TARGET_COLUMNS.values()) + list(FEATURE_COLUMNS)))
    where = "reg_home IS NOT NULL"
    if full_stats_only:
        where += (
            " AND home_team_overall_short_stats_coverage = 1"
            " AND away_team_overall_short_stats_coverage = 1"
        )
    return f"SELECT {', '.join(columns)} FROM ml_match_features WHERE {where}"


def load_training_rows(conn: psycopg.Connection, full_stats_only: bool = False) -> pd.DataFrame:
    """Сыгранные матчи (`reg_home IS NOT NULL`) с целями и признаками."""
    sql = training_sql(full_stats_only)
    with conn.cursor() as cur:
        cur.execute(sql)
        cols = [d.name for d in cur.description]
        rows = cur.fetchall()
    return pd.DataFrame(rows, columns=cols)


def coerce_feature_columns(df: pd.DataFrame, columns: tuple[str, ...] = FEATURE_COLUMNS) -> pd.DataFrame:
    """Приводит признаки к float64 (psycopg отдаёт NUMERIC как `Decimal`,
    что ломает LightGBM/sklearn без явного приведения); пропуски остаются NaN."""
    df = df.copy()
    for col in columns:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def naive_multiclass_proba(train_y: pd.Series, classes: list[str], n_rows: int) -> np.ndarray:
    """Вероятности = частоты классов на train, одинаковые для всех строк."""
    freqs = train_y.value_counts(normalize=True).reindex(classes, fill_value=0.0).to_numpy()
    return np.tile(freqs, (n_rows, 1))


def naive_binary_proba(train_y: pd.Series, n_rows: int) -> np.ndarray:
    p = float(train_y.astype(int).mean())
    return np.full(n_rows, p)


def evaluate_multiclass(y_true: pd.Series, proba: np.ndarray, classes: list[str]) -> dict[str, float]:
    y_idx = pd.Categorical(y_true, categories=classes).codes
    onehot = np.eye(len(classes))[y_idx]
    pred_idx = proba.argmax(axis=1)
    return {
        "log_loss": float(log_loss(y_idx, proba, labels=list(range(len(classes))))),
        "accuracy": float(accuracy_score(y_idx, pred_idx)),
        "brier": float(np.mean(np.sum((proba - onehot) ** 2, axis=1))),
        "n": int(len(y_true)),
    }


def evaluate_binary(y_true: pd.Series, proba_positive: np.ndarray) -> dict[str, float]:
    y = y_true.astype(int)
    pred = (proba_positive >= 0.5).astype(int)
    return {
        "log_loss": float(log_loss(y, proba_positive, labels=[0, 1])),
        "accuracy": float(accuracy_score(y, pred)),
        "brier": float(brier_score_loss(y, proba_positive)),
        "n": int(len(y_true)),
    }


def train_1x2(
    splits: dict[str, pd.DataFrame],
    models_dir: Path,
    gbm_params: dict[str, Any] | None = None,
    early_stopping_rounds: int = DEFAULT_EARLY_STOPPING_ROUNDS,
) -> dict[str, dict[str, float]]:
    target = TARGET_COLUMNS["1x2"]
    full = {name: part.dropna(subset=[target]) for name, part in splits.items()}
    classes = sorted(full["train"][target].unique())

    naive_test = naive_multiclass_proba(full["train"][target], classes, len(full["test"]))

    lr_cols = list(LR_FEATURE_COLUMNS)
    lr_data = {name: part.dropna(subset=lr_cols) for name, part in full.items()}
    lr = LogisticRegression(max_iter=1000, random_state=RANDOM_STATE)
    lr.fit(lr_data["train"][lr_cols], lr_data["train"][target])
    lr_proba_test = lr.predict_proba(lr_data["test"][lr_cols])

    gbm_cols = list(FEATURE_COLUMNS)
    params = {**DEFAULT_GBM_PARAMS, **(gbm_params or {})}
    gbm = lgb.LGBMClassifier(objective="multiclass", num_class=len(classes), **params)
    gbm.fit(
        full["train"][gbm_cols],
        full["train"][target],
        # eval_X/eval_y (замена eval_set в 4.7.0) пропускают кодирование строковых
        # меток через self._le и падают на object-dtype target — используем
        # по-прежнему eval_set, несмотря на DeprecationWarning.
        eval_set=[(full["val"][gbm_cols], full["val"][target])],
        eval_metric="multi_logloss",
        callbacks=[lgb.early_stopping(early_stopping_rounds, verbose=False)],
    )
    gbm_proba_test = gbm.predict_proba(full["test"][gbm_cols])

    models_dir.mkdir(parents=True, exist_ok=True)
    joblib.dump(gbm, models_dir / "result_1x2_lightgbm.joblib")
    joblib.dump(lr, models_dir / "result_1x2_logreg_baseline.joblib")

    return {
        "naive": evaluate_multiclass(full["test"][target], naive_test, classes),
        "logreg": evaluate_multiclass(lr_data["test"][target], lr_proba_test, list(lr.classes_)),
        "lightgbm": evaluate_multiclass(full["test"][target], gbm_proba_test, list(gbm.classes_)),
    }


def train_league_total(
    splits: dict[str, pd.DataFrame],
    models_dir: Path,
    gbm_params: dict[str, Any] | None = None,
    early_stopping_rounds: int = DEFAULT_EARLY_STOPPING_ROUNDS,
) -> dict[str, dict[str, float]]:
    target = TARGET_COLUMNS["league_total"]
    # league_total_over бывает NULL даже для сыгранных матчей (линия ещё не
    # набрала MIN_LEAGUE_MATCHES в build_features.py) — исключаем такие строки.
    full = {name: part.dropna(subset=[target]) for name, part in splits.items()}

    naive_test = naive_binary_proba(full["train"][target], len(full["test"]))

    lr_cols = list(LR_FEATURE_COLUMNS)
    lr_data = {name: part.dropna(subset=lr_cols) for name, part in full.items()}
    lr = LogisticRegression(max_iter=1000, random_state=RANDOM_STATE)
    lr.fit(lr_data["train"][lr_cols], lr_data["train"][target].astype(int))
    lr_proba_test = lr.predict_proba(lr_data["test"][lr_cols])[:, 1]

    gbm_cols = list(FEATURE_COLUMNS)
    params = {**DEFAULT_GBM_PARAMS, **(gbm_params or {})}
    gbm = lgb.LGBMClassifier(objective="binary", **params)
    gbm.fit(
        full["train"][gbm_cols],
        full["train"][target].astype(int),
        eval_set=[(full["val"][gbm_cols], full["val"][target].astype(int))],
        eval_metric="binary_logloss",
        callbacks=[lgb.early_stopping(early_stopping_rounds, verbose=False)],
    )
    gbm_proba_test = gbm.predict_proba(full["test"][gbm_cols])[:, 1]

    models_dir.mkdir(parents=True, exist_ok=True)
    joblib.dump(gbm, models_dir / "league_total_lightgbm.joblib")
    joblib.dump(lr, models_dir / "league_total_logreg_baseline.joblib")

    return {
        "naive": evaluate_binary(full["test"][target], naive_test),
        "logreg": evaluate_binary(lr_data["test"][target], lr_proba_test),
        "lightgbm": evaluate_binary(full["test"][target], gbm_proba_test),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models-dir", default="models")
    parser.add_argument(
        "--full-stats-only",
        action="store_true",
        help="обучать только на матчах с полной статистикой обеих команд (coverage = 1)",
    )
    args = parser.parse_args()

    url = database_url()
    with connect(url) as conn:
        df = load_training_rows(conn, full_stats_only=args.full_stats_only)
    df = coerce_feature_columns(df)
    splits = time_split(df)

    models_dir = Path(args.models_dir)
    report = {
        "result_1x2": train_1x2(splits, models_dir),
        "league_total_over": train_league_total(splits, models_dir),
    }

    text = json.dumps(report, indent=2, ensure_ascii=False)
    print(text)
    models_dir.mkdir(parents=True, exist_ok=True)
    (models_dir / "metrics.json").write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
